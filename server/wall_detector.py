"""Wall-proximity detector powered by the Gemini Live API.

How it works
------------
* ``submit(jpeg, ts)`` is called for every camera frame (cheap: it only keeps
  the newest JPEG).
* A background task holds one Gemini Live session open. About once a second
  (the Live API's max video rate is 1 fps) it sends the newest frame plus a
  short "CHECK" text turn. The model answers by calling the
  ``report_wall_proximity`` tool with ``clear`` / ``approaching`` / ``too_close``.
* The verdict is debounced and turned into speech on the phone through
  ``server.hub.say``. "too_close" is sent with ``urgent=True`` so it
  interrupts whatever the phone is currently saying.
* Sessions are reconnected automatically (on errors, ``go_away`` and every
  few minutes) so the detector keeps running on a long walk.

Environment variables (all optional except the key)
---------------------------------------------------
GEMINI_API_KEY            required; detector stays off without it
SAW_WALL_DETECTOR         "1" to switch it on. OFF by default: the team's depth model
                          (server/ai/proximity.py) is the proximity sensor; this is a backup.
SAW_GEMINI_LIVE_MODEL     default ``gemini-3.8-live``
SAW_WALL_INTERVAL_SEC     seconds between checks (default 1.0, min 1.0)
SAW_WALL_ALERT_COOLDOWN   seconds between repeated "back up" alerts (default 2.5)
SAW_WALL_CONFIRM          consecutive too_close verdicts before alerting (default 1)
SAW_WALL_SPEAK_APPROACH   "1" (default) to also say "Wall ahead, slow down"
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("saw.wall")

CLEAR = "clear"
APPROACHING = "approaching"
TOO_CLOSE = "too_close"
LEVELS = (CLEAR, APPROACHING, TOO_CLOSE)

TOOL_NAME = "report_wall_proximity"

SYSTEM_PROMPT = """\
You are the obstacle sensor for a blind pedestrian. Images come from a phone \
camera held at chest height, pointing the direction the person is walking.

Every time you receive the text CHECK, look ONLY at the most recent image and \
call the tool report_wall_proximity exactly once. Do not speak, do not explain, \
do not describe the scene. Never respond with audio or text, only the tool call.

Judge whether a wall, door, pillar, large furniture, or other large flat \
vertical surface is directly ahead in the walking path:
- too_close: the surface fills most of the frame, little or no floor is \
  visible in front of it, or it is roughly within one step (under ~1 metre). \
  The person must stop and back up.
- approaching: a surface is clearly ahead in the path and roughly 1 to 3 \
  metres away (a few steps).
- clear: nothing large is directly ahead within about 3 metres, or the path \
  continues (open corridor, open space, doorway that is open).

If the image is very dark, blurry or blocked by something touching the lens, \
report too_close only if it looks like a surface right in front of the camera; \
otherwise report clear with low confidence.
"""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass
class Verdict:
    level: str
    confidence: float = 0.5
    note: str = ""
    at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Alert policy (pure logic, unit-tested without the network)
# ---------------------------------------------------------------------------


@dataclass
class AlertPolicy:
    """Turns a stream of verdicts into a few well-timed spoken alerts."""

    cooldown_sec: float = 2.5
    approach_cooldown_sec: float = 6.0
    confirm: int = 1
    speak_approach: bool = True
    min_confidence: float = 0.35

    _too_close_streak: int = 0
    _last_alert_at: float = float("-inf")
    _last_approach_at: float = float("-inf")
    _alerting: bool = False

    def update(self, v: Verdict, now: float | None = None) -> tuple[str, bool] | None:
        """Return ``(text, urgent)`` to speak, or ``None``."""
        now = time.time() if now is None else now
        level = v.level if v.confidence >= self.min_confidence else CLEAR

        if level == TOO_CLOSE:
            self._too_close_streak += 1
            if self._too_close_streak >= self.confirm and now - self._last_alert_at >= self.cooldown_sec:
                self._last_alert_at = now
                first = not self._alerting
                self._alerting = True
                return ("Stop. Wall ahead. Back up." if first else "Still too close. Back up."), True
            return None

        self._too_close_streak = 0

        if level == APPROACHING:
            if self._alerting:
                # Was too close, now backing off: confirm once, quietly.
                self._alerting = False
                self._last_approach_at = now
                return "Okay, a little more room now.", False
            if self.speak_approach and now - self._last_approach_at >= self.approach_cooldown_sec:
                self._last_approach_at = now
                return "Wall ahead, slow down.", False
            return None

        # CLEAR
        if self._alerting:
            self._alerting = False
            return "Clear.", False
        return None


# ---------------------------------------------------------------------------
# Gemini Live session loop
# ---------------------------------------------------------------------------


SpeakFn = Callable[[str, bool], Awaitable[None]]
StatusFn = Callable[[dict[str, Any]], Awaitable[None]]


class WallDetector:
    def __init__(self) -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        self.enabled = _env_flag("SAW_WALL_DETECTOR", False) and bool(self.api_key)
        self.model = os.environ.get("SAW_GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
        self.interval = max(1.0, _env_float("SAW_WALL_INTERVAL_SEC", 1.0))
        self.reply_timeout = _env_float("SAW_WALL_REPLY_TIMEOUT", 4.0)
        # Reconnect well before the server's ~10 min connection lifetime.
        self.session_max_sec = _env_float("SAW_WALL_SESSION_SEC", 480.0)
        self.policy = AlertPolicy(
            cooldown_sec=_env_float("SAW_WALL_ALERT_COOLDOWN", 2.5),
            confirm=max(1, int(_env_float("SAW_WALL_CONFIRM", 1))),
            speak_approach=_env_flag("SAW_WALL_SPEAK_APPROACH", True),
        )
        self._latest: tuple[bytes, int] | None = None
        self._last_sent_ts: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._speak: SpeakFn | None = None
        self._status: StatusFn | None = None
        self.last_verdict: Verdict | None = None
        self.connected = False

    # -- wiring ------------------------------------------------------------

    def submit(self, jpeg: bytes, ts: int) -> None:
        """Keep only the newest frame. Never blocks. No-op when the detector is off."""
        if self.enabled:
            self._latest = (jpeg, ts)

    def start(self, speak: SpeakFn, status: StatusFn | None = None) -> None:
        self._speak = speak
        self._status = status
        if not self.enabled:
            log.debug("Gemini wall detector off (depth model is the proximity sensor; SAW_WALL_DETECTOR=1 enables it)")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name="saw-wall-detector")
            log.info("Wall detector starting: model=%s interval=%.1fs", self.model, self.interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # -- main loop ---------------------------------------------------------

    async def _run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._run_session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network, quota, bad key, ...
                self.connected = False
                log.warning("Gemini Live session ended: %s; reconnecting in %.0fs", exc, backoff)
                await self._emit_status(error=str(exc)[:200])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _build_config(self, types: Any) -> Any:
        tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=TOOL_NAME,
                    description="Report how close the walking path is to a wall or large obstacle.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "level": types.Schema(
                                type=types.Type.STRING,
                                enum=list(LEVELS),
                                description="clear, approaching (1-3 m), or too_close (under ~1 m).",
                            ),
                            "confidence": types.Schema(
                                type=types.Type.NUMBER,
                                description="0.0 to 1.0",
                            ),
                            "note": types.Schema(
                                type=types.Type.STRING,
                                description="At most 5 words, e.g. 'white wall' or 'open hallway'.",
                            ),
                        },
                        required=["level", "confidence"],
                    ),
                )
            ]
        )
        return types.LiveConnectConfig(
            # Live models answer in audio; we ignore the audio and use the tool call.
            response_modalities=[types.Modality.AUDIO],
            system_instruction=SYSTEM_PROMPT,
            tools=[tool],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Video sessions are capped at ~2 min without compression.
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
        )

    async def _run_session(self) -> None:
        from google import genai  # imported lazily so the server runs without the SDK
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        config = self._build_config(types)
        started = time.monotonic()

        async with client.aio.live.connect(model=self.model, config=config) as session:
            self.connected = True
            log.info("Gemini Live connected (%s)", self.model)
            await self._emit_status()
            replies: asyncio.Queue[Verdict] = asyncio.Queue()
            go_away = asyncio.Event()
            reader = asyncio.create_task(self._read_loop(session, types, replies, go_away))
            try:
                while not go_away.is_set() and time.monotonic() - started < self.session_max_sec:
                    tick = time.monotonic()
                    if reader.done():
                        reader.result()  # surface the reader's exception
                        raise RuntimeError("Gemini Live receive loop ended")
                    frame = self._latest
                    if frame is not None and frame[1] != self._last_sent_ts:
                        jpeg, ts = frame
                        self._last_sent_ts = ts
                        await session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))
                        await session.send_realtime_input(text="CHECK")
                        try:
                            verdict = await asyncio.wait_for(replies.get(), timeout=self.reply_timeout)
                        except asyncio.TimeoutError:
                            log.debug("no wall verdict within %.1fs", self.reply_timeout)
                        else:
                            await self._handle_verdict(verdict)
                        # Drop any late duplicates for older frames.
                        while not replies.empty():
                            replies.get_nowait()
                    elapsed = time.monotonic() - tick
                    await asyncio.sleep(max(0.05, self.interval - elapsed))
            finally:
                reader.cancel()
                self.connected = False
        log.info("Gemini Live session rotated")

    async def _read_loop(self, session: Any, types: Any, replies: asyncio.Queue, go_away: asyncio.Event) -> None:
        transcript = ""
        while True:
            got_any = False
            async for msg in session.receive():
                got_any = True
                if msg.go_away is not None:
                    log.info("Gemini Live go_away (time left %s)", msg.go_away.time_left)
                    go_away.set()
                if msg.tool_call and msg.tool_call.function_calls:
                    responses = []
                    for fc in msg.tool_call.function_calls:
                        if fc.name == TOOL_NAME:
                            replies.put_nowait(parse_args(fc.args or {}))
                        responses.append(
                            types.FunctionResponse(id=fc.id, name=fc.name, response={"result": "ok"})
                        )
                    await session.send_tool_response(function_responses=responses)
                sc = msg.server_content
                if sc is not None:
                    if sc.output_transcription and sc.output_transcription.text:
                        transcript += sc.output_transcription.text
                    if sc.turn_complete:
                        # Fallback: the model spoke instead of calling the tool.
                        guess = parse_text(transcript)
                        if guess is not None:
                            replies.put_nowait(guess)
                        transcript = ""
            # receive() ends after each complete turn; loop to keep listening.
            if not got_any:
                raise RuntimeError("Gemini Live connection closed")

    async def _handle_verdict(self, v: Verdict) -> None:
        self.last_verdict = v
        log.info("wall: %s (%.2f) %s", v.level, v.confidence, v.note)
        await self._emit_status()
        alert = self.policy.update(v)
        if alert and self._speak:
            text, urgent = alert
            await self._speak(text, urgent)

    async def _emit_status(self, error: str | None = None) -> None:
        if not self._status:
            return
        v = self.last_verdict
        payload: dict[str, Any] = {
            "connected": self.connected,
            "model": self.model,
            "level": v.level if v else None,
            "confidence": round(v.confidence, 2) if v else None,
            "note": v.note if v else "",
        }
        if error:
            payload["error"] = error
        try:
            await self._status(payload)
        except Exception:
            log.debug("wall status broadcast failed", exc_info=True)


def parse_args(args: dict[str, Any]) -> Verdict:
    level = str(args.get("level", CLEAR)).strip().lower().replace(" ", "_").replace("-", "_")
    if level not in LEVELS:
        level = CLEAR
    try:
        conf = float(args.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = min(1.0, max(0.0, conf))
    return Verdict(level=level, confidence=conf, note=str(args.get("note") or "")[:60])


def parse_text(text: str) -> Verdict | None:
    t = (text or "").lower().replace("-", "_")
    if not t.strip():
        return None
    if "too_close" in t or "too close" in t:
        return Verdict(TOO_CLOSE, 0.6, "from speech")
    if "approaching" in t:
        return Verdict(APPROACHING, 0.6, "from speech")
    if "clear" in t:
        return Verdict(CLEAR, 0.6, "from speech")
    return None


detector = WallDetector()
