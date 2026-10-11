"""Shared state: phone sockets, viewer sockets, say/play helpers, latest-frame workers."""

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
from server.audio_log import audio_log
from server.latency import book as latency_book
from server.proximity import engine as prox_engine
from server.proximity import mean_luminance
from server.trace import tracer

log = logging.getLogger("saw")


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    capture_ts: int
    recv_ms: int
    focus: dict[str, Any] | None = None


@dataclass
class Hub:
    phone: WebSocket | None = None
    viewers: set[WebSocket] = field(default_factory=set)
    latest_frame: Frame | None = None
    latest_prox: Frame | None = None
    frame_event: asyncio.Event = field(default_factory=asyncio.Event)
    prox_event: asyncio.Event = field(default_factory=asyncio.Event)
    frames_in_window: list[float] = field(default_factory=list)
    last_recv_mono: float = 0.0
    worker_task: asyncio.Task[None] | None = None
    prox_task: asyncio.Task[None] | None = None
    last_process_ctx: dict[str, int] | None = None
    pending_focus: dict[int, dict[str, Any]] = field(default_factory=dict)
    last_focus: dict[str, Any] | None = None
    last_focus_ts: int = 0

    async def attach_phone(self, ws: WebSocket) -> None:
        if self.phone is not None:
            try:
                await self.phone.close(code=4000, reason="replaced")
            except Exception:
                pass
        self.phone = ws
        # A new session: drop motion history so a reconnect cannot inherit speed.
        prox_engine.samples.clear()
        prox_engine.closing_mps = 0.0
        self.pending_focus.clear()
        self.last_focus = None
        self.last_focus_ts = 0
        tracer.start_session()
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

    def note_frame_meta(self, meta: dict[str, Any]) -> None:
        """Sidecar JSON sent immediately before a JPEG. Matched by capture ts."""
        try:
            ts = int(meta.get("ts"))
        except (TypeError, ValueError):
            return
        self.pending_focus[ts] = meta
        self.last_focus = meta
        self.last_focus_ts = ts
        if len(self.pending_focus) > 48:
            oldest = min(self.pending_focus)
            self.pending_focus.pop(oldest, None)

    def submit_frame(self, jpeg: bytes, ts: int, recv_ms: int | None = None) -> None:
        """Keep only the latest JPEG on each path. Workers never queue a backlog."""
        recv = int(recv_ms if recv_ms is not None else proto.now_ms())
        ts_i = int(ts)
        focus = self.pending_focus.pop(ts_i, None)
        if focus is None and self.last_focus is not None and abs(ts_i - self.last_focus_ts) <= 80:
            focus = self.last_focus
        frame = Frame(jpeg=jpeg, capture_ts=ts_i, recv_ms=recv, focus=focus)
        self.latest_frame = frame
        self.frame_event.set()
        self.latest_prox = frame
        self.prox_event.set()
        latency_book.note_frame(frame.capture_ts, frame.recv_ms)

    async def say(
        self,
        text: str,
        urgent: bool = False,
        capture_ts: int | None = None,
        hazard: bool | None = None,
    ) -> None:
        if not text or not str(text).strip():
            return
        audio_id = audio_log.next_id()
        fields: dict[str, Any] = {
            "text": str(text).strip(),
            "urgent": bool(urgent),
            "id": audio_id,
        }
        if capture_ts is not None:
            fields["capture_ts"] = int(capture_ts)
        msg = proto.encode_json(proto.MsgType.SAY, **fields)
        sent_ms = proto.now_ms()
        ctx = self.last_process_ctx or {}
        latency_book.note_say_sent(
            capture_ts,
            sent_ms,
            recv_ms=ctx.get("recv_ms"),
            process_start_ms=ctx.get("process_start_ms"),
            process_end_ms=ctx.get("process_end_ms"),
        )
        phone_connected = self.phone is not None
        send_ok = await self._send_phone_text(msg)
        is_hazard = bool(hazard) if hazard is not None else bool(urgent)
        entry = audio_log.record(
            audio_id=audio_id,
            kind="say",
            text=str(text).strip(),
            urgent=bool(urgent),
            hazard=is_hazard,
            sent_ms=sent_ms,
            phone_connected=phone_connected,
            send_ok=send_ok,
        )
        await self._broadcast_viewers_text(proto.encode_json(proto.MsgType.SAY, **fields))
        await self._broadcast_viewers_text(proto.encode_json(proto.MsgType.AUDIO_OUT, **entry.to_dict()))
        log.info("say id=%s urgent=%s capture_ts=%s send_ok=%s: %s", audio_id, urgent, capture_ts, send_ok, text)
        sinks.log_event(
            "say",
            str(text).strip(),
            hazard=is_hazard,
            meta={"urgent": bool(urgent), "capture_ts": capture_ts, "id": audio_id},
        )

    async def play(self, audio_bytes: bytes, mime: str = "audio/wav", urgent: bool = False) -> None:
        if not audio_bytes:
            return
        flags = proto.mime_to_flag(mime)
        envelope = proto.encode_binary(proto.Kind.AUDIO_CLIP, audio_bytes, flags=flags)
        audio_id = audio_log.next_id()
        # Urgent flag rides in JSON so the phone can flush before the clip arrives.
        meta = proto.encode_json(
            proto.MsgType.STATUS,
            msg="audio",
            urgent=bool(urgent),
            mime=proto.flag_to_mime(flags),
            id=audio_id,
        )
        phone_connected = self.phone is not None
        send_ok = await self._send_phone_text(meta)
        if send_ok:
            send_ok = await self._send_phone_bytes(envelope)
        entry = audio_log.record(
            audio_id=audio_id,
            kind="clip",
            text=f"clip {proto.flag_to_mime(flags)} {len(audio_bytes)} B",
            urgent=bool(urgent),
            hazard=bool(urgent),
            sent_ms=proto.now_ms(),
            phone_connected=phone_connected,
            send_ok=send_ok,
            mime=proto.flag_to_mime(flags),
        )
        await self._broadcast_viewers_text(proto.encode_json(proto.MsgType.AUDIO_OUT, **entry.to_dict()))
        log.info("play id=%s mime=%s bytes=%d urgent=%s send_ok=%s", audio_id, mime, len(audio_bytes), urgent, send_ok)

    async def apply_response(self, response: Response, capture_ts: int | None = None) -> None:
        if response is None:
            return
        items: list[Say | Play]
        if isinstance(response, (Say, Play)):
            items = [response]
        else:
            items = list(response)
        for item in items:
            if isinstance(item, Say):
                await self.say(item.text, urgent=item.urgent, capture_ts=capture_ts)
            elif isinstance(item, Play):
                await self.play(item.audio_bytes, mime=item.mime, urgent=item.urgent)

    async def _send_phone_text(self, text: str) -> bool:
        ws = self.phone
        if ws is None:
            return False
        try:
            await ws.send_text(text)
            return True
        except Exception as exc:
            log.warning("phone send_text failed: %s", exc)
            self.detach_phone(ws)
            return False

    async def _send_phone_bytes(self, data: bytes) -> bool:
        ws = self.phone
        if ws is None:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except Exception as exc:
            log.warning("phone send_bytes failed: %s", exc)
            self.detach_phone(ws)
            return False

    async def broadcast_audio_entry(self, entry) -> None:
        await self._broadcast_viewers_text(proto.encode_json(proto.MsgType.AUDIO_OUT, **entry.to_dict()))

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
        # Stamp-minus-recv is only honest after clock sync; still published
        # as latency_ms for back-compat. Prefer e2e_ms / stages from /latency.
        offset = latency_book.clock_offset_ms()
        if offset is not None:
            latency = max(0, int(round(recv_ms - (capture_ts + offset))))
        else:
            latency = max(0, recv_ms - capture_ts)
        snap = latency_book.snapshot()
        prox = prox_engine.snapshot()
        return proto.encode_json(
            proto.MsgType.STATS,
            fps=fps,
            latency_ms=latency,
            inter_ms=round(inter, 1),
            bytes=nbytes,
            capture_ts=capture_ts,
            recv_ts=recv_ms,
            phone_connected=self.phone is not None,
            e2e_p50_ms=snap["e2e_ms"]["p50"],
            e2e_p95_ms=snap["e2e_ms"]["p95"],
            remaining_p95_ms=snap["remaining_p95_ms"],
            clock_offset_ms=snap["clock"]["offset_ms"],
            clock_rtt_ms=snap["clock"]["rtt_ms"],
            proximity_m=prox["last_distance_m"],
            predicted_m=prox["last_predicted_m"],
            closing_mps=prox["last_closing_mps"],
            prox_ms=prox["last_process_ms"],
            prox_signal=prox.get("last_signal"),
            af_m=prox.get("last_af_m"),
            fused_m=prox.get("last_fused_m"),
            prox_reason=prox.get("last_reason"),
        )

    async def worker_loop(self) -> None:
        """LLM / demo path. Latest frame only; a slow model never stalls the socket."""
        log.info("AI worker started")
        while True:
            await self.frame_event.wait()
            self.frame_event.clear()
            item = self.latest_frame
            self.latest_frame = None
            if item is None:
                continue
            try:
                result = await on_frame(item.jpeg, item.capture_ts)
                await self.apply_response(result, capture_ts=item.capture_ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("on_frame failed")
                sentry_sdk.capture_exception()

    async def prox_loop(self) -> None:
        """Fast deterministic path. Isolated from the LLM so a 2s vision call cannot delay a warning."""
        log.info("proximity worker started")
        while True:
            await self.prox_event.wait()
            self.prox_event.clear()
            item = self.latest_prox
            self.latest_prox = None
            if item is None:
                continue
            process_start = proto.now_ms()
            try:
                luma = await asyncio.to_thread(mean_luminance, item.jpeg)
                if luma is not None:
                    flash = latency_book.note_brightness(luma, process_start)
                    if flash is not None:
                        log.info(
                            "flash detected brightness=%.1f scene_to_processed=%dms",
                            flash.brightness,
                            flash.scene_to_processed_ms,
                        )
                        await self._broadcast_viewers_text(
                            proto.encode_json(
                                proto.MsgType.FLASH,
                                detected=True,
                                scene_to_processed_ms=flash.scene_to_processed_ms,
                                brightness=round(flash.brightness, 1),
                            )
                        )
                decision = await asyncio.to_thread(
                    prox_engine.process_jpeg,
                    item.jpeg,
                    item.capture_ts,
                    item.recv_ms,
                    proto.now_ms(),
                    None,
                    item.focus,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("proximity failed")
                sentry_sdk.capture_exception()
                continue
            process_end = proto.now_ms()
            latency_book.note_process(item.recv_ms, process_start, process_end)
            try:
                tracer.record(
                    prox_engine.trace_row(
                        capture_ts=item.capture_ts,
                        recv_ms=item.recv_ms,
                        brightness=luma,
                        decision=decision,
                    ),
                    jpeg=item.jpeg,
                )
            except Exception:
                log.exception("proximity trace failed")
            self.last_process_ctx = {
                "recv_ms": item.recv_ms,
                "process_start_ms": process_start,
                "process_end_ms": process_end,
                "capture_ts": item.capture_ts,
            }
            if decision is None or not decision.should_warn:
                continue
            log.info(
                "proximity warn predicted=%.2fm distance=%.2fm closing=%.2f lead=%.0fms",
                decision.predicted_m,
                decision.distance_m,
                decision.closing_mps,
                decision.lead_ms,
            )
            await self.say(decision.text, urgent=True, capture_ts=item.capture_ts, hazard=True)

    def ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            # Rebind so a new event loop (pytest-asyncio) does not inherit a dead Event.
            self.frame_event = asyncio.Event()
            self.worker_task = asyncio.create_task(self.worker_loop(), name="saw-ai-worker")
        if self.prox_task is None or self.prox_task.done():
            self.prox_event = asyncio.Event()
            self.prox_task = asyncio.create_task(self.prox_loop(), name="saw-prox-worker")

    def reset_for_tests(self) -> None:
        self.latest_frame = None
        self.latest_prox = None
        self.frames_in_window.clear()
        self.last_recv_mono = 0.0
        self.last_process_ctx = None
        self.phone = None
        self.viewers.clear()
        self.worker_task = None
        self.prox_task = None
        try:
            self.frame_event = asyncio.Event()
            self.prox_event = asyncio.Event()
        except RuntimeError:
            pass
        self.pending_focus.clear()
        self.last_focus = None
        self.last_focus_ts = 0
        prox_engine.reset()
        latency_book.reset()
        audio_log.reset()
        tracer.reset()


# Process-wide hub used by HTTP handlers and the hook helpers.
hub = Hub()


async def say(text: str, urgent: bool = False, capture_ts: int | None = None) -> None:
    """Speak ``text`` on the phone via TTS. Urgent interrupts current speech."""
    await hub.say(text, urgent=urgent, capture_ts=capture_ts)


async def play(audio_bytes: bytes, mime: str = "audio/wav", urgent: bool = False) -> None:
    """Play an audio clip on the phone speaker."""
    await hub.play(audio_bytes, mime=mime, urgent=urgent)
