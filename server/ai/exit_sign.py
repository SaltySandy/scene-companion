"""EXIT-sign detector: illuminated red + compact rectangular blob.

COCO has no exit class. A real EXIT pictogram is a small, high-saturation red
(or red-on-white) rectangle, usually in the upper third of the frame. This is
a color + shape heuristic, not OCR: it stays under a few milliseconds on CPU.

An open doorway (dark rectangle, no red) is intentionally not an exit.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from server.nav import Bearing, Distance, Visible


@dataclass(frozen=True)
class ExitBlob:
    visible: Visible
    bearing: Bearing
    distance: Distance
    distance_m: float | None
    cx: float  # 0–1
    cy: float
    x0: float
    y0: float
    x1: float
    y1: float
    red_frac: float
    conf: float


def _bearing(cx: float) -> Bearing:
    if cx < 0.34:
        return "left"
    if cx > 0.66:
        return "right"
    return "center"


def _distance(area: float, cy: float) -> tuple[Distance, float]:
    # Small high-up sign → far. Larger / lower → closer.
    if area > 0.004 or cy > 0.28:
        return "close", 1.2
    if area > 0.0015 or cy > 0.18:
        return "mid", 3.5
    if area > 0.00035:
        return "far", 6.5
    return "far", 8.0


def find_exit(jpeg: bytes) -> ExitBlob | None:
    """Return the best EXIT-like blob, or None if the frame has no red sign."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return None
    # Upper 60% is enough; signs hang over doors, not on the floor.
    w, h = img.size
    crop_h = max(8, int(h * 0.62))
    arr = np.asarray(img.crop((0, 0, w, crop_h)), dtype=np.uint8)
    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)
    # Illuminated red: high R, R dominates G/B. Works on a dark room too.
    mask = (r > 145) & (r > g + 40) & (r > b + 40) & (r > 90)
    # Also catch dimmer red letters (low-light fixtures).
    dim = (r > 110) & (r > g + 28) & (r > b + 28) & ((r.astype(np.int32) - g) + (r - b) > 70)
    mask = mask | dim
    ys, xs = np.where(mask)
    n = int(xs.size)
    if n < 6:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw = max(1, x1 - x0)
    bh = max(1, y1 - y0)
    aspect = bw / bh
    # EXIT pictograms are wider than tall. A random red fire alarm is squarer
    # or a thin vertical streak; keep a loose band so a partial edge crop lives.
    if aspect < 0.7 or aspect > 10.0:
        # Still accept a tiny cluster of red letters (the word EXIT).
        if n < 18 and aspect < 0.7:
            return None
    area = (bw / w) * (bh / max(crop_h, 1))
    cx = float(xs.mean() / w)
    cy = float(ys.mean() / h)
    red_frac = n / float(arr.shape[0] * arr.shape[1])
    at_edge = cx < 0.10 or cx > 0.90
    if n >= 28 and not at_edge:
        visible: Visible = "yes"
        conf = 0.85
    elif n >= 8:
        visible = "partial"
        conf = 0.55
    else:
        return None
    if at_edge:
        visible = "partial"
        conf = min(conf, 0.6)
    dist, meters = _distance(area, cy)
    # Box in full-image normalized coords.
    return ExitBlob(
        visible=visible,
        bearing=_bearing(cx),
        distance=dist,
        distance_m=meters,
        cx=cx,
        cy=cy,
        x0=x0 / w,
        y0=y0 / h,
        x1=x1 / w,
        y1=y1 / h,
        red_frac=red_frac,
        conf=conf,
    )


def door_slab(jpeg: bytes) -> dict | None:
    """Large mid-gray vertical rectangle (closed door). Not an open doorway."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    arr = np.asarray(img, dtype=np.uint8)
    r, g, b = (arr[:, :, i].astype(np.int16) for i in range(3))
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    chroma = np.abs(r - g) + np.abs(g - b)
    # Closed painted door: mid gray, low chroma. Open doorway is much darker.
    door = (luma > 70) & (luma < 155) & (chroma < 28)
    # Ignore the floor band and the ceiling.
    door[: int(h * 0.08), :] = False
    door[int(h * 0.88) :, :] = False
    if door.mean() < 0.04:
        return None
    # Column occupancy: a door is a wide-ish run of gray columns.
    col = door.mean(axis=0)
    active = np.where(col > 0.18)[0]
    if active.size < w * 0.10:
        return None
    x0, x1 = int(active.min()), int(active.max())
    width = (x1 - x0) / w
    if width < 0.12 or width > 0.62:
        return None
    cx = (x0 + x1) / 2 / w
    return {"x0": x0 / w, "x1": x1 / w, "cx": cx, "width": width, "fill": float(door.mean())}


def plain_wall(jpeg: bytes) -> bool:
    """Featureless bright surface filling the frame — stop, do not walk into it."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return False
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception:
        return False
    arr = np.asarray(img.resize((160, 120), Image.BILINEAR), dtype=np.float32)
    luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    mean = float(luma.mean())
    std = float(luma.std())
    dark = float((luma < 45).mean())
    return mean > 150 and std < 42 and dark < 0.02
