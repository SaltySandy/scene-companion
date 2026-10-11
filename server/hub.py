"""Shared state: phone sockets, viewer sockets, say/play helpers, latest-frame worker."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from server import protocol as proto
from server.ai_hook import Play, Response, Say, on_frame

log = logging.getLogger("saw")


@dataclass
class Hub:
    phone: WebSocket | None = None
    viewers: set[WebSocket] = field(default_factory=set)
    latest_frame: tuple[bytes, int] | None = None
    frame_event: asyncio.Event = field(default_factory=asyncio.Event)
    frames_in_window: list[float] = field(default_factory=list)
    last_recv_mono: float = 0.0
    worker_task: asyncio.Task[None] | None = None

    async def attach_phone(self, ws: WebSocket) -> None:
        if self.phone is not None:
            try:
                await self.phone.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.phone = ws
        log.info("phone connected")

    def detach_phone(self, ws: WebSocket) -> None:
        if self.phone is ws:
            self.phone = None
            log.info("phone disconnected")

    def attach_viewer(self, ws: WebSocket) -> None:
        self.viewers.add(ws)

    def detach_viewer(self, ws: WebSocket) -> None:
        self.viewers.discard(ws)

    def submit_frame(self, jpeg: bytes, ts: int) -> None:
        """Keep only the latest JPEG. The worker never queues a backlog."""
        self.latest_frame = (jpeg, ts)
        self.frame_event.set()

    async def say(self, text: str, urgent: bool = False) -> None:
        if not text or not str(text).strip():
            return
        msg = proto.encode_json(proto.MsgType.SAY, text=str(text).strip(), urgent=bool(urgent))
        await self._send_phone_text(msg)
        await self._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.SAY, text=str(text).strip(), urgent=bool(urgent))
        )
        log.info("say urgent=%s: %s", urgent, text)

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
        if response is None:
            return
        items: list[Say | Play]
        if isinstance(response, (Say, Play)):
            items = [response]
        else:
            items = list(response)
        for item in items:
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
            item = self.latest_frame
            self.latest_frame = None
            if item is None:
                continue
            jpeg, ts = item
            try:
                result = await on_frame(jpeg, ts)
                await self.apply_response(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("on_frame failed")

    def ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self.worker_loop(), name="saw-ai-worker")


# Process-wide hub used by HTTP handlers and the hook helpers.
hub = Hub()


async def say(text: str, urgent: bool = False) -> None:
    """Speak ``text`` on the phone via TTS. Urgent interrupts current speech."""
    await hub.say(text, urgent=urgent)


async def play(audio_bytes: bytes, mime: str = "audio/wav", urgent: bool = False) -> None:
    """Play an audio clip on the phone speaker."""
    await hub.play(audio_bytes, mime=mime, urgent=urgent)
