"""Shared state: phone sockets, viewer sockets, say/play helpers, latest-frame worker."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import sentry_sdk
from fastapi import WebSocket

from server import protocol as proto
from server.ai_hook import Play, ProximityReading, Response, Say, on_frame
from server.clock import ClockSync
from server.event_sinks import sinks
from server.frame_record import Decision, FrameRecord
from server.recorder import Recorder

log = logging.getLogger("saw")

# Clock sync: a quick burst after connect so the offset is known within ~1 s,
# then a slow trickle to follow drift and pick up a lower-rtt sample.
CLOCK_BURST = 5
CLOCK_BURST_SEC = 0.2
CLOCK_EVERY_SEC = 2.0

# A frame older than this at send time is reported as unknown, never as a level:
# telling the walker "clear" about where they were 1 s ago is worse than silence.
STALE_MS = int(os.environ.get("SAW_STALE_MS") or 700)

# Viewer FoV JPEG. Off the proximity hot path: only when someone is watching.
FOV_EVERY_SEC = float(os.environ.get("SAW_FOV_EVERY_SEC") or "0.25")

# Phone TTS confirmations. Drop oldest; never grow without bound.
AUDIO_LOG_MAX = 100


@dataclass(frozen=True)
class PendingFrame:
    """A frame waiting for the AI worker, with the lineage we know so far."""

    jpeg: bytes
    capture_ts: int  # phone clock, epoch ms
    recv_ts: int  # laptop clock, epoch ms
    dropped_before: int  # frames overwritten in the slot since the last take


@dataclass
class Hub:
    phone: WebSocket | None = None
    viewers: set[WebSocket] = field(default_factory=set)
    latest_frame: tuple[bytes, int, int] | None = None  # (jpeg, capture_ts, recv_ts)
    frame_event: asyncio.Event = field(default_factory=asyncio.Event)
    frames_in_window: list[float] = field(default_factory=list)
    last_recv_mono: float = 0.0
    worker_task: asyncio.Task[None] | None = None
    frames_dropped: int = 0  # total overwritten in the latest-frame slot
    _drops_since_take: int = 0
    recorder: Recorder = field(default_factory=Recorder.disabled)
    clock: ClockSync = field(default_factory=ClockSync)
    clock_task: asyncio.Task[None] | None = None
    proximity_seq: int = 0
    last_cue_latency_ms: int | None = None  # camera to cue start, phone clock
    last_jpeg: bytes | None = None  # latest phone JPEG; not consumed by take_latest()
    last_jpeg_ts: int | None = None
    last_recv_ts: int | None = None
    last_fov_jpeg: bytes | None = None
    last_fov_meta: dict[str, Any] | None = None
    fov_seq: int = 0
    fov_task: asyncio.Task[None] | None = None
    phone_config: dict[str, Any] = field(default_factory=dict)
    say_seq: int = 0
    audio_events: list[dict[str, Any]] = field(default_factory=list)

    async def attach_phone(self, ws: WebSocket) -> None:
        if self.phone is not None:
            try:
                await self.phone.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.phone = ws
        self.clock.reset()
        log.info("phone connected")
        sinks.log_event("connect", "phone connected")

    def detach_phone(self, ws: WebSocket) -> None:
        if self.phone is ws:
            self.phone = None
            log.info("phone disconnected")
            sinks.log_event("disconnect", "phone disconnected")

    def attach_viewer(self, ws: WebSocket) -> None:
        self.viewers.add(ws)
        log.info("viewer connected (%d watching)", len(self.viewers))

    def detach_viewer(self, ws: WebSocket) -> None:
        if ws in self.viewers:
            self.viewers.discard(ws)
            log.info("viewer disconnected (%d watching)", len(self.viewers))

    def submit_frame(self, jpeg: bytes, capture_ts: int, recv_ts: int | None = None) -> None:
        """Keep only the latest JPEG. The worker never queues a backlog.

        If the previous frame is still in the slot, the worker never saw it:
        count it as dropped before overwriting.
        """
        if recv_ts is None:
            recv_ts = proto.now_ms()
        self.last_jpeg = jpeg
        self.last_jpeg_ts = capture_ts
        self.last_recv_ts = recv_ts
        if self.latest_frame is not None:
            self.frames_dropped += 1
            self._drops_since_take += 1
        self.latest_frame = (jpeg, capture_ts, recv_ts)
        self.frame_event.set()

    def take_latest(self) -> PendingFrame | None:
        """Empty the slot and return what was in it, with its drop count."""
        item = self.latest_frame
        self.latest_frame = None
        if item is None:
            return None
        jpeg, capture_ts, recv_ts = item
        dropped = self._drops_since_take
        self._drops_since_take = 0
        return PendingFrame(jpeg, capture_ts, recv_ts, dropped)

    def note_audio(self, entry: dict[str, Any]) -> None:
        """Record a say / tts_start / audio_event. Drops the oldest past AUDIO_LOG_MAX."""
        rec = dict(entry)
        rec.setdefault("ts", proto.now_ms())
        self.audio_events.append(rec)
        overflow = len(self.audio_events) - AUDIO_LOG_MAX
        if overflow > 0:
            del self.audio_events[:overflow]

    async def note_phone_audio(self, obj: dict[str, Any]) -> None:
        """Phone confirmed TTS start/progress. Log, keep, and show on the viewer."""
        kind = str(obj.get("type") or "")
        event = obj.get("event")
        if kind == proto.MsgType.TTS_START:
            event = event or "playing"
        entry = {
            "type": kind,
            "id": obj.get("id"),
            "event": event,
            "text": obj.get("text"),
            "capture_ts": obj.get("capture_ts"),
            "tts_start_ts": obj.get("tts_start_ts"),
        }
        self.note_audio(entry)
        log.info("phone %s id=%s event=%s text=%s", kind, entry.get("id"), event, entry.get("text"))
        await self._broadcast_viewers_text(proto.encode_json(kind, **{k: v for k, v in entry.items() if k != "type"}))

    async def say(self, text: str, urgent: bool = False) -> None:
        if not text or not str(text).strip():
            return
        self.say_seq += 1
        say_id = f"say-{self.say_seq}"
        clean = str(text).strip()
        msg = proto.encode_json(proto.MsgType.SAY, text=clean, urgent=bool(urgent), id=say_id)
        await self._send_phone_text(msg)
        await self._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.SAY, text=clean, urgent=bool(urgent), id=say_id)
        )
        self.note_audio({"type": proto.MsgType.SAY, "id": say_id, "event": "sent", "text": clean, "urgent": bool(urgent)})
        log.info("say id=%s urgent=%s: %s", say_id, urgent, clean)
        sinks.log_event("say", clean, meta={"urgent": bool(urgent), "id": say_id})
        try:
            from server.transcribe import transcriber

            transcriber.note_say(clean)
        except Exception:
            log.debug("speech mute failed", exc_info=True)

    async def send_proximity(
        self,
        state: str,
        level: str | None = None,
        levels: dict[str, str] | None = None,
        sectors_m: dict[str, float] | None = None,
        frame_key: int | None = None,
        age_ms: int | None = None,
    ) -> None:
        """One proximity heartbeat to the phone and every viewer."""
        self.proximity_seq += 1
        msg = proto.encode_proximity(self.proximity_seq, state, level, levels, sectors_m, frame_key, age_ms)
        await self._send_phone_text(msg)
        await self._broadcast_viewers_text(msg)

    async def play(self, audio_bytes: bytes, mime: str = "audio/wav", urgent: bool = False) -> None:
        if not audio_bytes:
            return
        flags = proto.mime_to_flag(mime)
        envelope = proto.encode_binary(proto.Kind.AUDIO_CLIP, audio_bytes, flags=flags)
        # Urgent flag rides in JSON so the phone can flush before the clip arrives.
        meta = proto.encode_json(
            proto.MsgType.STATUS,
            msg="audio",
            urgent=bool(urgent),
            mime=proto.flag_to_mime(flags),
        )
        await self._send_phone_text(meta)
        await self._send_phone_bytes(envelope)
        log.info("play mime=%s bytes=%d urgent=%s", mime, len(audio_bytes), urgent)

    async def apply_response(self, response: Response) -> None:
        for item in _response_items(response):
            if isinstance(item, Say):
                await self.say(item.text, urgent=item.urgent)
            elif isinstance(item, Play):
                await self.play(item.audio_bytes, mime=item.mime, urgent=item.urgent)

    async def _send_phone_text(self, text: str) -> None:
        ws = self.phone
        if ws is None:
            return
        try:
            await ws.send_text(text)
        except Exception as exc:
            log.warning("phone send_text failed: %s", exc)
            self.detach_phone(ws)

    async def _send_phone_bytes(self, data: bytes) -> None:
        ws = self.phone
        if ws is None:
            return
        try:
            await ws.send_bytes(data)
        except Exception as exc:
            log.warning("phone send_bytes failed: %s", exc)
            self.detach_phone(ws)

    async def _broadcast_viewers_text(self, text: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.viewers):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.detach_viewer(ws)

    async def broadcast_frame_to_viewers(self, jpeg: bytes, ts: int, recv_ms: int) -> None:
        envelope = proto.encode_binary(proto.Kind.JPEG_FRAME, jpeg, timestamp_ms=ts)
        stats = self._stats_json(ts, recv_ms, len(jpeg))
        dead: list[WebSocket] = []
        for ws in list(self.viewers):
            try:
                await ws.send_bytes(envelope)
                await ws.send_text(stats)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.detach_viewer(ws)

    async def send_snapshot_to_viewer(self, ws: WebSocket) -> None:
        """Catch a late viewer up: last JPEG, last FoV meta. Failures drop that socket."""
        try:
            if self.last_jpeg is not None:
                ts = self.last_jpeg_ts if self.last_jpeg_ts is not None else 0
                recv = self.last_recv_ts if self.last_recv_ts is not None else proto.now_ms()
                await ws.send_bytes(proto.encode_binary(proto.Kind.JPEG_FRAME, self.last_jpeg, timestamp_ms=ts))
                await ws.send_text(self._stats_json(ts, recv, len(self.last_jpeg)))
            from server.ai_hook import fov_params

            meta = dict(self.last_fov_meta or fov_params())
            meta["seq"] = self.fov_seq
            meta["has_jpeg"] = self.last_fov_jpeg is not None
            if self.phone_config:
                meta["phone"] = self.phone_config
            await ws.send_text(proto.encode_json(proto.MsgType.FOV, **meta))
        except Exception as exc:
            log.warning("viewer snapshot send failed: %s", exc)
            self.detach_viewer(ws)

    async def fov_loop(self) -> None:
        """Render the FoV JPEG only while someone is watching. Never on the infer thread."""
        from server.ai_hook import fov_params, render_fov_jpeg

        while True:
            await asyncio.sleep(max(0.1, FOV_EVERY_SEC))
            if not self.viewers:
                continue
            try:
                packed = await asyncio.to_thread(render_fov_jpeg)
            except asyncio.CancelledError:
                raise
            except ImportError as exc:
                log.warning("fov render unavailable: %s", exc)
                await asyncio.sleep(5)
                continue
            except Exception:
                log.exception("fov render failed")
                continue
            self.fov_seq += 1
            if packed is None:
                meta = fov_params()
            else:
                jpeg, meta = packed
                self.last_fov_jpeg = jpeg
                meta = {**fov_params(), **meta}
            meta["seq"] = self.fov_seq
            meta["has_jpeg"] = self.last_fov_jpeg is not None
            if self.phone_config:
                meta["phone"] = self.phone_config
            self.last_fov_meta = meta
            await self._broadcast_viewers_text(proto.encode_json(proto.MsgType.FOV, **meta))

    def _stats_json(self, capture_ts: int, recv_ms: int, nbytes: int) -> str:
        import time

        now = time.monotonic()
        self.frames_in_window.append(now)
        cutoff = now - 1.0
        self.frames_in_window = [t for t in self.frames_in_window if t >= cutoff]
        fps = len(self.frames_in_window)
        inter = 0.0
        if self.last_recv_mono:
            inter = (now - self.last_recv_mono) * 1000.0
        self.last_recv_mono = now
        capture_laptop = self.clock.to_laptop(capture_ts)
        latency = max(0, recv_ms - (capture_ts if capture_laptop is None else capture_laptop))
        return proto.encode_json(
            proto.MsgType.STATS,
            fps=fps,
            latency_ms=latency,
            inter_ms=round(inter, 1),
            bytes=nbytes,
            capture_ts=capture_ts,
            recv_ts=recv_ms,
            phone_connected=self.phone is not None,
            clock_synced=capture_laptop is not None,
        )

    async def worker_loop(self) -> None:
        """Process only the latest submitted frame; never queue a backlog."""
        log.info("AI worker started")
        while True:
            await self.frame_event.wait()
            self.frame_event.clear()
            frame = self.take_latest()
            if frame is None:
                continue
            await self.process_frame(frame)

    async def process_frame(self, frame: PendingFrame) -> FrameRecord:
        """Run the hook on one frame, send its response, record the lineage.

        A failing hook still gets a record, so a gap in frames.jsonl always
        means a dropped frame, never a crash.
        """
        infer_start = proto.now_ms()
        try:
            result = await on_frame(frame.jpeg, frame.capture_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_frame failed")
            sentry_sdk.capture_exception()
            result = None
        infer_end = proto.now_ms()
        reading = _reading(result)
        decision: Decision = "none"
        age_ms: int | None = None
        if reading is not None:
            decision, age_ms = await self._send_reading(frame, reading)
        try:
            await self.apply_response(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("apply_response failed")
            sentry_sdk.capture_exception()
            result = None
        items = _response_items(result)
        texts = [str(item.text).strip() for item in items if isinstance(item, Say)]
        if decision == "none" and items:
            decision = "sent"  # no proximity reading, but the hook spoke
        rec = FrameRecord(
            session_id=self.recorder.session_id,
            frame_key=frame.capture_ts,
            capture_ts=frame.capture_ts,
            recv_ts=frame.recv_ts,
            infer_start=infer_start,
            infer_end=infer_end,
            model=reading.model if reading else None,
            sectors_m=reading.sectors_m if reading else None,
            level=reading.level if reading else None,
            decision=decision,
            message=" | ".join(texts) or None,
            dropped_since_last=frame.dropped_before,
            clock_offset_ms=self.clock.offset_ms,
            clock_rtt_ms=self.clock.rtt_ms,
        )
        self.recorder.record(rec, frame.jpeg)
        return rec

    async def _send_reading(self, frame: PendingFrame, reading: ProximityReading) -> tuple[Decision, int | None]:
        """Heartbeat for one frame, with the staleness gate. Returns (decision, age_ms)."""
        now = proto.now_ms()
        capture_laptop = self.clock.to_laptop(frame.capture_ts)
        age_ms = now - capture_laptop if capture_laptop is not None else None
        # Unsynced: time since receipt is a lower bound on the true age.
        gate_age = age_ms if age_ms is not None else now - frame.recv_ts
        key = frame.capture_ts
        if reading.state != proto.PROXIMITY_OK:
            await self.send_proximity(proto.PROXIMITY_UNKNOWN, frame_key=key, age_ms=age_ms)
            return "none", age_ms
        if gate_age > STALE_MS:
            await self.send_proximity(proto.PROXIMITY_UNKNOWN, frame_key=key, age_ms=age_ms)
            return "stale", age_ms
        await self.send_proximity(
            proto.PROXIMITY_OK, reading.level, reading.levels, reading.sectors_m, key, age_ms
        )
        return ("suppressed" if reading.raw_level != reading.level else "sent"), age_ms

    def on_ack(self, frame_key: int, cue_start_ts: int, state: str) -> None:
        """Phone started the cue for frame_key. Both timestamps are on the phone clock."""
        self.last_cue_latency_ms = cue_start_ts - frame_key
        self.recorder.record_ack(
            {
                "frame_key": frame_key,
                "cue_start_ts": cue_start_ts,
                "state": state,
                "recv_ts": proto.now_ms(),
            }
        )

    async def clock_loop(self) -> None:
        """Ping the phone with the laptop clock; app.py feeds pongs to self.clock."""
        phone: WebSocket | None = None
        sent = 0
        while True:
            if self.phone is not phone:
                phone, sent = self.phone, 0
            if phone is not None:
                await self._send_phone_text(proto.encode_json(proto.MsgType.PING, t0=proto.now_ms()))
                sent += 1
            await asyncio.sleep(CLOCK_BURST_SEC if sent < CLOCK_BURST else CLOCK_EVERY_SEC)

    def ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            # An Event binds to the first loop that waits on it; a restarted
            # app runs on a new loop, so the new worker needs a new Event.
            self.frame_event = asyncio.Event()
            if self.latest_frame is not None:
                self.frame_event.set()
            self.worker_task = asyncio.create_task(self.worker_loop(), name="saw-ai-worker")
        if self.clock_task is None or self.clock_task.done():
            self.clock_task = asyncio.create_task(self.clock_loop(), name="saw-clock")
        if self.fov_task is None or self.fov_task.done():
            self.fov_task = asyncio.create_task(self.fov_loop(), name="saw-fov")


def _flatten(response: Response) -> list:
    if response is None:
        return []
    if isinstance(response, (Say, Play, ProximityReading)):
        return [response]
    return list(response)


def _reading(response: Response) -> ProximityReading | None:
    return next((i for i in _flatten(response) if isinstance(i, ProximityReading)), None)


def _response_items(response: Response) -> list[Say | Play]:
    """Flatten a hook response to the items say()/play() would actually send."""
    items = _flatten(response)
    return [
        item
        for item in items
        if (isinstance(item, Say) and str(item.text).strip())
        or (isinstance(item, Play) and item.audio_bytes)
    ]


# Process-wide hub used by HTTP handlers and the hook helpers.
hub = Hub(recorder=Recorder.from_env())


async def say(text: str, urgent: bool = False) -> None:
    """Speak ``text`` on the phone via TTS. Urgent interrupts current speech."""
    await hub.say(text, urgent=urgent)


async def play(audio_bytes: bytes, mime: str = "audio/wav", urgent: bool = False) -> None:
    """Play an audio clip on the phone speaker."""
    await hub.play(audio_bytes, mime=mime, urgent=urgent)
