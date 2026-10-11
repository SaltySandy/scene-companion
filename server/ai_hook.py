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

# Last depth + analysis. Pointer store only; the viewer render reads it later.
_debug: tuple[Any, Any, Any, str | None] | None = None  # depth, result, cfg, model name

# "What is it" descriptions (server/ai/describe.py): background tasks on the event loop.
_gate: Any = None
_levels: dict[str, str] = {}  # latest held levels, read when a description comes back
_tasks: set[asyncio.Task[None]] = set()


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

    depth = _model.predict(jpeg_bytes)
    result = analyze(depth, _cfg)
    global _debug
    _debug = (depth, result, _cfg, getattr(_model, "name", None))
    return result


def fov_params() -> dict[str, Any]:
    """The ROI / model size the running sensor is actually using. Safe anytime."""
    from server.ai.proximity import ProximityConfig

    cfg = _cfg or ProximityConfig.from_env()
    params: dict[str, Any] = {
        "roi_top": cfg.roi_top,
        "roi_bottom": cfg.roi_bottom,
        "percentile": cfg.percentile,
        "near_m": cfg.near_m,
        "danger_m": cfg.danger_m,
        "depth_height": getattr(_model, "height", None),
        "depth_width": getattr(_model, "width", None),
        "model": getattr(_model, "name", None),
        "mount_rotation": "phone",
        "mount_note": "Phone applies mount_rotation before JPEG encode; frames arrive upright.",
        "proximity_on": PROXIMITY_ON,
        "describe": _describe_status(),
    }
    if _debug is not None:
        _depth, result, _used_cfg, name = _debug
        params["sectors_m"] = dict(result.sectors_m)
        params["levels"] = dict(result.levels)
        params["level"] = result.level
        params["depth_height"] = int(_depth.shape[0])
        params["depth_width"] = int(_depth.shape[1])
        if name:
            params["model"] = name
    return params


def render_fov_jpeg() -> tuple[bytes, dict[str, Any]] | None:
    """Build the FoV JPEG from the last stashed depth. Blocking; call via to_thread."""
    snap = _debug
    if snap is None:
        return None
    depth, result, cfg, name = snap
    from server.ai.fov import render_fov

    return render_fov(depth, result, cfg, model_name=name)


def _describe_status() -> str:
    try:
        from server.ai import describe
    except ImportError:
        return "unavailable"
    if not describe.ENABLED:
        return "off (SAW_DESCRIBE=0)"
    if describe._disabled_reason:
        return f"disabled ({describe._disabled_reason})"
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return "idle (no ANTHROPIC_API_KEY; first obstacle will disable it)"
    return "on"


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
    _maybe_describe(jpeg_bytes, held)
    return ProximityReading(
        state="ok",
        level=overall(held),
        raw_level=result.level,
        levels=held,
        sectors_m=result.sectors_m,
        model=_model.name,
    )


def _maybe_describe(jpeg_bytes: bytes, held: dict[str, str]) -> None:
    """Start a description task when a new obstacle appears. Never awaits it."""
    global _gate, _levels
    prev, _levels = _levels, dict(held)
    try:
        from server.ai import describe
    except ImportError:
        return
    if not describe.available():
        return
    if _gate is None:
        _gate = describe.DescribeGate()
    sector = _gate.check(prev, held, describe.monotonic())
    if sector:
        task = asyncio.create_task(_describe_and_say(jpeg_bytes, sector, describe.monotonic()))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def _describe_and_say(jpeg_bytes: bytes, sector: str, started: float) -> None:
    from server.ai import describe
    from server.hub import say

    try:
        label = await describe.describe(jpeg_bytes, sector)
    finally:
        _gate.done()
    if not label:
        return
    if describe.monotonic() - started > describe.MAX_AGE_S:
        log.info("dropping late description %r", label)
        return
    if _levels.get(sector, "clear") == "clear":
        log.info("dropping description %r: %s sector cleared", label, sector)
        return
    await say(f"{label} {describe.WHERE[sector]}", urgent=False)


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


async def on_transcript(text: str, is_final: bool, ts: int) -> None:
    """Question hook. ``text`` is a partial or final utterance. ``ts`` is capture time in ms.

    The default stub proves the loop: finals are spoken back. Replace this
    with the real assistant. Do not do slow work before the first await if
    you can help it; the caller already runs off the camera thread.
    """
    import logging

    logging.getLogger("saw.transcribe").info("transcript final=%s ts=%s: %s", is_final, ts, text)
    if not is_final or not text.strip():
        return
    from server.event_sinks import sinks
    from server.hub import say

    sinks.log_event("transcript", text.strip(), meta={"ts": ts, "final": True})

    from server.scene_assistant import assistant

    question = text.strip()
    if assistant.enabled and len(question) >= 3:
        # Gemini looks at the current frame; Nimble adds web facts when useful.
        assistant.ask(question)
    else:
        await say(f"You said: {question}")


def reset_demo_state() -> None:
    """Test helper."""
    global _last_announce
    _frame_times.clear()
    _last_announce = 0.0
