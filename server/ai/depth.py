"""Metric depth (metres) from one JPEG with ONNX Runtime on the CPU.

The model is Depth Anything V2 Metric Indoor Small, exported once by
scripts/export_depth_onnx.py. Its input size is fixed at export time and
read back from the ONNX graph. Blocking: call predict() via asyncio.to_thread.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("saw.depth")

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODEL = ROOT / "models" / "da2-metric-indoor-s-350x266.onnx"
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class DepthModel:
    def __init__(self, path: Path, threads: int | None = None) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        _, _, self.height, self.width = inp.shape
        self.name = path.stem

    def preprocess(self, jpeg: bytes) -> np.ndarray:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        img = img.resize((self.width, self.height), Image.BICUBIC)
        x = (np.asarray(img, dtype=np.float32) / 255.0 - MEAN) / STD
        return x.transpose(2, 0, 1)[None].astype(np.float32)

    def predict(self, jpeg: bytes) -> np.ndarray:
        """Depth map in metres, shape (height, width), rows top to bottom."""
        out = self.session.run(None, {self.input_name: self.preprocess(jpeg)})[0]
        return out[0]


def load(path: Path | None = None) -> DepthModel | None:
    """Load the model named by SAW_DEPTH_MODEL (or the default). None if missing."""
    path = path or Path(os.environ.get("SAW_DEPTH_MODEL") or DEFAULT_MODEL)
    if not path.exists():
        log.warning("depth model not found at %s (run scripts/export_depth_onnx.py)", path)
        return None
    threads = int(os.environ.get("SAW_DEPTH_THREADS") or 0) or None
    model = DepthModel(path, threads)
    log.info("depth model %s input %dx%d", model.name, model.height, model.width)
    return model
