"""AI hook: the proximity sensor runs here.

``on_frame`` is called from the latest-frame worker with only the newest JPEG.
It runs metric depth plus sector analysis in a thread (``asyncio.to_thread``)
and returns a ``ProximityReading``. The hub sends it to the phone as a heartbeat
on every processed frame and applies the staleness gate; this module never
touches the hub.

If the depth model or its dependencies are missing, every reading is
``state="unknown"`` so the phone keeps saying "sensor lost" instead of going
quiet. Set SAW_PROXIMITY=0 to turn the sensor off (for /debug/proximity tests).

You may also return ``Say`` / ``Play`` items (or a list mixing them with the
reading) to talk to the phone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

import sentry_sdk

log = logging.getLogger("saw.ai")

# ---------------------------------------------------------------------------
# Response types the worker understands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Say:
    text: str
    urgent: bool = False


@dataclass(frozen=True)
class Play:
    audio_bytes: bytes
    mime: str = "audio/wav"
    urgent: bool = False


@dataclass(frozen=True)
class ProximityReading:
    """What the sensor saw in one frame. level is after hysteresis; raw_level before."""

    state: str  # "ok" or "unknown"
    level: str | None = None
    raw_level: str | None = None
    levels: dict[str, str] | None = None
    sectors_m: dict[str, float] | None = None
    model: str | None = None


Response = Say | Play | ProximityReading | Sequence[Say | Play | ProximityReading] | None

UNKNOWN = ProximityReading(state="unknown")

# ---------------------------------------------------------------------------
# Proximity sensor
# ---------------------------------------------------------------------------

PROXIMITY_ON = (os.environ.get("SAW_PROXIMITY") or "1").strip() != "0"

_model: Any = None
_model_tried = False
_cfg: Any = None
_tracker: Any = None


def warmup() -> str | None:
    """Load the depth model (blocking; call via to_thread at startup). Returns its name."""
    global _model, _model_tried, _cfg, _tracker
    if _model_tried:
        return getattr(_model, "name", None)
    _model_tried = True
    if not PROXIMITY_ON:
        log.info("proximity sensor off (SAW_PROXIMITY=0)")
        return None
    try:
        from server.ai import depth
        from server.ai.proximity import LevelTracker, ProximityConfig

        _cfg = ProximityConfig.from_env()
        _tracker = LevelTracker()
        _model = depth.load()
    except ImportError as exc:
        log.warning("proximity sensor unavailable: %s (pip install -r server/requirements-ai.txt)", exc)
        _model = None
    return getattr(_model, "name", None)


def _infer(jpeg_bytes: bytes) -> Any:
    """Runs in a worker thread: bytes in, plain data out."""
    from server.ai.proximity import analyze

    return analyze(_model.predict(jpeg_bytes), _cfg)


async def read_proximity(jpeg_bytes: bytes) -> ProximityReading:
    if not _model_tried:
        await asyncio.to_thread(warmup)
    if _model is None:
        return UNKNOWN
    try:
        result = await asyncio.to_thread(_infer, jpeg_bytes)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("depth inference failed")
        sentry_sdk.capture_exception()
        _tracker.reset()
        return UNKNOWN
    from server.ai.proximity import overall

    held = _tracker.update(result.levels)
    return ProximityReading(
        state="ok",
        level=overall(held),
        raw_level=result.level,
        levels=held,
        sectors_m=result.sectors_m,
        model=_model.name,
    )


# ---------------------------------------------------------------------------
# Demo announcement (off by default; tests turn it on)
# ---------------------------------------------------------------------------

ANNOUNCE_EVERY_SEC = float(os.environ.get("SAW_DEMO_ANNOUNCE_SEC", "0"))

_frame_times: deque[float] = deque()
_last_announce = 0.0


def _demo_announce() -> Say | None:
    global _last_announce
    now = time.time()
    _frame_times.append(now)
    while _frame_times and _frame_times[0] < now - 1.0:
        _frame_times.popleft()
    if ANNOUNCE_EVERY_SEC > 0 and now - _last_announce >= ANNOUNCE_EVERY_SEC:
        _last_announce = now
        return Say(f"frame received, {len(_frame_times)} per second")
    return None


async def on_frame(jpeg_bytes: bytes, ts: int) -> Response:
    """Called with the latest JPEG. ``ts`` is the phone capture time (epoch ms, phone clock)."""
    del ts
    out: list[Say | Play | ProximityReading] = []
    if PROXIMITY_ON:
        out.append(await read_proximity(jpeg_bytes))
    announce = _demo_announce()
    if announce:
        out.append(announce)
    return out or None


def reset_demo_state() -> None:
    """Test helper."""
    global _last_announce
    _frame_times.clear()
    _last_announce = 0.0
