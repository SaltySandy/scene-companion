"""Run the portrait DA2 metric-indoor ONNX (input 350x266, HxW) on landscape frames.

Three preprocessing modes (all deterministic, CPU):
  stretch  : what server/ai/depth.py does today: anisotropic resize 640x480 -> 266x350
             (x squeezed 0.42, y 0.73; geometry distorted ~1.75x)
  letterbox: isotropic resize to 266 wide (266x200), pad top/bottom to 350 with ImageNet-mean gray
  tiles    : two overlapping 3:4 portrait crops (x 0..360 and 280..640, full height), each
             isotropically resized to 262x350 -> padded to 266, run separately, then stitched
             back into one landscape map (350 x 467) with a linear blend across the overlap.
Output depth is resampled (bilinear) to the requested out_hw (default: 240x320, i.e. half of 640x480)
so downstream code works in one landscape pixel grid regardless of mode.
"""
from __future__ import annotations
import io, os
from functools import lru_cache
import numpy as np
from PIL import Image

def model_path() -> str:
    """SAW_DEPTH_MODEL, else the repo default used by server/ai/depth.py."""
    from server.ai.depth import DEFAULT_MODEL

    return os.environ.get("SAW_DEPTH_MODEL") or str(DEFAULT_MODEL)
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
MH, MW = 350, 266
LAST_TILE_RATIO: list = []  # debug: scale applied to the right tile on the last call


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort
    so = ort.SessionOptions(); so.intra_op_num_threads = int(os.environ.get("SAW_DEPTH_THREADS", "4"))
    return ort.InferenceSession(model_path(), so, providers=["CPUExecutionProvider"])


def _run(img: Image.Image) -> np.ndarray:
    assert img.size == (MW, MH), img.size
    x = ((np.asarray(img, np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
    return _session().run(None, {"pixel_values": x})[0][0].astype(np.float32)


def _resize_depth(d: np.ndarray, hw) -> np.ndarray:
    return np.asarray(Image.fromarray(d).resize((hw[1], hw[0]), Image.BILINEAR), np.float32)


def _pad_img(img: Image.Image, w: int, h: int) -> tuple[Image.Image, int, int]:
    fill = tuple(int(round(m * 255)) for m in MEAN)
    canvas = Image.new("RGB", (w, h), fill)
    ox, oy = (w - img.width) // 2, (h - img.height) // 2
    canvas.paste(img, (ox, oy)); return canvas, ox, oy


def depth_landscape(jpeg_or_img, mode: str = "tiles", out_hw=(240, 320)) -> np.ndarray:
    img = jpeg_or_img if isinstance(jpeg_or_img, Image.Image) else Image.open(io.BytesIO(jpeg_or_img))
    img = img.convert("RGB")
    W, H = img.size
    if mode == "stretch":
        return _resize_depth(_run(img.resize((MW, MH), Image.BICUBIC)), out_hw)
    if mode == "letterbox":
        s = MW / W; nw, nh = MW, int(round(H * s))
        canvas, ox, oy = _pad_img(img.resize((nw, nh), Image.BICUBIC), MW, MH)
        d = _run(canvas)[oy:oy + nh, ox:ox + nw]
        return _resize_depth(d, out_hw)
    if mode == "tiles":
        tw = int(round(H * 3 / 4))                     # 360 for 480-high frames
        xs = [0, W - tw] if W > tw else [0]
        s = MH / H; nw = int(round(tw * s))            # 262
        full_w = int(round(xs[-1] * s)) + nw; acc = np.zeros((MH, full_w), np.float32); wsum = np.zeros_like(acc)
        self_ratio = []
        ov = int(round((xs[0] + tw - xs[-1]) * s)) if len(xs) > 1 else 0
        for x0 in xs:
            crop = img.crop((x0, 0, x0 + tw, H)).resize((nw, MH), Image.BICUBIC)
            canvas, ox, _ = _pad_img(crop, MW, MH)
            d = _run(canvas)[:, ox:ox + nw]
            c0 = int(round(x0 * s)); c1 = min(c0 + nw, full_w); d = d[:, : c1 - c0]
            # weight ramps down toward the tile's inner edge so the overlap blends linearly
            wgt = np.ones(c1 - c0, np.float32)
            if len(xs) > 1 and ov > 0:
                ramp = np.linspace(1, 0, ov, dtype=np.float32)
                if x0 == xs[0]: wgt[-ov:] = ramp
                else: wgt[:ov] = ramp[::-1]
            if x0 != xs[0] and ov > 0:
                # align this tile's metric scale to what is already accumulated (median ratio
                # over the overlap); DA2 picks a slightly different scale per crop -> visible seam
                prev = acc[:, c0:c0 + ov] / np.maximum(wsum[:, c0:c0 + ov], 1e-6)
                ratio = float(np.median(prev / np.maximum(d[:, :ov], 1e-3)))
                d = d * ratio
                self_ratio.append(ratio)
            acc[:, c0:c1] += d * wgt; wsum[:, c0:c1] += wgt
        LAST_TILE_RATIO[:] = self_ratio
        return _resize_depth(acc / np.maximum(wsum, 1e-6), out_hw)
    raise ValueError(mode)
