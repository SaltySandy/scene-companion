"""Turn a metric depth map into left/center/right distances and levels.

Only a horizontal band of the image is used. A chest camera sees the floor in
the bottom of the frame, and the floor is always "near"; counting it would make
the sensor cry wolf on every step. With the camera ~1.3 m up and level, floor
pixels above 75% of the frame height are at least ~5 m away. The cost: low
obstacles within about a metre of the feet are below the band (and often below
the frame). That is the spotter's job.

Each sector reports a low percentile of its depths, so an obstacle filling a
small part of the sector still counts while single-pixel noise does not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import numpy as np

SECTORS = ("left", "center", "right")
Level = Literal["clear", "near", "danger"]
SEVERITY: dict[str, int] = {"clear": 0, "near": 1, "danger": 2}


@dataclass(frozen=True)
class ProximityConfig:
    roi_top: float = 0.15  # fraction of image height
    roi_bottom: float = 0.75
    percentile: float = 10.0
    near_m: float = 1.6
    danger_m: float = 0.8

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProximityConfig:
        source = env if env is not None else os.environ
        base = cls()

        def num(key: str, default: float) -> float:
            raw = (source.get(key) or "").strip()
            return float(raw) if raw else default

        return cls(
            roi_top=num("SAW_ROI_TOP", base.roi_top),
            roi_bottom=num("SAW_ROI_BOTTOM", base.roi_bottom),
            percentile=num("SAW_SECTOR_PERCENTILE", base.percentile),
            near_m=num("SAW_NEAR_M", base.near_m),
            danger_m=num("SAW_DANGER_M", base.danger_m),
        )


@dataclass(frozen=True)
class Proximity:
    sectors_m: dict[str, float]
    levels: dict[str, Level]
    level: Level


def sector_distances(depth: np.ndarray, cfg: ProximityConfig) -> dict[str, float]:
    """Low-percentile depth per sector. Raises ValueError if a sector has no valid depth."""
    h = depth.shape[0]
    band = depth[int(h * cfg.roi_top) : max(int(h * cfg.roi_bottom), int(h * cfg.roi_top) + 1)]
    out: dict[str, float] = {}
    for name, part in zip(SECTORS, np.array_split(band, 3, axis=1)):
        valid = part[np.isfinite(part) & (part > 0)]
        if valid.size == 0:
            raise ValueError(f"no valid depth in {name} sector")
        out[name] = round(float(np.percentile(valid, cfg.percentile)), 2)
    return out


def level_for(distance_m: float, cfg: ProximityConfig) -> Level:
    if distance_m < cfg.danger_m:
        return "danger"
    if distance_m < cfg.near_m:
        return "near"
    return "clear"


def overall(levels: dict[str, Level]) -> Level:
    """The center sector decides; a side only counts when it is danger.

    Walking a corridor puts walls "near" on both sides all the time, so side
    "near" must not raise the alarm. A side close enough to hit still does.
    """
    level = levels["center"]
    if any(levels[s] == "danger" for s in ("left", "right")):
        level = "danger"
    return level


def analyze(depth: np.ndarray, cfg: ProximityConfig | None = None) -> Proximity:
    cfg = cfg or ProximityConfig()
    sectors = sector_distances(depth, cfg)
    levels = {name: level_for(d, cfg) for name, d in sectors.items()}
    return Proximity(sectors_m=sectors, levels=levels, level=overall(levels))


class LevelTracker:
    """Per-sector hysteresis: escalate at once, de-escalate after `hold` calmer frames.

    A warning is never delayed. Dropping back (danger -> near -> clear) waits
    for `hold` consecutive frames at the calmer level, so a reading that
    hovers at a threshold does not make the cue flicker.
    """

    def __init__(self, hold: int = 2) -> None:
        self.hold = hold
        self._held: dict[str, Level] = {}
        self._calmer: dict[str, int] = {}

    def reset(self) -> None:
        self._held.clear()
        self._calmer.clear()

    def update(self, raw: dict[str, Level]) -> dict[str, Level]:
        for sector, level in raw.items():
            held = self._held.get(sector)
            if held is None or SEVERITY[level] >= SEVERITY[held]:
                self._held[sector] = level
                self._calmer[sector] = 0
            else:
                self._calmer[sector] = self._calmer.get(sector, 0) + 1
                if self._calmer[sector] >= self.hold:
                    self._held[sector] = level
                    self._calmer[sector] = 0
        return {s: self._held[s] for s in raw}
