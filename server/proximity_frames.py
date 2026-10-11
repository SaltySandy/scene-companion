"""Synthetic JPEG frames with a known obstacle distance (pinhole model).

Used by the simulation harness and unit tests so warning timing has
ground truth. Matches ``CorridorLoomingEstimator``'s assumed width / focal.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

from server.proximity import ASSUMED_WIDTH_M, FOCAL_FRAC


def apparent_width_px(distance_m: float, width: int, assumed_width_m: float = ASSUMED_WIDTH_M) -> int:
    focal = FOCAL_FRAC * width
    return max(2, int(round(assumed_width_m * focal / max(distance_m, 0.05))))


def render_obstacle(
    distance_m: float,
    width: int = 160,
    height: int = 120,
    *,
    extras: list[tuple[float, float]] | None = None,
    sky: tuple[int, int, int] = (190, 196, 205),
    body: tuple[int, int, int] = (36, 36, 40),
    quality: int = 80,
) -> bytes:
    """Dark rectangle in the walking corridor. Size scales as 1/distance.

    ``extras`` is a list of ``(distance_m, x_frac)`` extra objects (multiple
    obstacles). The estimator should report the nearest (largest) one.
    """
    img = Image.new("RGB", (width, height), sky)
    draw = ImageDraw.Draw(img)
    # Ground band so an empty scene is not a flat field.
    draw.rectangle((0, int(height * 0.72), width, height), fill=(150, 148, 140))

    def _draw_one(dist: float, x_frac: float, color: tuple[int, int, int]) -> None:
        aw = min(width - 2, apparent_width_px(dist, width))
        ah = min(height - 2, int(aw * 1.6))
        cx = int(width * x_frac)
        x0 = max(0, cx - aw // 2)
        x1 = min(width - 1, x0 + aw)
        y1 = int(height * 0.92)
        y0 = max(0, y1 - ah)
        draw.rectangle((x0, y0, x1, y1), fill=color)

    _draw_one(distance_m, 0.50, body)
    if extras:
        for dist, x_frac in extras:
            _draw_one(dist, x_frac, (50, 44, 40))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def render_empty(width: int = 160, height: int = 120) -> bytes:
    """Open corridor: sky + light ground, no obstacle blob."""
    img = Image.new("RGB", (width, height), (190, 196, 205))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, int(height * 0.78), width, height), fill=(168, 166, 158))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def render_dark(width: int = 160, height: int = 120, *, value: int = 6) -> bytes:
    """Near-black covered-lens frame (the field false-positive)."""
    v = max(0, min(255, int(value)))
    img = Image.new("RGB", (width, height), (v, v, v))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def render_white_flash(width: int = 160, height: int = 120) -> bytes:
    img = Image.new("RGB", (width, height), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def render_wall(distance_m: float, width: int = 160, height: int = 120) -> bytes:
    """A surface that fills more of the frame as you get closer."""
    fill = min(0.98, 0.6 / max(distance_m, 0.08))
    img = Image.new("RGB", (width, height), (200, 202, 208))
    draw = ImageDraw.Draw(img)
    band = int(width * min(1.0, fill))
    x0 = (width - band) // 2
    draw.rectangle((x0, 0, x0 + band, height), fill=(42, 40, 38))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@dataclass(frozen=True)
class ApproachFrame:
    jpeg: bytes
    distance_m: float
    t_scene_s: float


def approach_sequence(
    start_m: float = 2.4,
    end_m: float = 0.12,
    speed_mps: float = 1.4,
    fps: float = 8.0,
    width: int = 160,
    height: int = 120,
    kind: str = "object",
) -> list[ApproachFrame]:
    """Evenly sampled walk toward an object. ``t_scene_s`` is scene time."""
    dt = 1.0 / max(fps, 0.1)
    frames: list[ApproachFrame] = []
    t = 0.0
    d = start_m
    renderer = render_wall if kind == "wall" else render_obstacle
    while d >= end_m:
        frames.append(ApproachFrame(jpeg=renderer(d, width, height), distance_m=d, t_scene_s=t))
        t += dt
        d -= speed_mps * dt
    return frames
