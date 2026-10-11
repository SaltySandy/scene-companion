"""YOLOv8n / YOLO11n ONNX detector. CPU, latest-frame friendly.

COCO has ``chair`` (56) and other furniture; it has no EXIT sign and no door.
Missing model → empty list, never an exception on the hot path.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("saw.yolo")

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL = ROOT / "models" / "yolov8n.onnx"

# Furniture / people that block a walking path. Small clutter is ignored.
OBSTACLE_CLASSES = {
    0: "person",
    13: "bench",
    24: "backpack",
    26: "handbag",
    28: "suitcase",
    56: "chair",
    57: "couch",
    59: "bed",
    60: "table",
    62: "tv",
    63: "laptop",
    72: "refrigerator",
}

COCO80 = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)


@dataclass(frozen=True)
class Det:
    label: str
    conf: float
    x0: float
    y0: float
    x1: float
    y1: float  # normalized 0–1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)


_session: Any = None
_tried = False
_input_name = "images"
_in_size = 320


def model_path() -> Path:
    return Path(os.environ.get("SAW_YOLO_MODEL") or DEFAULT_MODEL)


def input_size() -> int:
    raw = (os.environ.get("SAW_YOLO_SIZE") or "320").strip()
    try:
        return max(160, min(640, int(raw)))
    except ValueError:
        return 320


def available() -> bool:
    return model_path().is_file()


def load() -> str | None:
    """Blocking. Safe to call more than once. None if the ONNX file is missing."""
    global _session, _tried, _input_name, _in_size
    if _tried:
        return None if _session is None else "yolov8n"
    _tried = True
    path = model_path()
    if not path.is_file():
        log.info("YOLO model missing at %s (run scripts/fetch_nav_models.py)", path)
        return None
    try:
        import onnxruntime as ort
    except ImportError as exc:
        log.warning("onnxruntime missing (%s); YOLO off", exc)
        _session = None
        return None

    opts = ort.SessionOptions()
    threads = int(os.environ.get("SAW_YOLO_THREADS") or 0) or None
    if threads:
        opts.intra_op_num_threads = threads
    try:
        _session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    except Exception:
        log.exception("YOLO failed to load %s", path)
        _session = None
        return None
    inp = _session.get_inputs()[0]
    _input_name = inp.name
    shape = inp.shape
    # [1, 3, H, W] or dynamic
    try:
        h = int(shape[2]) if shape[2] not in (None, "height") else input_size()
    except (TypeError, ValueError, IndexError):
        h = input_size()
    _in_size = h
    log.info("YOLO %s input %s", path.name, shape)
    return path.stem


def detect(jpeg: bytes, *, conf: float = 0.25, iou: float = 0.45) -> list[Det]:
    """Boxes in normalized image coordinates. Empty if the model is missing."""
    if _session is None:
        load()
    if _session is None:
        return []
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return []
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    size = _in_size or input_size()
    scale = min(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    resized = img.resize((nw, nh), Image.BILINEAR)
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    canvas.paste(resized, (pad_x, pad_y))
    x = (np.asarray(canvas, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    out = _session.run(None, {_input_name: x})[0]
    return _decode(out, w, h, size, pad_x, pad_y, nw, nh, conf, iou)


def _decode(raw, w, h, size, pad_x, pad_y, nw, nh, conf_th, iou_th) -> list[Det]:
    import numpy as np

    arr = np.asarray(raw)
    if arr.ndim == 3:
        arr = arr[0]
    # [84, N] or [N, 84]
    if arr.shape[0] in (84, 85) and arr.shape[1] > arr.shape[0]:
        arr = arr.T
    if arr.shape[1] < 6:
        return []
    boxes = arr[:, :4]
    scores = arr[:, 4:]
    if scores.shape[1] == 81:  # obj * cls
        obj = scores[:, 0]
        cls_scores = scores[:, 1:]
        cls_id = cls_scores.argmax(axis=1)
        conf = obj * cls_scores[np.arange(len(arr)), cls_id]
    else:
        cls_id = scores.argmax(axis=1)
        conf = scores[np.arange(len(arr)), cls_id]
    keep = conf >= conf_th
    boxes, conf, cls_id = boxes[keep], conf[keep], cls_id[keep]
    if boxes.size == 0:
        return []
    # xywh center → xyxy in letterboxed pixels
    cx, cy, bw, bh = boxes.T
    x0 = cx - bw / 2
    y0 = cy - bh / 2
    x1 = cx + bw / 2
    y1 = cy + bh / 2
    # undo letterbox
    x0 = (x0 - pad_x) / max(nw, 1) * w
    x1 = (x1 - pad_x) / max(nw, 1) * w
    y0 = (y0 - pad_y) / max(nh, 1) * h
    y1 = (y1 - pad_y) / max(nh, 1) * h
    dets = []
    for i in range(len(conf)):
        cid = int(cls_id[i])
        label = OBSTACLE_CLASSES.get(cid)
        if label is None:
            if 0 <= cid < len(COCO80):
                # ignore non-obstacle classes
                continue
            continue
        dets.append(
            Det(
                label=label,
                conf=float(conf[i]),
                x0=float(max(0.0, x0[i] / w)),
                y0=float(max(0.0, y0[i] / h)),
                x1=float(min(1.0, x1[i] / w)),
                y1=float(min(1.0, y1[i] / h)),
            )
        )
    return _nms(dets, iou_th)


def _nms(dets: list[Det], iou_th: float) -> list[Det]:
    dets = sorted(dets, key=lambda d: d.conf, reverse=True)
    kept: list[Det] = []
    for det in dets:
        if all(_iou(det, k) < iou_th or det.label != k.label for k in kept):
            kept.append(det)
        if len(kept) >= 12:
            break
    return kept


def _iou(a: Det, b: Det) -> float:
    ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
    ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0
