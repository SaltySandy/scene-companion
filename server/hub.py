"""Shared state: phone sockets, viewer sockets, say/play helpers, latest-frame worker."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import sentry_sdk
from fastapi import WebSocket

from server import protocol as proto
from server.ai_hook import Play, Response, Say, on_frame
from server.event_sinks import sinks
from server.frame_record import FrameRecord
from server.recorder import Recorder

log = logging.getLogger("saw")


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

    async def attach_phone(self, ws: WebSocket) -> None:
        if self.phone is not None:
            try:
                await self.phone.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.phone = ws
        log.info("phone connected")
        sinks.log_event("connect", "phone connected")

    def detach_phone(self, ws: WebSocket) -> None:
        if self.phone is ws:
            self.phone = None
            log.info("phone disconnected")
            sinks.log_event("disconnect", "phone disconnected")

    def attach_viewer(self, ws: WebSocket) -> None:
        self.viewers.add(ws)

    def detach_viewer(self, ws: WebSocket) -> None:
        self.viewers.discard(ws)

    def submit_frame(self, jpeg: bytes, capture_ts: int, recv_ts: int | None = None) -> None:
        """Keep only the latest JPEG. The worker never queues a backlog.

        If the previous frame is still in the slot, the worker never saw it:
        count it as dropped before overwriting.
        """
        if recv_ts is None:
            recv_ts = proto.now_ms()
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

    async def say(self, text: str, urgent: bool = False) -> None:
        if not text or not str(text).strip():
            return
        msg = proto.encode_json(proto.MsgType.SAY, text=str(text).strip(), urgent=bool(urgent))
        await self._send_phone_text(msg)
        await self._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.SAY, text=str(text).strip(), urgent=bool(urgent))
        )
        log.info("say urgent=%s: %s", urgent, text)
        sinks.log_event("say", str(text).strip(), meta={"urgent": bool(urgent)})

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
        latency = max(0, recv_ms - capture_ts)
        return proto.encode_json(
            proto.MsgType.STATS,
            fps=fps,
            latency_ms=latency,
            inter_ms=round(inter, 1),
            bytes=nbytes,
            capture_ts=capture_ts,
            recv_ts=recv_ms,
            phone_connected=self.phone is not None,
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
        rec = FrameRecord(
            session_id=self.recorder.session_id,
            frame_key=frame.capture_ts,
            capture_ts=frame.capture_ts,
            recv_ts=frame.recv_ts,
            infer_start=infer_start,
            infer_end=infer_end,
            model=None,
            sectors_m=None,
            level=None,
            decision="sent" if items else "none",
            message=" | ".join(texts) or None,
            dropped_since_last=frame.dropped_before,
        )
        self.recorder.record(rec, frame.jpeg)
        return rec

    def ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self.worker_loop(), name="saw-ai-worker")


def _response_items(response: Response) -> list[Say | Play]:
    """Flatten a hook response to the items say()/play() would actually send."""
    if response is None:
        return []
    items = [response] if isinstance(response, (Say, Play)) else list(response)
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
