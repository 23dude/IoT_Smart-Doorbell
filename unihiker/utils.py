import threading

import cv2
import numpy as np
from PIL import Image, ImageDraw


class CameraReader(threading.Thread):
    """Background camera reader for smooth UI updates."""

    def __init__(self, cap, screen_w, screen_h):
        super().__init__(daemon=True)
        self.cap = cap
        self.screen_w = screen_w
        self.screen_h = screen_h
        self._frame = None
        self._lock = threading.Lock()
        self._running = True

    def run(self):
        while self._running:
            ret, raw = self.cap.read()
            if not ret:
                continue
            rotated = cv2.rotate(raw, cv2.ROTATE_90_CLOCKWISE)
            h, w = rotated.shape[:2]
            if w != self.screen_w or h != self.screen_h:
                rotated = cv2.resize(
                    rotated,
                    (self.screen_w, self.screen_h),
                    interpolation=cv2.INTER_NEAREST,
                )
            with self._lock:
                self._frame = rotated

    def grab(self):
        with self._lock:
            return self._frame

    def stop(self):
        self._running = False


def to_pil(bgr):
    return Image.fromarray(bgr[:, :, ::-1])


def letterbox(image, new_shape, color=(114, 114, 114)):
    """Resize + pad image while preserving aspect ratio."""
    h, w = image.shape[:2]
    new_h, new_w = new_shape
    scale = min(new_w / w, new_h / h)

    resized_w = int(round(w * scale))
    resized_h = int(round(h * scale))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

    pad_w = new_w - resized_w
    pad_h = new_h - resized_h
    left = pad_w // 2
    right = pad_w - left
    top = pad_h // 2
    bottom = pad_h - top

    out = cv2.copyMakeBorder(
        resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color
    )
    return out, scale, (left, top)


def preprocess(frame, model_h, model_w):
    letterboxed, scale, (pad_x, pad_y) = letterbox(frame, (model_h, model_w))
    rgb = letterboxed[:, :, ::-1]
    normalized = rgb.astype(np.float32) / 255.0
    chw = np.transpose(normalized, (2, 0, 1))
    tensor = np.expand_dims(chw, axis=0)
    return tensor, scale, pad_x, pad_y


def decode_detections(
    raw_output, confidence_threshold, scale, pad_x, pad_y, frame_shape, class_names
):
    """Decode YOLO26N output rows: [x1, y1, x2, y2, confidence, class_id]."""
    h, w = frame_shape[:2]
    arr = np.asarray(raw_output)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2 or arr.shape[1] < 6:
        return []

    detections = []
    for det in arr:
        x1, y1, x2, y2, conf, class_id = det[:6]
        if conf < confidence_threshold:
            continue

        # Map from letterbox space back to original frame coordinates.
        x1 = (float(x1) - pad_x) / scale
        y1 = (float(y1) - pad_y) / scale
        x2 = (float(x2) - pad_x) / scale
        y2 = (float(y2) - pad_y) / scale

        x1 = max(0, min(w - 1, x1))
        y1 = max(0, min(h - 1, y1))
        x2 = max(0, min(w - 1, x2))
        y2 = max(0, min(h - 1, y2))
        if x2 <= x1 or y2 <= y1:
            continue

        cls_idx = int(class_id)
        label = (
            class_names[cls_idx]
            if 0 <= cls_idx < len(class_names)
            else f"class_{cls_idx}"
        )
        detections.append(
            {
                "x1": int(x1),
                "y1": int(y1),
                "x2": int(x2),
                "y2": int(y2),
                "confidence": float(conf),
                "label": label,
            }
        )
    return detections


def draw_boxes(frame, detections, box_color):
    img = to_pil(frame)
    draw = ImageDraw.Draw(img)
    for det in detections:
        x1, y1, x2, y2 = det["x1"], det["y1"], det["x2"], det["y2"]
        tag = f'{det["label"]} {det["confidence"]:.0%}'
        draw.rectangle([x1, y1, x2, y2], outline=box_color, width=2)
        draw.text((x1 + 2, max(0, y1 - 13)), tag, fill=box_color)
    return img, len(detections)


def resolve_model_hw(input_shape):
    """Resolve model input H/W from ONNX shape metadata."""
    if len(input_shape) != 4:
        raise ValueError(f"Unexpected model input shape: {input_shape}")

    h, w = input_shape[2], input_shape[3]
    if isinstance(h, int) and isinstance(w, int):
        return h, w

    # Fallback for dynamic dimensions. This keeps preprocessing valid even if
    # dims are exported as symbolic names.
    return 320, 320
