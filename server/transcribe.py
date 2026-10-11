"""Mic PCM -> text, on a background thread.

Backend: local faster-whisper when it is installed, otherwise Gemini (needs
GEMINI_API_KEY; only finished utterances are sent, no grey partials).
Force one with SAW_STT_BACKEND=whisper|gemini.

The WebSocket handler only enqueues bytes. Model load and transcribe never
run on the asyncio loop, so a slow CPU decode cannot stall camera frames.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import queue
import threading
import time
import wave
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
        self._backend: str | None = None
        self._gemini_client = None

    @property
    def backend(self) -> str:
        """"custom", "whisper", "gemini" or "none". Decided once."""
        if self._backend is None:
            if self._transcribe_fn is not None:
                self._backend = "custom"
            else:
                forced = os.environ.get("SAW_STT_BACKEND", "auto").strip().lower()
                has_whisper = importlib.util.find_spec("faster_whisper") is not None
                has_gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
                if forced == "whisper" or (forced == "auto" and has_whisper):
                    self._backend = "whisper"
                elif forced == "gemini" or (forced == "auto" and has_gemini):
                    self._backend = "gemini"
                else:
                    self._backend = "none"
            log.info("speech-to-text backend: %s", self._backend)
        return self._backend

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
                # Gemini is a network call: skip partials, send only full utterances.
                if not piece.is_final and self.backend == "gemini":
                    continue
                if self.backend == "none":
                    continue
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
        if self.backend == "gemini":
            return self._gemini_transcribe(pcm)
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

    def _gemini_transcribe(self, pcm: bytes) -> str:
        from google import genai
        from google.genai import types

        if self._gemini_client is None:
            self._gemini_client = genai.Client(
                api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16_000)
            w.writeframes(pcm)
        model = os.environ.get("SAW_GEMINI_STT_MODEL", "gemini-3.5-flash-lite").strip()
        resp = self._gemini_client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                "Transcribe this English speech exactly. Output only the spoken words. "
                "If there is no clear speech, output nothing.",
            ],
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=120),
        )
        return (resp.text or "").strip()

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
