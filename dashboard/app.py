"""Streamlit dashboard for the smart doorbell.

Features:
  - vis.js timeline of recent detections (last 24 h) from InfluxDB.
  - Click a timeline item to view the annotated image from Cloudinary.
  - Sidebar LED On / LED Off buttons publish to MQTT so the UniHiker toggles
    its built-in LED.
  - Subscribes to `doorbell/press` on the same MQTT connection used for LED
    commands and appends presses to `events.json`. Also polls every 1.5 s;
    when a new press appears it shows a toast + red banner and plays a sound
    via `chime` in a background thread. (Optional: run `mqtt_listener.py`
    separately only if you want a bridge without Streamlit — do not run both
    or you will get duplicate events.)

Run with:

    streamlit run app.py
"""

from __future__ import annotations

import json
import ssl
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

import paho.mqtt.client as mqtt
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from streamlit_timeline import st_timeline

import dashboard_config as cfg
import events_store

# ─── Page setup ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Smart Doorbell", page_icon=":bell:", layout="wide")
st.title(":bell: Smart Doorbell Dashboard")

# Poll for new events + refresh timeline.
st_autorefresh(interval=cfg.REFRESH_INTERVAL_MS, key="doorbell_autorefresh")

if "last_seen_event_id" not in st.session_state:
    latest = events_store.latest_event()
    st.session_state.last_seen_event_id = latest["id"] if latest else None
if "press_banner_until" not in st.session_state:
    st.session_state.press_banner_until = 0.0
if "selected_detection" not in st.session_state:
    st.session_state.selected_detection = None

# ─── MQTT publisher (cached across reruns) ──────────────────────────────────

def _mqtt_on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(cfg.TOPIC_PRESS, qos=1)


def _mqtt_on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    events_store.append_event(payload)


@st.cache_resource(show_spinner=False)
def get_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(client_id=cfg.MQTT_PUBLISHER_CLIENT_ID)
    client.on_connect = _mqtt_on_connect
    client.on_message = _mqtt_on_message
    client.username_pw_set(cfg.BROKER_USERNAME, cfg.BROKER_PASSWORD)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
    ssl_ctx.load_default_certs()
    client.tls_set_context(ssl_ctx)
    client.connect(cfg.BROKER_HOST, cfg.BROKER_PORT, keepalive=60)
    client.loop_start()
    return client


def ensure_mqtt_press_subscriber() -> None:
    """Start MQTT (subscribe + LED publish) once per session so presses reach events.json."""
    try:
        get_mqtt_client()
    except Exception as exc:
        st.sidebar.warning(f"MQTT subscriber offline — doorbell toasts need the broker. ({exc})")


ensure_mqtt_press_subscriber()


def publish_led(state: str) -> bool:
    try:
        client = get_mqtt_client()
        info = client.publish(
            cfg.TOPIC_LED, json.dumps({"state": state}), qos=1
        )
        return info.rc == mqtt.MQTT_ERR_SUCCESS
    except Exception as exc:
        st.sidebar.error(f"MQTT publish failed: {exc}")
        return False


# ─── InfluxDB query ──────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def get_influx():
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(
        url=cfg.INFLUX_URL, token=cfg.INFLUX_TOKEN,
        org=cfg.INFLUX_ORG, timeout=10000,
    )
    return client, client.query_api()


def load_detections() -> List[Dict[str, Any]]:
    _client, query_api = get_influx()
    flux = f'''
from(bucket: "{cfg.INFLUX_BUCKET}")
  |> range(start: {cfg.TIMELINE_LOOKBACK})
  |> filter(fn: (r) => r._measurement == "{cfg.INFLUX_MEASUREMENT}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"], desc: false)
'''
    try:
        tables = query_api.query(flux)
    except Exception as exc:
        st.error(f"InfluxDB query failed: {exc}")
        return []

    records: List[Dict[str, Any]] = []
    for table in tables:
        for row in table.records:
            values = row.values
            url = values.get("url")
            if not url:
                continue
            records.append({
                "time": row.get_time(),
                "url": url,
                "top_class": values.get("top_class", "object"),
                "count": int(values.get("count", 1) or 1),
                "confidence": float(values.get("confidence", 0.0) or 0.0),
            })
    return records


# ─── Doorbell press polling ──────────────────────────────────────────────────

def _chime_async():
    def _run():
        try:
            import chime

            chime.info()
        except Exception as exc:
            print(f"[chime] error: {exc}")
    threading.Thread(target=_run, daemon=True).start()


def _handle_new_presses():
    events = events_store.read_events()
    if not events:
        return
    latest = events[-1]
    if latest["id"] == st.session_state.last_seen_event_id:
        return
    if st.session_state.last_seen_event_id is None:
        st.session_state.last_seen_event_id = latest["id"]
        return
    st.session_state.last_seen_event_id = latest["id"]
    st.session_state.press_banner_until = datetime.now(timezone.utc).timestamp() + 5.0
    _chime_async()
    st.toast("Doorbell pressed!", icon=":material/notifications_active:")


_handle_new_presses()

now_ts = datetime.now(timezone.utc).timestamp()
if st.session_state.press_banner_until > now_ts:
    secs_left = int(st.session_state.press_banner_until - now_ts) + 1
    st.error(f":rotating_light: Someone rang the doorbell! (dismissing in {secs_left}s)")


# ─── Sidebar: LED controls ──────────────────────────────────────────────────

with st.sidebar:
    st.header("UniHiker LED")
    col_on, col_off = st.columns(2)
    with col_on:
        if st.button("LED ON", width="stretch", type="primary"):
            if publish_led("on"):
                st.success("LED ON sent")
    with col_off:
        if st.button("LED OFF", width="stretch"):
            if publish_led("off"):
                st.success("LED OFF sent")

    st.divider()
    st.header("Recent presses")
    presses = events_store.read_events(limit=10)
    if not presses:
        st.caption("No doorbell presses yet.")
    else:
        for ev in reversed(presses):
            st.write(f":bell: `{ev.get('ts') or ev.get('received_at')}`")


# ─── Main: timeline + image viewer ───────────────────────────────────────────

records = load_detections()

# Extension 2.4: filter timeline by detected class.
all_classes = sorted({r["top_class"] for r in records})
if all_classes:
    selected_classes = st.multiselect(
        "Filter by class",
        options=all_classes,
        default=all_classes,
        help="Hide events whose top class is not selected.",
    )
    records = [r for r in records if r["top_class"] in selected_classes]

st.subheader(f"Detections (last 24h) — {len(records)} events")

if not records:
    st.info("No detections match the current filter. When the UniHiker sees an object, it will show up here.")
else:
    items = [
        {
            "id": i,
            "content": f"{r['top_class']} {r['confidence']:.0%}",
            "start": r["time"].isoformat(),
            "title": f"{r['top_class']} · conf {r['confidence']:.0%} · {r['count']} obj",
        }
        for i, r in enumerate(records)
    ]
    selected = st_timeline(
        items,
        groups=[],
        options={"stack": True, "zoomKey": "ctrlKey", "height": "280px"},
        height="300px",
    )
    if selected and isinstance(selected, dict) and "id" in selected:
        try:
            idx = int(selected["id"])
            if 0 <= idx < len(records):
                st.session_state.selected_detection = records[idx]
        except (ValueError, TypeError):
            pass

    chosen = st.session_state.selected_detection
    if chosen is None:
        st.caption("Click a timeline item to view its image.")
    else:
        col_img, col_meta = st.columns([3, 1])
        with col_img:
            st.image(
                chosen["url"],
                caption=f"{chosen['top_class']} · {chosen['confidence']:.0%}",
                width="stretch",
            )
        with col_meta:
            st.markdown("**Detection details**")
            st.write(f"Time: `{chosen['time'].isoformat()}`")
            st.write(f"Top class: **{chosen['top_class']}**")
            st.write(f"Confidence: **{chosen['confidence']:.1%}**")
            st.write(f"Objects in frame: **{chosen['count']}**")
            st.markdown(f"[Open image in new tab]({chosen['url']})")
