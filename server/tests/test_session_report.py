"""scripts/session_report.py: drops, gaps, and both clock modes."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.session_report import main, percentile, summarize


def rec(capture_ts: int, recv_delay: int = 40, infer_ms: int = 50, dropped: int = 0, **extra) -> dict:
    recv = capture_ts + recv_delay
    return {
        "session_id": "s1",
        "frame_key": capture_ts,
        "capture_ts": capture_ts,
        "recv_ts": recv,
        "infer_start": recv + 2,
        "infer_end": recv + 2 + infer_ms,
        "decision": "none",
        "level": None,
        "dropped_since_last": dropped,
        **extra,
    }


def test_percentile_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5
    assert percentile(list(range(1, 101)), 50) == 50
    assert percentile(list(range(1, 101)), 95) == 95


def test_drops_and_blind_gaps() -> None:
    records = [rec(0), rec(125), rec(250, dropped=2), rec(1250), rec(1375)]
    s = summarize(records)
    assert (s["frames"], s["dropped"]) == (5, 2)
    assert s["drop_rate"] == 2 / 7
    assert s["long_gaps"] == [{"gap_ms": 1000, "after_capture_ts": 250, "until_capture_ts": 1250}]
    assert s["latency_ms"]["inference"]["p50"] == 50


def test_unsynced_transport_is_relative_to_fastest_frame() -> None:
    # Phone clock is 5 s ahead: raw recv - capture is negative.
    records = [rec(10_000, recv_delay=-5000 + 30), rec(10_125, recv_delay=-5000 + 90)]
    s = summarize(records)
    assert not s["clock_synced"]
    assert s["latency_ms"]["transport"]["max"] == 60


def test_synced_transport_is_absolute() -> None:
    records = [rec(10_000, recv_delay=-5000 + 30, clock_offset_ms=5000)]
    s = summarize(records)
    assert s["clock_synced"]
    assert s["latency_ms"]["transport"]["p50"] == 30


def test_cli_reads_a_session_dir(tmp_path: Path, capsys) -> None:
    (tmp_path / "frames.jsonl").write_text("\n".join(json.dumps(rec(t)) for t in (0, 125, 250)) + "\n")
    assert main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["frames"] == 3
