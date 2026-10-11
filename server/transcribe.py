"""Mic PCM -> faster-whisper, on a background thread.

The WebSocket handler only enqueues bytes. Model load and transcribe never
run on the asyncio loop, so a slow CPU decode cannot stall camera frames.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from typing import Awaitable, Callable

from server.vad import UtteranceSplitter

log = logging.getLogger("saw.transcribe")

Emit = Callable[[str, bool, int], Awaitable[None]]
DEFAULT_MODEL = "base.en"


class LiveTranscriber:
    def __init__(self, transcribe_fn: Callable[[bytes], str] | None = None) -> None:
        self._transcribe_fn = transcribe_fn
        self._splitter = UtteranceSplitter()
        self._splitter_lock = threading.Lock()
        self._queue: queue.Queue[tuple[bytes, int] | None] = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._emit: Emit | None = None
        self._model = None
        self._model_name = ""
        self.phone_speaking = False
        self.mute_until = 0.0
        self.dropped = 0

    def bind(self, loop: asyncio.AbstractEventLoop, emit: Emit) -> None:
        self._loop = loop
        self._emit = emit

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # A previous stop() can leave a sentinel if the thread had already exited.
        self._clear_queue()
        self._thread = threading.Thread(target=self._run, name="saw-whisper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._enqueue_stop()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _enqueue_stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self._clear_queue()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    def push_pcm(self, pcm: bytes, ts: int) -> None:
        """Non-blocking. Drops the chunk if the phone is speaking or the queue is full."""
        if not pcm or self._muted():
            if self._muted():
                with self._splitter_lock:
                    self._splitter.reset()
            return
        try:
            self._queue.put_nowait((pcm, ts))
        except queue.Full:
            self.dropped += 1

    def set_phone_speaking(self, active: bool) -> None:
        self.phone_speaking = bool(active)
        if active:
            with self._splitter_lock:
                self._splitter.reset()
        else:
            self.mute_until = max(self.mute_until, time.monotonic() + 0.4)

    def note_say(self, text: str) -> None:
        """Backup mute from the moment we send speech, before the phone flag arrives."""
        seconds = min(8.0, 0.45 + 0.05 * len(text or ""))
        self.mute_until = max(self.mute_until, time.monotonic() + seconds)
        with self._splitter_lock:
            self._splitter.reset()

    def _muted(self) -> bool:
        return self.phone_speaking or time.monotonic() < self.mute_until

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            pcm, ts = item
            if self._muted():
                with self._splitter_lock:
                    self._splitter.reset()
                continue
            try:
                with self._splitter_lock:
                    pieces = self._splitter.push(pcm, ts)
            except Exception:
                log.exception("vad failed")
                continue
            for piece in pieces:
                try:
                    text = self._transcribe(piece.pcm)
                except Exception:
                    log.exception("transcribe failed")
                    continue
                text = " ".join(text.split())
                if not text:
                    continue
                self._schedule(text, piece.is_final, piece.ts)

    def _schedule(self, text: str, is_final: bool, ts: int) -> None:
        loop = self._loop
        emit = self._emit
        if loop is None or emit is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(emit(text, is_final, ts), loop)
        except Exception:
            log.debug("transcript emit failed", exc_info=True)

    def _transcribe(self, pcm: bytes) -> str:
        if self._transcribe_fn is not None:
            return self._transcribe_fn(pcm)
        model = self._load_model()
        import numpy as np

        audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        segments, _info = model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=1,
            without_timestamps=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _load_model(self):
        name = (os.environ.get("WHISPER_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        if self._model is not None and self._model_name == name:
            return self._model
        from faster_whisper import WhisperModel

        log.info("loading whisper model %s (cpu, int8). First run downloads it.", name)
        self._model = WhisperModel(name, device="cpu", compute_type="int8")
        self._model_name = name
        return self._model


transcriber = LiveTranscriber()
