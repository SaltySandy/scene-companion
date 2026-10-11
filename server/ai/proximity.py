"""Turn a metric depth map into left/center/right distances and levels.

Two detectors feed each sector, and the nearer one wins:

1. The band (roi_top..roi_bottom): walls, doors, people at body height. The
   bottom of the frame is left out because the floor there is always "near".

2. Floor obstacles (floor_top..bottom): things standing on the floor, such as
   chairs, bags, and low tables. Below the horizon the floor is the farthest
   surface along each ray (anything in the way is closer), and for a flat
   floor inverse depth is a plane in pixel coordinates, 1/Z = a*col + b*row + c,
   whatever the camera's pitch and roll. Each frame fits that plane with RANSAC
   on sampled pixels, preferring the farthest plausible surface, and flags
   pixels clearly closer than it. No fit (facing a wall, no floor in view)
   means no floor obstacles; the band still works. The bottom few rows are
   skipped: the depth model bends there and reads the floor as too near.

Each sector reports a low percentile of its depths, so an obstacle filling a
small part of the sector still counts while single-pixel noise does not.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

SECTORS = ("left", "center", "right")
Level = Literal["clear", "near", "danger"]
SEVERITY: dict[str, int] = {"clear": 0, "near": 1, "danger": 2}

FLOOR_BOTTOM = 0.96  # the model's depth bends in the last rows of the frame
FLOOR_TOL = 0.10  # RANSAC inlier: inverse depth within 10% of the plane
FLOOR_MAX_M = 8.0  # pixels whose floor is farther than this are not searched
FLOOR_MIN_INLIERS = 0.15  # share of sampled pixels that must lie on the floor
FLOOR_MAX_ROLL = 0.5  # |a| <= 0.5 * b: about 27 degrees of roll; side walls fail this
FLOOR_STRIDE = 3  # sample every 3rd pixel each way for the fit
FLOOR_ITERS = 120
FLOOR_BEHIND_WEIGHT = 5.0  # each pixel behind a candidate floor cancels this many inliers
FLOOR_MIN_GROWTH = 1.25  # floor inverse depth must rise >= 25% from top to bottom of the region


@dataclass(frozen=True)
class ProximityConfig:
    roi_top: float = 0.15  # fraction of image height
    roi_bottom: float = 0.75
    percentile: float = 10.0
    near_m: float = 1.6
    danger_m: float = 0.8
    floor: bool = True  # detect obstacles standing on the floor
    floor_top: float = 0.55  # floor search starts here (fraction of height)
    floor_margin: float = 0.2  # obstacle if inverse depth exceeds the floor's by 20%
    floor_min_frac: float = 0.02  # ignore floor blobs under 2% of the sector's floor area

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
            floor=(source.get("SAW_FLOOR") or "1").strip() != "0",
            floor_top=num("SAW_FLOOR_TOP", base.floor_top),
            floor_margin=num("SAW_FLOOR_MARGIN", base.floor_margin),
            floor_min_frac=num("SAW_FLOOR_MIN_FRAC", base.floor_min_frac),
        )


@dataclass(frozen=True)
class FloorFit:
    """Floor plane in inverse depth: 1/Z = a * col + b * row + c, for rows top..bottom."""

    top: int
    bottom: int
    a: float
    b: float
    c: float
    inlier_frac: float

    def floor_inv(self, width: int) -> np.ndarray:
        """Expected floor inverse depth for every pixel in rows top..bottom."""
        rows = np.arange(self.top, self.bottom, dtype=np.float64)[:, None]
        cols = np.arange(width, dtype=np.float64)[None, :]
        return self.a * cols + self.b * rows + self.c


@dataclass(frozen=True)
class Proximity:
    sectors_m: dict[str, float]  # what the walker is warned about: nearer of band and floor
    levels: dict[str, Level]
    level: Level
    band_m: dict[str, float] | None = None
    floor_m: dict[str, float | None] | None = None  # None: no floor obstacle in that sector
    floor_fit: FloorFit | None = None
    floor_mask: np.ndarray | None = field(default=None, repr=False, compare=False)


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


def _plane_ok(a, b, c, top: int, bottom: int, width: int):
    """Floor-shaped: nearer toward the bottom, not rolled like a side wall, and in front.

    Works on scalars or arrays of candidate planes. A wall straight ahead has
    nearly constant inverse depth down the frame, so it fails the growth test.
    """
    mid = width / 2
    at_top = a * mid + b * top + c
    at_bottom = a * mid + b * bottom + c
    return (b > 0) & (np.abs(a) <= FLOOR_MAX_ROLL * b) & (at_top > 0) & (at_bottom >= FLOOR_MIN_GROWTH * at_top)


def _solve3(m: np.ndarray, rhs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Batched 3x3 solve by Cramer's rule. Returns (solutions (k, 3), solvable mask (k,)).

    Plain elementwise numpy on purpose: BLAS/LAPACK threads busy-wait after each call
    and slow the next depth inference on the same cores. These systems are tiny.
    """

    def det(x: np.ndarray) -> np.ndarray:
        return (
            x[:, 0, 0] * (x[:, 1, 1] * x[:, 2, 2] - x[:, 1, 2] * x[:, 2, 1])
            - x[:, 0, 1] * (x[:, 1, 0] * x[:, 2, 2] - x[:, 1, 2] * x[:, 2, 0])
            + x[:, 0, 2] * (x[:, 1, 0] * x[:, 2, 1] - x[:, 1, 1] * x[:, 2, 0])
        )

    d = det(m)
    ok = np.abs(d) > 1e-12
    out = np.zeros((m.shape[0], 3))
    for col in range(3):
        swapped = m.copy()
        swapped[:, :, col] = rhs
        out[:, col] = det(swapped) / np.where(ok, d, 1.0)
    return out, ok


def fit_floor(depth: np.ndarray, cfg: ProximityConfig) -> FloorFit | None:
    """Fit the floor plane in the lower part of the frame, or None if there is no floor."""
    h, w = depth.shape
    top = min(int(h * cfg.floor_top), h - 2)
    bottom = max(int(h * FLOOR_BOTTOM), top + 2)
    rows, cols = np.mgrid[top:bottom:FLOOR_STRIDE, 0:w:FLOOR_STRIDE]
    z = depth[rows, cols]
    ok = np.isfinite(z) & (z > 0)
    if ok.sum() < 30:
        return None
    u, v, y = cols[ok].astype(np.float64), rows[ok].astype(np.float64), 1.0 / z[ok]
    # Score every candidate plane at once: (pixels x candidates), elementwise.
    rng = np.random.default_rng(0)  # deterministic: same frame, same answer
    picks = np.stack([rng.choice(y.size, 3, replace=False) for _ in range(FLOOR_ITERS)])
    systems = np.stack([u[picks], v[picks], np.ones(picks.shape)], axis=-1)  # (k, 3, 3)
    planes, solvable = _solve3(systems, y[picks])
    planes = planes[solvable]
    planes = planes[_plane_ok(planes[:, 0], planes[:, 1], planes[:, 2], top, bottom, w)]
    if planes.size == 0:
        return None
    pred = u[:, None] * planes[:, 0] + v[:, None] * planes[:, 1] + planes[:, 2]  # (n, k)
    rel = (y[:, None] - pred) / np.maximum(np.abs(pred), 1e-6)
    inliers = np.abs(rel) <= FLOOR_TOL
    # The floor is the farthest surface: pixels well behind a candidate count against it,
    # so a chair seat or the front of an armchair cannot pass for the floor.
    behind = rel < -2 * FLOOR_TOL
    score = inliers.sum(axis=0) - FLOOR_BEHIND_WEIGHT * behind.sum(axis=0)
    best = int(np.argmax(score))
    best_inliers = inliers[:, best] if np.isfinite(score[best]) else None
    if best_inliers is None or best_inliers.sum() < FLOOR_MIN_INLIERS * y.size:
        return None
    # Least-squares refit on the inliers via the 3x3 normal equations.
    iu, iv, iy = u[best_inliers], v[best_inliers], y[best_inliers]
    n = float(iu.size)
    normal = np.array([[[iu @ iu, iu @ iv, iu.sum()], [iu @ iv, iv @ iv, iv.sum()], [iu.sum(), iv.sum(), n]]])
    solution, solved = _solve3(normal, np.array([[iu @ iy, iv @ iy, iy.sum()]]))
    if not solved[0]:
        return None
    a, b, c = solution[0]
    if not _plane_ok(a, b, c, top, bottom, w):
        return None
    return FloorFit(top, bottom, float(a), float(b), float(c), float(best_inliers.mean()))


def floor_obstacle_mask(depth: np.ndarray, fit: FloorFit, cfg: ProximityConfig) -> np.ndarray:
    """Pixels clearly closer than the floor along their ray: things standing on the floor."""
    floor_inv = fit.floor_inv(depth.shape[1])
    region = depth[fit.top : fit.bottom]
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = np.where(np.isfinite(region) & (region > 0), 1.0 / region, 0.0)
    searchable = floor_inv > 1.0 / FLOOR_MAX_M  # skip what lies near the horizon
    mask = np.zeros(depth.shape, dtype=bool)
    mask[fit.top : fit.bottom] = searchable & (inv > floor_inv * (1.0 + cfg.floor_margin))
    return mask


def floor_distances(
    depth: np.ndarray, mask: np.ndarray, fit: FloorFit, cfg: ProximityConfig
) -> dict[str, float | None]:
    """Low-percentile depth of floor-obstacle pixels per sector, or None if too few."""
    out: dict[str, float | None] = {}
    region, region_mask = depth[fit.top : fit.bottom], mask[fit.top : fit.bottom]
    for name, part, part_mask in zip(
        SECTORS, np.array_split(region, 3, axis=1), np.array_split(region_mask, 3, axis=1)
    ):
        hits = part[part_mask]
        if hits.size < max(1, cfg.floor_min_frac * part.size):
            out[name] = None
        else:
            out[name] = round(float(np.percentile(hits, cfg.percentile)), 2)
    return out


def combined_distances(depth: np.ndarray, cfg: ProximityConfig) -> Proximity:
    """Band and floor distances per sector; levels are filled in by analyze()."""
    band = sector_distances(depth, cfg)
    fit = fit_floor(depth, cfg) if cfg.floor else None
    mask = floor_obstacle_mask(depth, fit, cfg) if fit else None
    floor = floor_distances(depth, mask, fit, cfg) if fit and mask is not None else None
    sectors = {
        s: min(band[s], floor[s]) if floor and floor[s] is not None else band[s] for s in SECTORS
    }
    return Proximity(sectors, {}, "clear", band_m=band, floor_m=floor, floor_fit=fit, floor_mask=mask)


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
    d = combined_distances(depth, cfg)
    levels = {name: level_for(dist, cfg) for name, dist in d.sectors_m.items()}
    return Proximity(
        sectors_m=d.sectors_m,
        levels=levels,
        level=overall(levels),
        band_m=d.band_m,
        floor_m=d.floor_m,
        floor_fit=d.floor_fit,
        floor_mask=d.floor_mask,
    )


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
