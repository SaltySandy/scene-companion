"""Energy VAD that splits 16 kHz mono PCM16 into utterances.

faster-whisper's own vad_filter runs later, on each utterance. This splitter
only decides where an utterance starts and ends so partials and finals can
be emitted. It does not load a model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SAMPLE_RATE = 16_000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 640


@dataclass(frozen=True)
class SpeechPiece:
    pcm: bytes
    is_final: bool
    ts: int


def frame_rms(frame: bytes) -> float:
    count = len(frame) // 2
    if count <= 0:
        return 0.0
    total = 0
    for i in range(0, count * 2, 2):
        sample = int.from_bytes(frame[i : i + 2], "little", signed=True)
        total += sample * sample
    return math.sqrt(total / count)


class UtteranceSplitter:
    def __init__(
        self,
        speech_rms: float = 500.0,
        end_silence_ms: int = 500,
        min_speech_ms: int = 200,
        partial_every_ms: int = 800,
    ) -> None:
        self.speech_rms = speech_rms
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self.partial_every_ms = partial_every_ms
        self._pending = bytearray()
        self.reset()

    def reset(self) -> None:
        self._in_speech = False
        self._speech = bytearray()
        self._silence_ms = 0
        self._speech_ms = 0
        self._since_partial_ms = 0
        self._start_ts = 0

    def push(self, pcm: bytes, ts: int) -> list[SpeechPiece]:
        self._pending.extend(pcm)
        pieces: list[SpeechPiece] = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            piece = self._feed_frame(frame, ts)
            if piece is not None:
                pieces.append(piece)
        return pieces

    def _feed_frame(self, frame: bytes, ts: int) -> SpeechPiece | None:
        loud = frame_rms(frame) >= self.speech_rms
        if not self._in_speech:
            if not loud:
                return None
            self._in_speech = True
            self._speech = bytearray()
            self._silence_ms = 0
            self._speech_ms = 0
            self._since_partial_ms = 0
            self._start_ts = ts
        self._speech.extend(frame)
        if loud:
            self._silence_ms = 0
            self._speech_ms += FRAME_MS
            self._since_partial_ms += FRAME_MS
            if self._speech_ms >= self.min_speech_ms and self._since_partial_ms >= self.partial_every_ms:
                self._since_partial_ms = 0
                return SpeechPiece(bytes(self._speech), False, self._start_ts)
            return None
        self._silence_ms += FRAME_MS
        if self._silence_ms < self.end_silence_ms:
            return None
        speech_ms = self._speech_ms
        pcm = bytes(self._speech)
        start = self._start_ts
        self.reset()
        if speech_ms < self.min_speech_ms:
            return None
        return SpeechPiece(pcm, True, start)
