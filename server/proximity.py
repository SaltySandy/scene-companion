"""Deterministic proximity warning: nearest obstacle in the walking path.

Default estimator is a center-corridor pinhole / fill heuristic. It is
pluggable (``ProximityEstimator``) so a depth model can drop in later. No
weights are downloaded; Pillow + numpy only.

The warning uses *predicted* distance at the moment TTS will start:

    predicted = distance - closing_speed * (frame_age + remaining_p95) / 1000

not the distance in the (already old) frame.
"""

from __future__ import annotations

import io
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from PIL import Image

from server.latency import LatencyBook, book as latency_book

log = logging.getLogger("saw.proximity")

# --- tunables (env vars, sensible defaults) ---------------------------------

THRESHOLD_M = float(os.environ.get("SAW_PROXIMITY_THRESHOLD_M", "0.3"))
HYSTERESIS_M = float(os.environ.get("SAW_PROXIMITY_HYSTERESIS_M", "0.35"))
COOLDOWN_MS = int(os.environ.get("SAW_PROXIMITY_COOLDOWN_MS", "2000"))
STALE_MS = int(os.environ.get("SAW_PROXIMITY_STALE_MS", "800"))
ASSUMED_WIDTH_M = float(os.environ.get("SAW_PROXIMITY_OBJECT_WIDTH_M", "0.6"))
FOCAL_FRAC = float(os.environ.get("SAW_PROXIMITY_FOCAL_FRAC", "0.75"))
WALKING_MPS = float(os.environ.get("SAW_WALKING_SPEED_MPS", "1.4"))
CORRIDOR_FRAC = float(os.environ.get("SAW_PROXIMITY_CORRIDOR_FRAC", "0.40"))
FILL_MIN = float(os.environ.get("SAW_PROXIMITY_FILL_MIN", "0.08"))
MAX_PROC_WIDTH = int(os.environ.get("SAW_PROXIMITY_MAX_WIDTH", "320"))
ENABLED = os.environ.get("SAW_PROXIMITY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
WARN_TEXT = os.environ.get("SAW_PROXIMITY_TEXT", "stop, obstacle ahead")
ESTIMATOR_NAME = os.environ.get("SAW_PROXIMITY_ESTIMATOR", "corridor_looming")
MIN_IMAGE = 32
MAX_CLOSING_MPS = float(os.environ.get("SAW_PROXIMITY_MAX_CLOSING_MPS", "4.0"))
SPEED_GAP_S = float(os.environ.get("SAW_PROXIMITY_SPEED_GAP_S", "0.50"))
SPEED_EMA = float(os.environ.get("SAW_PROXIMITY_SPEED_EMA", "0.45"))
LOW_LIGHT_MEAN = float(os.environ.get("SAW_PROXIMITY_LOW_LIGHT", "20"))
NO_SIGNAL_CONTRAST = float(os.environ.get("SAW_PROXIMITY_NO_SIGNAL_CONTRAST", "12"))
STATIONARY_MPS = float(os.environ.get("SAW_PROXIMITY_STATIONARY_MPS", "0.15"))


@dataclass(frozen=True)
class DistanceEstimate:
    distance_m: float
    confidence: float = 1.0
    fill: float = 0.0
    apparent_width_px: float = 0.0
    signal: str = "ok"  # ok | low_light | no_signal
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.signal == "ok"


class ProximityEstimator(Protocol):
    name: str

    def estimate(self, jpeg: bytes) -> DistanceEstimate | None:
        """Same JPEG -> same estimate. Return None if no obstacle / unusable."""


def _decode_gray(jpeg: bytes) -> np.ndarray | None:
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("L")
    except Exception:
        return None
    if img.width < MIN_IMAGE or img.height < MIN_IMAGE:
        return None
    if img.width > MAX_PROC_WIDTH:
        h = max(1, int(img.height * MAX_PROC_WIDTH / img.width))
        img = img.resize((MAX_PROC_WIDTH, h), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32)


def classify_signal(arr: np.ndarray) -> str:
    """Reject a covered lens / featureless dark field. Not an obstacle."""
    mean = float(arr.mean())
    p80 = float(np.percentile(arr, 80))
    p20 = float(np.percentile(arr, 20))
    contrast = p80 - p20
    gx = np.abs(np.diff(arr, axis=1, prepend=arr[:, :1]))
    edge = float((gx > 10.0).mean())
    if mean < LOW_LIGHT_MEAN:
        return "low_light"
    # Featureless and still very dim (not a textured wall that happens to be dark).
    if contrast < NO_SIGNAL_CONTRAST and edge < 0.02 and mean < 28.0:
        return "no_signal"
    return "ok"


def mean_luminance(jpeg: bytes) -> float | None:
    """Average luma 0-255. None if the JPEG cannot be decoded."""
    arr = _decode_gray(jpeg)
    if arr is None:
        # Tiny JPEGs (protocol 1x1 test images) still have a brightness.
        try:
            img = Image.open(io.BytesIO(jpeg)).convert("L")
            return float(np.asarray(img, dtype=np.float32).mean())
        except Exception:
            return None
    return float(arr.mean())


class CorridorLoomingEstimator:
    """Center walking-path occupancy + pinhole size -> meters.

    A dark / high-contrast mass in the middle third of the frame is treated as
    the nearest obstacle. Apparent width maps to distance with a pinhole model
    (``distance = assumed_width * focal / apparent_width``). Corridor fill is
    a second cue so a wall that floods the frame still reads as close.

    Downscales to ``MAX_PROC_WIDTH`` so High quality (1080p) stays in budget.
    """

    name = "corridor_looming"

    def __init__(
        self,
        assumed_width_m: float = ASSUMED_WIDTH_M,
        focal_frac: float = FOCAL_FRAC,
        corridor_frac: float = CORRIDOR_FRAC,
    ) -> None:
        self.assumed_width_m = assumed_width_m
        self.focal_frac = focal_frac
        self.corridor_frac = corridor_frac

    def estimate(self, jpeg: bytes) -> DistanceEstimate | None:
        arr = _decode_gray(jpeg)
        if arr is None:
            return None
        h, w = arr.shape
        x0 = int(w * (0.5 - self.corridor_frac / 2.0))
        x1 = int(w * (0.5 + self.corridor_frac / 2.0))
        x0 = max(0, min(x0, w - 1))
        x1 = max(x0 + 1, min(x1, w))
        # Mid-height walking path: skip sky and the near-ground band.
        y0 = int(h * 0.18)
        y1 = int(h * 0.78)
        corridor = arr[y0:y1, x0:x1]
        if corridor.size == 0:
            return None

        signal = classify_signal(arr)
        if signal != "ok":
            return DistanceEstimate(
                distance_m=0.0,
                confidence=0.0,
                fill=0.0,
                signal=signal,
                details={"mean": float(arr.mean()), "reason": signal},
            )

        # Bright reference: sky / far field. When an object fills the sensor
        # the top strip is dark too — treat that as "already in your face".
        top = arr[: max(2, h // 6)]
        p80 = float(np.percentile(arr, 80))
        p20 = float(np.percentile(arr, 20))
        global_mean = float(arr.mean())
        # Near-black is handled above. A dim but featured frame can still be a wall.
        filled_frame = 30.0 <= global_mean < 95.0 and (p80 - p20) < 40.0
        bg = max(p80, float(np.median(top)))
        if filled_frame:
            bg = max(bg, 180.0)
        gx = np.abs(np.diff(corridor, axis=1, prepend=corridor[:, :1]))
        dark = corridor < (bg - 22.0)
        edged = gx > 14.0
        occupied = dark | edged
        fill = float(occupied.mean())
        corridor_mean = float(corridor.mean())
        if corridor_mean < bg - 28.0:
            # Uniformly dark path: wall or object in your face.
            fill = max(fill, min(0.98, (bg - corridor_mean) / max(bg, 1.0)))
            occupied = occupied | (corridor < (bg - 16.0))
        if filled_frame:
            fill = max(fill, 0.92)
            occupied = np.ones_like(occupied)
        if fill < FILL_MIN:
            return None

        # Apparent width: fraction of corridor columns that are mostly occupied.
        col_fill = occupied.mean(axis=0)
        occupied_cols = col_fill > 0.25
        if occupied_cols.any():
            idx = np.where(occupied_cols)[0]
            apparent = float(idx[-1] - idx[0] + 1)
        else:
            apparent = fill * corridor.shape[1]

        focal = self.focal_frac * w
        apparent = max(apparent, 1.0)
        dist_blob = self.assumed_width_m * focal / apparent
        dist_fill = self.assumed_width_m * focal / max(fill * w, 1.0)
        distance = float(min(dist_blob, dist_fill))
        # Clamp to a sane pedestrian range. A wall that fills the frame is
        # closer than the assumed-object pinhole suggests — fold fill in.
        if fill > 0.85:
            distance = min(distance, 0.28)
        distance = float(min(max(distance, 0.08), 8.0))
        confidence = float(min(1.0, 0.35 + fill))
        return DistanceEstimate(
            distance_m=distance,
            confidence=confidence,
            fill=fill,
            apparent_width_px=apparent,
            details={"dist_blob": dist_blob, "dist_fill": dist_fill, "bg": bg, "w": w, "h": h},
        )


class FixedEstimator:
    """Test double: always returns a supplied distance (or a callable)."""

    name = "fixed"

    def __init__(self, distance_m: float | None = None, fn=None) -> None:
        self.distance_m = distance_m
        self.fn = fn

    def estimate(self, jpeg: bytes) -> DistanceEstimate | None:
        del jpeg
        d = self.fn() if self.fn is not None else self.distance_m
        if d is None:
            return None
        return DistanceEstimate(distance_m=float(d), confidence=1.0)


def make_estimator(name: str | None = None) -> ProximityEstimator:
    chosen = (name or ESTIMATOR_NAME).strip().lower()
    if chosen in {"corridor", "corridor_looming", "looming", "default"}:
        return CorridorLoomingEstimator()
    if chosen in {"none", "off"}:
        return FixedEstimator(distance_m=None)
    # Unknown names fall back to the default so a typo never silently disables
    # the safety path. A better model drops in by implementing the protocol
    # and setting SAW_PROXIMITY_ESTIMATOR (or calling engine.set_estimator).
    log.warning("unknown proximity estimator %r, using corridor_looming", chosen)
    return CorridorLoomingEstimator()


@dataclass
class MotionSample:
    t_s: float
    distance_m: float


@dataclass
class WarningDecision:
    should_warn: bool
    reason: str
    distance_m: float
    closing_mps: float
    predicted_m: float
    lead_ms: float
    frame_age_ms: float
    stale: bool
    text: str = WARN_TEXT


class ProximityEngine:
    """Tracks distance over time, estimates closing speed, decides warnings."""

    def __init__(
        self,
        estimator: ProximityEstimator | None = None,
        latency: LatencyBook | None = None,
        threshold_m: float = THRESHOLD_M,
        hysteresis_m: float = HYSTERESIS_M,
        cooldown_ms: int = COOLDOWN_MS,
        stale_ms: int = STALE_MS,
        enabled: bool = ENABLED,
    ) -> None:
        self.estimator = estimator or make_estimator()
        self.latency = latency if latency is not None else latency_book
        self.threshold_m = threshold_m
        self.hysteresis_m = hysteresis_m
        self.cooldown_ms = cooldown_ms
        self.stale_ms = stale_ms
        self.enabled = enabled
        self.samples: deque[MotionSample] = deque(maxlen=12)
        self.closing_mps = 0.0
        self.in_alarm = False
        self.last_warn_ms = 0
        self.last_estimate: DistanceEstimate | None = None
        self.last_decision: WarningDecision | None = None
        self.last_process_ms = 0.0
        self.processed = 0
        self.skipped_stale = 0
        self.warnings = 0
        self.last_signal = "ok"

    def reset(self) -> None:
        self.samples.clear()
        self.closing_mps = 0.0
        self.in_alarm = False
        self.last_warn_ms = 0
        self.last_estimate = None
        self.last_decision = None
        self.last_process_ms = 0.0
        self.processed = 0
        self.skipped_stale = 0
        self.warnings = 0
        self.last_signal = "ok"

    def set_estimator(self, estimator: ProximityEstimator) -> None:
        self.estimator = estimator
        self.samples.clear()
        self.closing_mps = 0.0

    def _update_speed(self, t_s: float, distance_m: float) -> float:
        if self.samples:
            prev = self.samples[-1]
            dt = t_s - prev.t_s
            if dt < 0:
                self.samples.clear()
            elif dt > SPEED_GAP_S:
                # Quality-switch rebind, reconnect, or a stall — do not slam.
                self.samples.clear()
                self.closing_mps = 0.0
            elif dt >= 0.04:
                instant = -(distance_m - prev.distance_m) / dt
                instant = float(min(max(instant, -1.0), MAX_CLOSING_MPS))
                recent = [-(distance_m - s.distance_m) / max(t_s - s.t_s, 1e-3) for s in list(self.samples)[-3:]]
                recent = [min(max(v, -1.0), MAX_CLOSING_MPS) for v in recent]
                robust = float(np.median(recent + [instant]))
                if len(self.samples) == 1:
                    # First pair: take the measurement. EMA from 0 would lag a slam.
                    self.closing_mps = robust
                else:
                    self.closing_mps = SPEED_EMA * robust + (1.0 - SPEED_EMA) * self.closing_mps
        self.samples.append(MotionSample(t_s=t_s, distance_m=distance_m))
        return self.closing_mps

    def decide(
        self,
        estimate: DistanceEstimate,
        capture_ts: int,
        recv_ms: int,
        now_ms: int | None = None,
        remaining_p95_ms: float | None = None,
    ) -> WarningDecision:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        age = self.latency.frame_age_ms(capture_ts, now, recv_ms)
        if age > self.stale_ms:
            self.skipped_stale += 1
            self.latency.mark_stale()
            decision = WarningDecision(
                should_warn=False,
                reason="stale",
                distance_m=estimate.distance_m,
                closing_mps=self.closing_mps,
                predicted_m=estimate.distance_m,
                lead_ms=age,
                frame_age_ms=age,
                stale=True,
            )
            self.last_decision = decision
            return decision

        t_s = capture_ts / 1000.0
        closing = self._update_speed(t_s, estimate.distance_m)
        remain = self.latency.remaining_p95_ms() if remaining_p95_ms is None else remaining_p95_ms
        lead = age + remain
        predicted = estimate.distance_m - max(0.0, closing) * (lead / 1000.0)
        # A wall that already sits inside the threshold warns even if speed is 0.
        cross = predicted <= self.threshold_m
        cooled = (now - self.last_warn_ms) >= self.cooldown_ms
        if not cross:
            if predicted >= self.threshold_m + self.hysteresis_m:
                self.in_alarm = False
            decision = WarningDecision(
                should_warn=False,
                reason="far",
                distance_m=estimate.distance_m,
                closing_mps=closing,
                predicted_m=predicted,
                lead_ms=lead,
                frame_age_ms=age,
                stale=False,
            )
            self.last_decision = decision
            return decision

        if self.in_alarm and not cooled:
            decision = WarningDecision(
                should_warn=False,
                reason="cooldown",
                distance_m=estimate.distance_m,
                closing_mps=closing,
                predicted_m=predicted,
                lead_ms=lead,
                frame_age_ms=age,
                stale=False,
            )
            self.last_decision = decision
            return decision

        if self.in_alarm and cooled and predicted <= self.threshold_m:
            if closing < STATIONARY_MPS:
                # Already warned; standing still. Don't nag every cooldown.
                decision = WarningDecision(
                    should_warn=False,
                    reason="stationary",
                    distance_m=estimate.distance_m,
                    closing_mps=closing,
                    predicted_m=predicted,
                    lead_ms=lead,
                    frame_age_ms=age,
                    stale=False,
                )
                self.last_decision = decision
                return decision
            # Still closing in after cooldown — repeat once.
        elif self.in_alarm:
            decision = WarningDecision(
                should_warn=False,
                reason="hysteresis",
                distance_m=estimate.distance_m,
                closing_mps=closing,
                predicted_m=predicted,
                lead_ms=lead,
                frame_age_ms=age,
                stale=False,
            )
            self.last_decision = decision
            return decision

        self.in_alarm = True
        self.last_warn_ms = now
        self.warnings += 1
        decision = WarningDecision(
            should_warn=True,
            reason="predicted_threshold",
            distance_m=estimate.distance_m,
            closing_mps=closing,
            predicted_m=predicted,
            lead_ms=lead,
            frame_age_ms=age,
            stale=False,
        )
        self.last_decision = decision
        return decision

    def process_jpeg(
        self,
        jpeg: bytes,
        capture_ts: int,
        recv_ms: int,
        now_ms: int | None = None,
        remaining_p95_ms: float | None = None,
    ) -> WarningDecision | None:
        if not self.enabled:
            return None
        t0 = time.perf_counter()
        estimate = self.estimator.estimate(jpeg)
        self.last_process_ms = (time.perf_counter() - t0) * 1000.0
        self.processed += 1
        self.last_estimate = estimate
        if estimate is None:
            self.last_signal = "empty"
            if self.last_decision and self.last_decision.predicted_m >= self.threshold_m + self.hysteresis_m:
                self.in_alarm = False
            return None
        self.last_signal = estimate.signal
        if not estimate.usable:
            # Covered lens / featureless field is not a wall. Drop motion so
            # a later real scene does not inherit a fake slam.
            self.samples.clear()
            self.closing_mps = 0.0
            self.in_alarm = False
            decision = WarningDecision(
                should_warn=False,
                reason=estimate.signal,
                distance_m=0.0,
                closing_mps=0.0,
                predicted_m=0.0,
                lead_ms=0.0,
                frame_age_ms=0.0,
                stale=False,
            )
            self.last_decision = decision
            return decision
        return self.decide(estimate, capture_ts, recv_ms, now_ms=now_ms, remaining_p95_ms=remaining_p95_ms)

    def snapshot(self) -> dict[str, Any]:
        est = self.last_estimate
        dec = self.last_decision
        return {
            "enabled": self.enabled,
            "estimator": getattr(self.estimator, "name", type(self.estimator).__name__),
            "threshold_m": self.threshold_m,
            "hysteresis_m": self.hysteresis_m,
            "cooldown_ms": self.cooldown_ms,
            "stale_ms": self.stale_ms,
            "last_distance_m": None if est is None or not est.usable else round(est.distance_m, 3),
            "last_fill": None if est is None or not est.usable else round(est.fill, 3),
            "last_closing_mps": round(self.closing_mps, 3),
            "last_predicted_m": None if dec is None else round(dec.predicted_m, 3),
            "last_lead_ms": None if dec is None else round(dec.lead_ms, 1),
            "last_reason": None if dec is None else dec.reason,
            "last_signal": self.last_signal,
            "last_process_ms": round(self.last_process_ms, 2),
            "processed": self.processed,
            "skipped_stale": self.skipped_stale,
            "warnings": self.warnings,
            "in_alarm": self.in_alarm,
        }


engine = ProximityEngine()
