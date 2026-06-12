#!/usr/bin/env python3
"""UniHiker M10 smart-doorbell.

Runs a continuous YOLO26N detection loop and uploads any frame whose top
detection exceeds CONF_THRESHOLD to Cloudinary (with a time-based cooldown),
then writes the image URL + metadata to InfluxDB.

A small on-device dashboard shows FPS, object count, and top class, plus
CAMERA and DOORBELL buttons; CAMERA opens a live annotated feed, BACK returns.
The doorbell buzzes and publishes to HiveMQ so the laptop dashboard can alert.
The LED is controlled remotely via a second MQTT topic.
"""

import json
import os
import queue
import ssl
import tempfile
import threading
import time
import uuid
from datetime import datetime

import cloudinary
import cloudinary.uploader
import cv2
import onnxruntime as ort
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from PIL import Image
from pinpong.board import Board, Pin
from pinpong.extension.unihiker import buzzer
from unihiker import GUI

import device_config as cfg

from utils import (
    CameraReader,
    decode_detections,
    draw_boxes,
    preprocess,
    resolve_model_hw,
)

# ─── Constants ───────────────────────────────────────────────────────────────

SCREEN_W, SCREEN_H = 240, 320
CAP_W, CAP_H = 320, 240
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolo25n.onnx")

# Main-screen buttons (ttk.Button uses place; values must match restore after camera view).
# Placed below LED line (led_label y=176) so CAMERA does not cover MQTT/LED status.
CAMERA_BTN_X = SCREEN_W // 2
CAMERA_BTN_Y = 216
CAMERA_BTN_W = 160
CAMERA_BTN_H = 36
DOORBELL_BTN_X = SCREEN_W // 2
DOORBELL_BTN_Y = 272
DOORBELL_BTN_W = 180
DOORBELL_BTN_H = 60

# Camera feed overlay + BACK control
FEED_IMG_W = 240
FEED_IMG_H = 260
BACK_BTN_X = SCREEN_W // 2
BACK_BTN_Y = 300
BACK_BTN_W = 160
BACK_BTN_H = 40

CAMERA_FEED_PLACEHOLDER = Image.new("RGB", (FEED_IMG_W, FEED_IMG_H), (16, 16, 16))

# "main" | "camera" — updated from button callbacks (Tk thread); main loop applies UI.
screen_mode = ["main"]

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven",
    "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
]

# ─── GUI setup ───────────────────────────────────────────────────────────────

gui = GUI()
gui.fill_rect(x=0, y=0, w=SCREEN_W, h=SCREEN_H, color="#101010")

title = gui.draw_text(
    text="Smart Doorbell", x=SCREEN_W // 2, y=16,
    font_size=14, color="#FFCC00", origin="center",
)
fps_label = gui.draw_text(
    text="FPS: --", x=12, y=48, font_size=18, color="#FFFFFF",
)
count_label = gui.draw_text(
    text="Objects: 0", x=12, y=82, font_size=18, color="#FFFFFF",
)
class_label = gui.draw_text(
    text="Top: -", x=12, y=116, font_size=18, color="#00FF88",
)
status_label = gui.draw_text(
    text="MQTT: connecting...", x=12, y=154, font_size=11, color="#AAAAAA",
)
led_label = gui.draw_text(
    text="LED: off", x=12, y=176, font_size=11, color="#AAAAAA",
)


def _set_status(text, color="#AAAAAA"):
    try:
        status_label.config(text=text, color=color)
    except Exception:
        pass


def _set_led_text(text, color="#AAAAAA"):
    try:
        led_label.config(text=text, color=color)
    except Exception:
        pass


# ─── Hardware ────────────────────────────────────────────────────────────────

Board().begin()
led_pin = Pin(Pin.P25, Pin.OUT)
led_pin.value(0)

# ─── Cloudinary + Influx clients ─────────────────────────────────────────────

cloudinary.config(
    cloud_name=cfg.CLOUDINARY_CLOUD_NAME,
    api_key=cfg.CLOUDINARY_API_KEY,
    api_secret=cfg.CLOUDINARY_API_SECRET,
    secure=True,
)

influx_client = InfluxDBClient(
    url=cfg.INFLUX_URL, token=cfg.INFLUX_TOKEN, org=cfg.INFLUX_ORG, timeout=10000,
)
influx_write = influx_client.write_api(write_options=SYNCHRONOUS)


def upload_to_cloudinary(pil_img, labels):
    """Save annotated PIL image to a temp JPEG and upload. Returns secure URL or None.

    Cloudinary `public_id` (image name in the URL path — *not* written to InfluxDB):
      The laptop dashboard never parses `public_id`; it only displays the `url` you
      store in InfluxDB. Still, use a stable, URL-safe pattern so uploads succeed:
      - Recommended shape:
          "{device}/det_{YYYYMMDD}_{HHMMSS}_{label_slug}"
        where `device` is ``cfg.INFLUX_DEVICE_TAG`` (e.g. ``unihiker_m10``),
        timestamp is UTC, and `label_slug` is the *top* COCO class with spaces and
        odd characters replaced by ``_`` (e.g. ``hot_dog``, ``potted_plant``).
      - Use only ASCII letters, digits, ``_``, ``-``, and ``/`` in ``public_id``.
      - Keep a folder prefix (``…/det_…``) so uploads are grouped in the Cloudinary UI.

    Return value: the full HTTPS **secure_url** string from the upload response (that
    exact string is what must be written to InfluxDB field ``url``).
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    top_label = labels[0] if labels else "object"
    label_slug = "".join(c if c.isalnum() else "_" for c in top_label).strip("_") or "object"
    public_id = f"{cfg.INFLUX_DEVICE_TAG}/det_{timestamp}_{label_slug}"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        pil_img.save(tmp_path, format="JPEG", quality=85)
        result = cloudinary.uploader.upload(
            tmp_path, public_id=public_id, overwrite=True,
        )
        return result.get("secure_url") or result.get("url")
    except Exception as exc:
        print(f"[cloudinary] upload error: {exc}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as exc:
                print(f"[cloudinary] temp cleanup failed: {exc}")


def write_detection_to_influx(url, top_class, count, confidence):
    """Write one detection row for the Streamlit dashboard (`dashboard/app.py`).

    The dashboard runs a Flux query that **pivots** field keys into columns, then
    reads ``url``, ``count``, ``confidence``, and ``top_class`` from each row.
    Spelling and types must match exactly or the timeline / image viewer breaks.

    Bucket / org / token: use ``cfg.INFLUX_BUCKET``, ``cfg.INFLUX_ORG``, and
    ``cfg.INFLUX_TOKEN`` (defaults must match ``dashboard/dashboard_config.py``:
    bucket ``image_url_bucket``, measurement ``doorbell_detection``).

    Point layout (Line protocol names are case-sensitive):
      - **Measurement:** ``cfg.INFLUX_MEASUREMENT`` (default ``doorbell_detection``).
      - **Tags:** ``device`` = ``cfg.INFLUX_DEVICE_TAG`` (e.g. ``unihiker_m10``);
        ``top_class`` = the same string as ``top["label"]`` from inference (COCO
        names may include spaces: ``"hot dog"``, ``"potted plant"`` — do not slugify
        for Influx; the dashboard displays this string).
      - **Fields** (these exact names):
          - ``url`` — **str**, the full Cloudinary **secure_url** (``https://…``).
          - ``count`` — **int**, number of detections in the frame (``len(detections)``).
          - ``confidence`` — **float**, top box score in ``0.0``–``1.0`` (not percent).
      - **Time:** write with **seconds** precision (``WritePrecision.S``); use default
        "now" or an explicit UTC time consistent with your course's expectations.

    Do **not** rename fields (e.g. ``image_url``) or store ``confidence`` as a string;
    ``load_detections()`` uses ``int()`` / ``float()`` on field values.
    """
    try:
        point = (
            Point(cfg.INFLUX_MEASUREMENT)
            .tag("device", cfg.INFLUX_DEVICE_TAG)
            .tag("top_class", top_class)
            .field("url", str(url))
            .field("count", int(count))
            .field("confidence", float(confidence))
            .time(datetime.utcnow(), WritePrecision.S)
        )
        influx_write.write(
            bucket=cfg.INFLUX_BUCKET, org=cfg.INFLUX_ORG, record=point,
        )
        return True
    except Exception as exc:
        print(f"[influx] write error: {exc}")
        return False


# ─── Uploader worker thread ──────────────────────────────────────────────────

upload_queue: "queue.Queue" = queue.Queue(maxsize=4)


def uploader_loop():
    while True:
        job = upload_queue.get()
        if job is None:
            break
        pil_img, detections = job
        labels = [d["label"] for d in detections]
        top = max(detections, key=lambda d: d["confidence"])
        _set_status(f"Uploading {top['label']}...", "#00CCFF")
        url = upload_to_cloudinary(pil_img, labels)
        if not url:
            _set_status("Upload failed", "#FF4444")
            continue
        ok = write_detection_to_influx(url, top["label"], len(detections), top["confidence"])
        if ok:
            _set_status(f"Uploaded {top['label']}", "#00FF88")
        else:
            _set_status("Influx write failed", "#FF8800")
        print(f"[upload] {url} ({top['label']} {top['confidence']:.2f})")


threading.Thread(target=uploader_loop, daemon=True).start()


# ─── MQTT client ─────────────────────────────────────────────────────────────

mqtt_connected = threading.Event()


def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(cfg.TOPIC_LED, qos=1)
        mqtt_connected.set()
        _set_status("MQTT: connected", "#00FF88")
        print(f"[mqtt] connected; subscribed to {cfg.TOPIC_LED}")
    else:
        _set_status(f"MQTT err rc={rc}", "#FF4444")
        print(f"[mqtt] connect failed rc={rc}")


def on_mqtt_disconnect(client, userdata, rc):
    mqtt_connected.clear()
    _set_status("MQTT: disconnected", "#FF8800")
    print(f"[mqtt] disconnected rc={rc}")


def on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        print(f"[mqtt] bad payload on {msg.topic}: {msg.payload!r}")
        return
    if msg.topic == cfg.TOPIC_LED:
        state = str(payload.get("state", "")).lower()
        if state == "on":
            led_pin.value(1)
            _set_led_text("LED: ON", "#FFFF00")
            print("[led] ON")
        elif state == "off":
            led_pin.value(0)
            _set_led_text("LED: off", "#AAAAAA")
            print("[led] off")


mqtt_client = mqtt.Client(client_id=cfg.MQTT_CLIENT_ID)
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_disconnect = on_mqtt_disconnect
mqtt_client.on_message = on_mqtt_message
mqtt_client.username_pw_set(cfg.BROKER_USERNAME, cfg.BROKER_PASSWORD)
_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
_ssl_ctx.load_default_certs()
mqtt_client.tls_set_context(_ssl_ctx)

try:
    mqtt_client.connect(cfg.BROKER_HOST, cfg.BROKER_PORT, keepalive=60)
    mqtt_client.loop_start()
except Exception as exc:
    print(f"[mqtt] initial connect failed: {exc}")
    _set_status("MQTT offline", "#FF4444")


# ─── Doorbell button ────────────────────────────────────────────────────────

def play_doorbell_tune():
    start = time.monotonic()
    try:
        buzzer.pitch(523, 1)
        buzzer.pitch(659, 2)
        buzzer.pitch(784, 2)
        remaining = 1.0 - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(remaining)
    except Exception as exc:
        print(f"[buzzer] error: {exc}")
    finally:
        # Hard stop so the buzzer never latches on indefinitely.
        try:
            buzzer.stop()
        except Exception as exc:
            print(f"[buzzer] stop error: {exc}")


def on_doorbell_click():
    threading.Thread(target=play_doorbell_tune, daemon=True).start()
    payload = json.dumps({
        "ts": datetime.utcnow().isoformat() + "Z",
        "id": uuid.uuid4().hex,
    })
    try:
        info = mqtt_client.publish(cfg.TOPIC_PRESS, payload, qos=1)
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            _set_status("Bell sent", "#00FF88")
        else:
            _set_status(f"Bell rc={info.rc}", "#FF4444")
    except Exception as exc:
        _set_status(f"Bell err: {exc}", "#FF4444")


def on_camera_click():
    screen_mode[0] = "camera"


def on_back_click():
    screen_mode[0] = "main"


camera_button = gui.add_button(
    x=CAMERA_BTN_X, y=CAMERA_BTN_Y, w=CAMERA_BTN_W, h=CAMERA_BTN_H,
    text="CAMERA",
    origin="center",
    onclick=on_camera_click,
)

doorbell_button = gui.add_button(
    x=DOORBELL_BTN_X, y=DOORBELL_BTN_Y, w=DOORBELL_BTN_W, h=DOORBELL_BTN_H,
    text="DOORBELL",
    origin="center",
    onclick=on_doorbell_click,
)


# ─── Detection loop ──────────────────────────────────────────────────────────

def main():
    try:
        session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    except Exception as exc:
        print(f"[yolo] failed to load model: {exc}")
        _set_status("Model load failed", "#FF4444")
        return

    input_meta = session.get_inputs()[0]
    input_name = input_meta.name
    try:
        model_h, model_w = resolve_model_hw(input_meta.shape)
    except ValueError as exc:
        print(exc)
        return
    print(f"[yolo] input {input_name} shape={input_meta.shape} HxW={model_h}x{model_w}")

    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_H)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print("[camera] cannot open USB camera")
        _set_status("Camera open failed", "#FF4444")
        return

    reader = CameraReader(cap, SCREEN_W, SCREEN_H)
    reader.start()

    feed_image = None
    back_button = None
    applied_mode = "main"
    last_feed_update = 0.0

    def forget_main_buttons():
        camera_button.button.place_forget()
        doorbell_button.button.place_forget()

    def restore_main_buttons():
        camera_button.button.place(
            x=CAMERA_BTN_X, y=CAMERA_BTN_Y,
            width=CAMERA_BTN_W, height=CAMERA_BTN_H,
            anchor="center",
        )
        doorbell_button.button.place(
            x=DOORBELL_BTN_X, y=DOORBELL_BTN_Y,
            width=DOORBELL_BTN_W, height=DOORBELL_BTN_H,
            anchor="center",
        )

    def enter_camera_view():
        nonlocal feed_image, back_button
        forget_main_buttons()
        if feed_image is None:
            feed_image = gui.draw_image(
                x=0, y=0, w=FEED_IMG_W, h=FEED_IMG_H,
                image=CAMERA_FEED_PLACEHOLDER,
                origin="nw",
            )
        if back_button is None:
            back_button = gui.add_button(
                x=BACK_BTN_X, y=BACK_BTN_Y, w=BACK_BTN_W, h=BACK_BTN_H,
                text="BACK",
                origin="center",
                onclick=on_back_click,
            )

    def leave_camera_view():
        nonlocal feed_image, back_button
        if back_button is not None:
            back_button.remove()
            back_button = None
        if feed_image is not None:
            feed_image.remove()
            feed_image = None
        restore_main_buttons()

    last_upload = 0.0
    frame_count = 0
    fps_timer = time.monotonic()

    try:
        while True:
            frame = reader.grab()
            if frame is None:
                time.sleep(0.005)
                continue

            if screen_mode[0] != applied_mode:
                if applied_mode == "camera" and screen_mode[0] == "main":
                    leave_camera_view()
                elif applied_mode == "main" and screen_mode[0] == "camera":
                    enter_camera_view()
                applied_mode = screen_mode[0]

            input_tensor, scale, pad_x, pad_y = preprocess(frame, model_h, model_w)
            outputs = session.run(None, {input_name: input_tensor})
            detections = decode_detections(
                outputs[0],
                confidence_threshold=cfg.CONF_THRESHOLD,
                scale=scale, pad_x=pad_x, pad_y=pad_y,
                frame_shape=frame.shape,
                class_names=COCO_CLASSES,
            )

            frame_count += 1
            now = time.monotonic()
            elapsed = now - fps_timer

            if elapsed >= cfg.FPS_WINDOW_SEC:
                fps = frame_count / elapsed
                frame_count = 0
                fps_timer = now
                try:
                    fps_label.config(text=f"FPS: {fps:.1f}")
                    count_label.config(text=f"Objects: {len(detections)}")
                    if detections:
                        top = max(detections, key=lambda d: d["confidence"])
                        class_label.config(
                            text=f"Top: {top['label']} {top['confidence']:.0%}",
                            color="#00FF88",
                        )
                    else:
                        class_label.config(text="Top: -", color="#888888")
                except Exception:
                    pass

            if screen_mode[0] == "camera" and feed_image is not None:
                if now - last_feed_update >= cfg.CAMERA_VIEW_UPDATE_SEC:
                    last_feed_update = now
                    pil_frame, _ = draw_boxes(frame, detections, "#00FF00")
                    try:
                        feed_image.config(image=pil_frame, w=FEED_IMG_W, h=FEED_IMG_H)
                    except Exception:
                        pass

            if detections and (now - last_upload) >= cfg.COOLDOWN_SEC:
                last_upload = now
                top = max(detections, key=lambda d: d["confidence"])
                annotated, _ = draw_boxes(frame, detections, "#00FF00")
                try:
                    upload_queue.put_nowait((annotated, list(detections)))
                except queue.Full:
                    print(f"[upload] queue full, skipping {top['label']}")

    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        cap.release()
        if applied_mode == "camera":
            try:
                leave_camera_view()
            except Exception:
                pass
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
