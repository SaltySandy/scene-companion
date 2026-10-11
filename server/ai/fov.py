"""Viewer-only picture of the depth band the proximity sensor actually uses.

The heartbeat path never calls this. The hub asks for a JPEG when a viewer is
connected, at a few Hz, on a thread that is not the inference thread.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from server.ai.proximity import SECTORS, Proximity, ProximityConfig

# Far end of the colour scale (metres). Closer than this is warmer.
VMAX_M = 5.0
FLOOR_OUTLINE = (61, 214, 214)
FLOOR_HIT = np.array((255, 0, 255), dtype=np.float32)  # pixels standing on the floor
LEVEL_RGB = {
    "clear": (61, 214, 140),
    "near": (245, 197, 66),
    "danger": (255, 107, 107),
}


def sector_edges(width: int) -> list[int]:
    """Column starts (and a final end) matching ``np.array_split(..., 3)``."""
    edges = [0]
    for part in np.array_split(np.arange(width), 3):
        edges.append(int(part[-1]) + 1)
    return edges


def colorize_depth(depth: np.ndarray, vmax: float = VMAX_M) -> np.ndarray:
    """Near = red/yellow, far = blue. Invalid pixels are dark."""
    finite = np.isfinite(depth) & (depth > 0)
    t = np.zeros(depth.shape, dtype=np.float32)
    t[finite] = np.clip(depth[finite], 0.0, vmax) / vmax
    rgb = np.zeros(depth.shape + (3,), dtype=np.uint8)
    # t=0 (touching) -> warm; t=1 (vmax) -> cool
    rgb[..., 0] = np.clip(255 * (1.0 - t) + 40 * t, 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(255 * (1.0 - np.abs(t - 0.35) * 1.6), 0, 255).astype(np.uint8)
    rgb[..., 2] = np.clip(40 + 200 * t, 0, 255).astype(np.uint8)
    rgb[~finite] = (20, 24, 32)
    return rgb


def render_fov(
    depth: np.ndarray,
    result: Proximity,
    cfg: ProximityConfig,
    *,
    model_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Colourised depth JPEG with the live ROI band, sectors, and labels.

    ``cfg`` and ``depth.shape`` must be the values the sensor just used.
    """
    h, w = int(depth.shape[0]), int(depth.shape[1])
    y0 = int(h * cfg.roi_top)
    y1 = max(int(h * cfg.roi_bottom), y0 + 1)
    edges = sector_edges(w)

    fit = result.floor_fit
    rgb = colorize_depth(depth)
    outside = np.ones((h, w, 1), dtype=np.float32)
    outside[y0:y1, :] = 0.0
    if fit is not None:
        outside[fit.top : fit.bottom, :] = 0.0  # the floor search region is live too
    shaded = rgb.astype(np.float32) * (1.0 - 0.72 * outside)
    if result.floor_mask is not None:
        hit = result.floor_mask
        shaded[hit] = 0.35 * shaded[hit] + 0.65 * FLOOR_HIT
    rgb = shaded.astype(np.uint8)

    # Draw at a readable size; the model input is typically 350x266.
    scale = 2 if max(h, w) < 500 else 1
    img = Image.fromarray(rgb, mode="RGB").resize((w * scale, h * scale), Image.NEAREST)
    canvas_h = img.height + 56
    canvas = Image.new("RGB", (img.width, canvas_h), (11, 13, 16))
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    def sx(x: float) -> int:
        return int(round(x * scale))

    def sy(y: float) -> int:
        return int(round(y * scale))

    if fit is not None:
        draw.rectangle([sx(0), sy(fit.top), sx(w) - 1, sy(fit.bottom) - 1], outline=FLOOR_OUTLINE, width=1)
    draw.rectangle([sx(0), sy(y0), sx(w) - 1, sy(y1) - 1], outline=(245, 197, 66), width=2)
    for x in edges[1:-1]:
        draw.line([(sx(x), sy(y0)), (sx(x), sy(y1) - 1)], fill=(232, 237, 244), width=1)

    for i, name in enumerate(SECTORS):
        x0, x1 = edges[i], edges[i + 1]
        dist = result.sectors_m.get(name)
        level = result.levels.get(name, "?")
        colour = LEVEL_RGB.get(level, (232, 237, 244))
        label = f"{name} {dist:.2f}m {level}" if isinstance(dist, (int, float)) else f"{name} {level}"
        tx = sx((x0 + x1) / 2)
        ty = sy((y0 + y1) / 2)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        px, py = tx - tw // 2, ty - th // 2
        draw.rectangle([px - 3, py - 2, px + tw + 3, py + th + 2], fill=(11, 13, 16))
        draw.text((px, py), label, fill=colour, font=font)

    note = (
        f"depth {h}x{w}  ROI {cfg.roi_top:g}-{cfg.roi_bottom:g}  "
        f"p{cfg.percentile:g}  near {cfg.near_m:g}m / danger {cfg.danger_m:g}m"
    )
    if model_name:
        note = f"{model_name}  {note}"
    draw.text((8, img.height + 8), note, fill=(147, 160, 179), font=font)
    if not cfg.floor:
        floor_note = "floor obstacles: off (SAW_FLOOR=0)"
    elif fit is None:
        floor_note = "floor obstacles: no floor in view, band only"
    else:
        floor_note = (
            f"floor obstacles (magenta): margin {cfg.floor_margin:g}, "
            f"{fit.inlier_frac:.0%} of region on the floor plane"
        )
    draw.text((8, img.height + 28), floor_note, fill=FLOOR_OUTLINE, font=font)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=70)
    meta: dict[str, Any] = {
        "depth_height": h,
        "depth_width": w,
        "roi_top": cfg.roi_top,
        "roi_bottom": cfg.roi_bottom,
        "percentile": cfg.percentile,
        "near_m": cfg.near_m,
        "danger_m": cfg.danger_m,
        "sectors_m": dict(result.sectors_m),
        "levels": dict(result.levels),
        "level": result.level,
        "model": model_name,
        "mount_rotation": "phone",
        "sector_edges": edges,
        "floor": cfg.floor,
        "floor_margin": cfg.floor_margin,
        "floor_top": fit.top if fit else None,
        "floor_bottom": fit.bottom if fit else None,
        "floor_inliers": round(fit.inlier_frac, 3) if fit else None,
        "floor_m": dict(result.floor_m) if result.floor_m else None,
        "band_m": dict(result.band_m) if result.band_m else None,
    }
    if extra:
        meta.update(extra)
    return buf.getvalue(), meta
