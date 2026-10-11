"""Single-clock latency, NTP-style clock sync, and on-demand flash test.

End-to-end latency that the proximity warning uses is measured on the *phone*
clock: capture timestamp -> ``UtteranceProgressListener.onStart``. The laptop
never subtracts the two wall clocks for that figure.

Stage breakdown (uplink / queue / process / downlink / TTS) uses an NTP-style
offset from the minimum-RTT ping/pong sample. Asymmetric paths (cellular
tunnels) bias the offset; the phone-clock e2e number stays honest.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from server import protocol as proto

DEFAULT_REMAINING_MS = float(os.environ.get("SAW_DEFAULT_REMAINING_MS", "250"))
ROLLING_WINDOW = int(os.environ.get("SAW_LATENCY_WINDOW", "80"))
FLASH_BRIGHTNESS = float(os.environ.get("SAW_FLASH_BRIGHTNESS", "200"))
FLASH_HOLD_MS = int(os.environ.get("SAW_FLASH_HOLD_MS", "8000"))


def percentile(samples: list[float], p: float) -> float | None:
    """Linear-interpolation percentile. ``p`` is 0-100."""
    if not samples:
        return None
    xs = sorted(samples)
    if len(xs) == 1:
        return float(xs[0])
    rank = (p / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


def ntp_offset_rtt(t0: float, t1: float, t2: float, t3: float) -> tuple[float, float]:
    """Return ``(offset_ms, rtt_ms)`` where offset is laptop minus phone.

    ``t0``/``t3`` are phone clock; ``t1``/``t2`` are laptop clock.
    offset ≈ ((t1 - t0) + (t2 - t3)) / 2 so ``laptop ≈ phone + offset``.
    """
    rtt = (t3 - t0) - (t2 - t1)
    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    return float(offset), float(rtt)


@dataclass
class Rolling:
    maxlen: int = ROLLING_WINDOW
    values: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not isinstance(self.values, deque) or self.values.maxlen != self.maxlen:
            self.values = deque(self.values, maxlen=self.maxlen)

    def add(self, value: float) -> None:
        self.values.append(float(value))

    def p(self, q: float) -> float | None:
        return percentile(list(self.values), q)

    def summary(self) -> dict[str, Any]:
        xs = list(self.values)
        return {
            "n": len(xs),
            "p50": None if not xs else round(percentile(xs, 50) or 0.0, 1),
            "p95": None if not xs else round(percentile(xs, 95) or 0.0, 1),
        }


@dataclass
class PendingSay:
    capture_ts: int
    recv_ms: int | None
    process_start_ms: int | None
    process_end_ms: int | None
    say_sent_ms: int


@dataclass
class FlashResult:
    flash_ts: int
    detect_ts: int
    scene_to_processed_ms: int
    brightness: float


class LatencyBook:
    """Process-wide latency + clock-offset state. Thread-safe for worker + HTTP."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.e2e = Rolling()
        self.uplink = Rolling()
        self.queue = Rolling()
        self.process = Rolling()
        self.downlink = Rolling()
        self.tts = Rolling()
        self.remaining = Rolling()
        self.offset_ms: float | None = None
        self.offset_rtt_ms: float | None = None
        self.sync_samples = 0
        self.pending: dict[int, PendingSay] = {}
        self.flash_armed = False
        self.flash_ts: int | None = None
        self.last_flash: FlashResult | None = None
        self.last_brightness: float | None = None
        self.frames_recv = 0
        self.frames_dropped_stale = 0

    def reset(self) -> None:
        lock = self._lock
        with lock:
            self.e2e = Rolling()
            self.uplink = Rolling()
            self.queue = Rolling()
            self.process = Rolling()
            self.downlink = Rolling()
            self.tts = Rolling()
            self.remaining = Rolling()
            self.offset_ms = None
            self.offset_rtt_ms = None
            self.sync_samples = 0
            self.pending = {}
            self.flash_armed = False
            self.flash_ts = None
            self.last_flash = None
            self.last_brightness = None
            self.frames_recv = 0
            self.frames_dropped_stale = 0

    def consider_sync(self, t0: float, t1: float, t2: float, t3: float) -> dict[str, float]:
        offset, rtt = ntp_offset_rtt(t0, t1, t2, t3)
        with self._lock:
            self.sync_samples += 1
            if rtt >= 0 and (self.offset_rtt_ms is None or rtt <= self.offset_rtt_ms):
                self.offset_ms = offset
                self.offset_rtt_ms = rtt
            return {
                "offset_ms": float(self.offset_ms if self.offset_ms is not None else offset),
                "rtt_ms": float(self.offset_rtt_ms if self.offset_rtt_ms is not None else rtt),
                "sample_offset_ms": offset,
                "sample_rtt_ms": rtt,
            }

    def note_frame(self, capture_ts: int, recv_ms: int) -> None:
        with self._lock:
            self.frames_recv += 1
            off = self.offset_ms
        if off is None:
            return
        uplink = recv_ms - (capture_ts + off)
        # Clock noise can make this slightly negative; clamp for the rolling window.
        if -500 <= uplink <= 10_000:
            with self._lock:
                self.uplink.add(max(0.0, uplink))

    def note_process(self, recv_ms: int, start_ms: int, end_ms: int) -> None:
        with self._lock:
            self.queue.add(max(0.0, start_ms - recv_ms))
            self.process.add(max(0.0, end_ms - start_ms))

    def note_say_sent(
        self,
        capture_ts: int | None,
        say_sent_ms: int,
        recv_ms: int | None = None,
        process_start_ms: int | None = None,
        process_end_ms: int | None = None,
    ) -> None:
        if capture_ts is None:
            return
        with self._lock:
            self.pending[int(capture_ts)] = PendingSay(
                capture_ts=int(capture_ts),
                recv_ms=recv_ms,
                process_start_ms=process_start_ms,
                process_end_ms=process_end_ms,
                say_sent_ms=say_sent_ms,
            )
            if len(self.pending) > 40:
                oldest = sorted(self.pending)[:20]
                for key in oldest:
                    self.pending.pop(key, None)

    def note_tts_start(
        self,
        capture_ts: int,
        tts_start_ts: int,
        say_recv_ts: int | None = None,
    ) -> dict[str, Any]:
        e2e = float(tts_start_ts - capture_ts)
        with self._lock:
            pending = self.pending.pop(int(capture_ts), None)
            off = self.offset_ms
            if 0 <= e2e <= 20_000:
                self.e2e.add(e2e)
            tts_engine = None
            if say_recv_ts is not None:
                tts_engine = float(tts_start_ts - say_recv_ts)
                if 0 <= tts_engine <= 10_000:
                    self.tts.add(tts_engine)
            downlink = None
            remain = None
            if pending is not None and off is not None and say_recv_ts is not None:
                downlink = (say_recv_ts + off) - pending.say_sent_ms
                if -500 <= downlink <= 10_000:
                    self.downlink.add(max(0.0, downlink))
            if pending is not None and pending.process_end_ms is not None and off is not None:
                remain = (tts_start_ts + off) - pending.process_end_ms
                if -500 <= remain <= 10_000:
                    self.remaining.add(max(0.0, remain))
            elif tts_engine is not None:
                remain = tts_engine
                if pending is not None and downlink is not None:
                    remain = max(0.0, downlink) + max(0.0, tts_engine)
                    self.remaining.add(remain)
        return {
            "e2e_ms": e2e,
            "tts_ms": tts_engine,
            "downlink_ms": downlink,
            "remaining_ms": remain,
        }

    def remaining_p95_ms(self) -> float:
        with self._lock:
            r = self.remaining.p(95)
            e = self.e2e.p(95)
        if r is not None:
            return r
        if e is not None:
            # Without a remaining series, e2e is the conservative total lead.
            return e
        return DEFAULT_REMAINING_MS

    def e2e_p95_ms(self) -> float | None:
        with self._lock:
            return self.e2e.p(95)

    def clock_offset_ms(self) -> float | None:
        with self._lock:
            return self.offset_ms

    def frame_age_ms(self, capture_ts: int, now_ms: int, recv_ms: int | None = None) -> float:
        """Age of this frame on the laptop clock. Falls back if offset is unknown."""
        with self._lock:
            off = self.offset_ms
        if off is not None:
            return max(0.0, now_ms - (capture_ts + off))
        if recv_ms is not None:
            return max(0.0, now_ms - recv_ms)
        return 0.0

    def lead_time_ms(self, capture_ts: int, now_ms: int, recv_ms: int | None = None) -> float:
        """frame_age + expected remaining (p95), the horizon the warning uses."""
        age = self.frame_age_ms(capture_ts, now_ms, recv_ms)
        return age + self.remaining_p95_ms()

    def arm_flash(self, flash_ts: int | None = None) -> int:
        ts = int(flash_ts if flash_ts is not None else proto.now_ms())
        with self._lock:
            self.flash_armed = True
            self.flash_ts = ts
        return ts

    def disarm_flash(self) -> None:
        with self._lock:
            self.flash_armed = False

    def note_brightness(self, brightness: float, detect_ts: int) -> FlashResult | None:
        with self._lock:
            self.last_brightness = float(brightness)
            if not self.flash_armed or self.flash_ts is None:
                return None
            if detect_ts - self.flash_ts > FLASH_HOLD_MS:
                self.flash_armed = False
                return None
            if brightness < FLASH_BRIGHTNESS:
                return None
            result = FlashResult(
                flash_ts=self.flash_ts,
                detect_ts=detect_ts,
                scene_to_processed_ms=int(detect_ts - self.flash_ts),
                brightness=float(brightness),
            )
            self.last_flash = result
            self.flash_armed = False
            return result

    def mark_stale(self) -> None:
        with self._lock:
            self.frames_dropped_stale += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            flash = None
            if self.last_flash is not None:
                flash = {
                    "flash_ts": self.last_flash.flash_ts,
                    "detect_ts": self.last_flash.detect_ts,
                    "scene_to_processed_ms": self.last_flash.scene_to_processed_ms,
                    "brightness": round(self.last_flash.brightness, 1),
                }
            return {
                "ok": True,
                "clock": {
                    "offset_ms": None if self.offset_ms is None else round(self.offset_ms, 2),
                    "rtt_ms": None if self.offset_rtt_ms is None else round(self.offset_rtt_ms, 2),
                    "samples": self.sync_samples,
                    "source": "min_rtt" if self.offset_ms is not None else None,
                },
                "e2e_ms": self.e2e.summary(),
                "stages_ms": {
                    "uplink": self.uplink.summary(),
                    "queue": self.queue.summary(),
                    "process": self.process.summary(),
                    "downlink": self.downlink.summary(),
                    "tts": self.tts.summary(),
                    "remaining": self.remaining.summary(),
                },
                "remaining_p95_ms": round(self.remaining.p(95) or self.e2e.p(95) or DEFAULT_REMAINING_MS, 1),
                "default_remaining_ms": DEFAULT_REMAINING_MS,
                "flash": {
                    "armed": self.flash_armed,
                    "flash_ts": self.flash_ts,
                    "last": flash,
                    "last_brightness": None
                    if self.last_brightness is None
                    else round(self.last_brightness, 1),
                    "threshold": FLASH_BRIGHTNESS,
                },
                "frames_recv": self.frames_recv,
                "frames_dropped_stale": self.frames_dropped_stale,
            }


book = LatencyBook()
