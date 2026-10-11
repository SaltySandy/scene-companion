"""One line of lineage per processed frame, written to sessions/<id>/frames.jsonl."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

# sent: a proximity level went to the phone (or, with no reading, the hook spoke).
# suppressed: sent, but hysteresis held a higher level than this frame's raw one.
# stale: frame too old at send time; "unknown" went out instead of a level.
# none: no reading, or the sensor could not vouch (model missing or failed).
Decision = Literal["sent", "suppressed", "stale", "none"]


@dataclass(frozen=True)
class FrameRecord:
    session_id: str
    frame_key: int  # capture_ts until the phone sends a real frame_id
    capture_ts: int  # phone clock, epoch ms
    recv_ts: int  # laptop clock, epoch ms
    infer_start: int  # laptop clock, epoch ms
    infer_end: int  # laptop clock, epoch ms
    model: str | None  # None until a real model runs (commit 6)
    sectors_m: dict[str, float] | None  # {left, center, right} metres (commit 6)
    level: str | None  # proximity level (commit 6)
    decision: Decision
    message: str | None  # text sent to the phone, if any
    dropped_since_last: int  # frames overwritten in the slot before this one
    clock_offset_ms: int | None = None  # phone clock minus laptop clock, if synced
    clock_rtt_ms: int | None = None  # rtt of the sync sample; offset error <= rtt/2

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameRecord:
        """Rebuild from a parsed JSONL line. Unknown keys are ignored so old
        readers survive new fields."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})
