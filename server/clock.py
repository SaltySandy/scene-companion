"""Phone-to-laptop clock offset from ping/pong round trips (NTP-style).

The laptop sends ping {t0}; the phone answers pong {t0, tp} with its own
clock; the laptop notes t1 on arrival. Assuming the two legs take equal time,

    offset = tp - (t0 + t1) / 2      (phone clock minus laptop clock)

with error at most rtt / 2. Queueing delay inflates rtt and skews the
estimate, so we keep the lowest-rtt sample among recent ones.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

WINDOW = 16  # ~30 s of samples at one ping per 2 s


@dataclass(frozen=True)
class ClockSample:
    offset_ms: int
    rtt_ms: int


class ClockSync:
    def __init__(self, window: int = WINDOW) -> None:
        self._samples: deque[ClockSample] = deque(maxlen=window)

    def add(self, t0: int, tp: int, t1: int) -> ClockSample | None:
        """Record one round trip. Returns the sample, or None if it is nonsense."""
        rtt = t1 - t0
        if rtt < 0:
            return None
        sample = ClockSample(offset_ms=round(tp - (t0 + t1) / 2), rtt_ms=rtt)
        self._samples.append(sample)
        return sample

    def reset(self) -> None:
        """A new phone connection may be a different clock."""
        self._samples.clear()

    @property
    def best(self) -> ClockSample | None:
        return min(self._samples, key=lambda s: s.rtt_ms, default=None)

    @property
    def offset_ms(self) -> int | None:
        best = self.best
        return best.offset_ms if best else None

    @property
    def rtt_ms(self) -> int | None:
        best = self.best
        return best.rtt_ms if best else None

    def to_laptop(self, phone_ts: int) -> int | None:
        """Convert a phone-clock timestamp to the laptop clock, if synced."""
        offset = self.offset_ms
        return None if offset is None else phone_ts - offset
