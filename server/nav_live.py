"""Live navigation path: toggle, latest-frame worker, rate-limited speech.

Kept out of ``ai_hook`` / ``app`` so Zo's Gemini wall-detector merge only
has to coexist with a couple of one-liners.

``note_frame`` is drop-don't-queue: it overwrites the latest JPEG and starts
a worker only if none is running. The worker analyzes in a thread, then
speaks through ``hub.say``. Proximity danger always wins: nav stays quiet
unless the utterance is an urgent Stop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from server.nav import NavResult, analyze, default_navigator, nav_enabled, set_nav_enabled

log = logging.getLogger("saw.nav")

SAY_EVERY_SEC = float(os.environ.get("SAW_NAV_SAY_SEC") or 2.5)
STOP_EVERY_SEC = float(os.environ.get("SAW_NAV_STOP_SEC") or 1.6)
VLM_EVERY_SEC = float(os.environ.get("SAW_NAV_VLM_SEC") or 8.0)

_latest: tuple[bytes, Any] | None = None
_task: asyncio.Task[None] | None = None
_vlm_task: asyncio.Task[None] | None = None
_last_say = 0.0
_last_text = ""
_last_state: dict[str, Any] = {"enabled": False, "boxes": [], "heading": "none"}
_last_vlm = 0.0


def banner() -> str:
    on = nav_enabled()
    from server.ai import yolo

    yolo_s = yolo.model_path().name if yolo.available() else "no YOLO (color fallback)"
    return f"{'ON' if on else 'OFF'}  navigator={_navigator_name()}  {yolo_s}  (SAW_NAV=1 or POST /nav)"


def _navigator_name() -> str:
    try:
        return default_navigator().name
    except Exception:
        return os.environ.get("SAW_NAVIGATOR") or "exit"


def status() -> dict[str, Any]:
    state = dict(_last_state)
    state["enabled"] = nav_enabled()
    state["navigator"] = _navigator_name()
    try:
        from server.ai import yolo

        state["yolo"] = yolo.available()
        state["yolo_path"] = str(yolo.model_path()) if yolo.available() else None
    except Exception:
        state["yolo"] = False
    return state


def set_enabled(on: bool) -> dict[str, Any]:
    set_nav_enabled(on)
    _last_state["enabled"] = on
    log.info("navigation %s", "on" if on else "off")
    return status()


def note_frame(jpeg: bytes, reading: Any = None) -> None:
    """Called from ``on_frame``. Never blocks. No-op when nav is off."""
    global _latest, _task
    if not nav_enabled():
        return
    _latest = (jpeg, reading)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is None or _task.done():
        _task = loop.create_task(_worker())


async def _worker() -> None:
    global _latest, _task
    while nav_enabled() and _latest is not None:
        jpeg, reading = _latest
        t0 = time.perf_counter()
        try:
            _apply_context(reading)
            result = await asyncio.to_thread(analyze, jpeg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nav analyze failed")
            return
        ms = (time.perf_counter() - t0) * 1000.0
        _publish(result, ms)
        stale = _latest is not None and _latest[0] is not jpeg
        if not stale:
            await _maybe_say(result, reading)
            _maybe_vlm(jpeg, result)
        # Drop-don't-queue: if a newer frame arrived, loop once more; else stop.
        if _latest is None or _latest[0] is jpeg:
            return


def _apply_context(reading: Any) -> None:
    nav = default_navigator()
    setter = getattr(nav, "set_context", None)
    if setter is None:
        return
    prox = None
    if reading is not None and getattr(reading, "state", None) == "ok":
        prox = {
            "level": getattr(reading, "level", None),
            "levels": getattr(reading, "levels", None),
            "sectors_m": getattr(reading, "sectors_m", None),
        }
    depth = None
    try:
        from server import ai_hook

        snap = getattr(ai_hook, "_debug", None)
        if snap is not None:
            depth = snap[0]
    except Exception:
        depth = None
    setter(proximity=prox, depth=depth)


def _publish(result: NavResult, ms: float) -> None:
    global _last_state
    _last_state = {
        "enabled": True,
        "exit_visible": result.exit.visible,
        "exit_bearing": result.exit.bearing,
        "exit_distance": result.exit.distance,
        "heading": result.heading,
        "should_stop": result.should_stop,
        "arrived": result.arrived,
        "utterance": result.utterance,
        "boxes": list(result.boxes),
        "latency_ms": result.latency_ms if result.latency_ms is not None else round(ms, 1),
        "obstacle_ahead": result.obstacle_ahead,
    }


def _danger(reading: Any) -> bool:
    if reading is None:
        return False
    if getattr(reading, "level", None) == "danger":
        return True
    levels = getattr(reading, "levels", None) or {}
    return levels.get("center") == "danger"


async def _maybe_say(result: NavResult, reading: Any) -> None:
    global _last_say, _last_text
    text = (result.utterance or "").strip()
    if not text:
        return
    now = time.monotonic()
    urgent = bool(result.should_stop or result.arrived)
    if _danger(reading) and not urgent:
        # Proximity tones own the ear. Don't narrate a heading over a danger beep.
        return
    gap = STOP_EVERY_SEC if urgent else SAY_EVERY_SEC
    if text == _last_text and now - _last_say < gap:
        return
    if now - _last_say < gap and not urgent:
        return
    try:
        from server.hub import say
    except Exception:
        return
    _last_say = now
    _last_text = text
    await say(text, urgent=urgent)


def _maybe_vlm(jpeg: bytes, result: NavResult) -> None:
    """Occasional non-blocking confirmation when the EXIT heuristic is unsure."""
    global _vlm_task, _last_vlm
    if result.exit.visible == "yes":
        return
    now = time.monotonic()
    if now - _last_vlm < VLM_EVERY_SEC:
        return
    if _vlm_task is not None and not _vlm_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _last_vlm = now
    _vlm_task = loop.create_task(_confirm_exit(jpeg))


async def _confirm_exit(jpeg: bytes) -> None:
    try:
        answer = await asyncio.to_thread(_vlm_exit, jpeg)
    except Exception:
        log.debug("exit VLM confirm failed", exc_info=True)
        return
    if not answer:
        return
    nav = default_navigator()
    apply = getattr(nav, "accept_vlm", None)
    if apply:
        apply(answer)
    log.info("exit VLM confirm: %s", answer)


def _vlm_exit(jpeg: bytes) -> dict[str, Any] | None:
    """Best-effort: Anthropic describe plumbing, then Gemini if a key is present."""
    schema = {
        "type": "object",
        "properties": {
            "exit_visible": {"type": "boolean"},
            "bearing": {"type": "string"},
        },
        "required": ["exit_visible"],
        "additionalProperties": True,
    }
    prompt = (
        "This is a chest-camera photo. Is there a red or illuminated EXIT sign "
        "in view? Reply JSON: exit_visible (boolean), bearing "
        "(left|center|right|none). An open doorway without a sign is not an exit."
    )
    try:
        from server.ai import describe, vlm

        if describe.available() and describe._client is not None:
            answer = vlm.ask_json(describe._client, jpeg, prompt, schema, effort="low")
            if answer:
                return answer
    except Exception:
        pass
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        return None
    try:
        import base64
        import json
        import urllib.request

        model = os.environ.get("SAW_GEMINI_MODEL") or "gemini-2.0-flash"
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt + " Return only JSON."},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.standard_b64encode(jpeg).decode("ascii"),
                            }
                        },
                    ]
                }
            ]
        }
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def reset() -> None:
    global _latest, _last_say, _last_text, _last_state, _last_vlm
    _latest = None
    _last_say = 0.0
    _last_text = ""
    _last_vlm = 0.0
    _last_state = {"enabled": nav_enabled(), "boxes": [], "heading": "none"}
