"""Summarize one recorded session: latency per stage, drops, and blind gaps.

    python scripts/session_report.py                 # newest session under sessions/
    python scripts/session_report.py sessions/<id>   # a session directory
    python scripts/session_report.py path/frames.jsonl --json

Clock note: capture_ts is the phone clock, every other timestamp is the
laptop clock. When records carry clock_offset_ms (phone minus laptop, from
the ping/pong sync), phone-to-laptop latency is absolute. Without it, it is
reported relative to the session's fastest frame, which still shows stalls
and spikes but not the true network delay.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GAP_WARN_MS = 500  # a gap this long is a moment the walker had no sensor


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stats(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def find_jsonl(target: Path | None) -> Path:
    if target is None:
        sessions = sorted(p for p in (ROOT / "sessions").glob("*/frames.jsonl"))
        if not sessions:
            raise SystemExit("no sessions/*/frames.jsonl found (record with SAW_RECORD=1)")
        return max(sessions, key=lambda p: p.stat().st_mtime)
    if target.is_dir():
        return target / "frames.jsonl"
    return target


def load(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"frames": 0}
    records = sorted(records, key=lambda r: r["capture_ts"])
    processed = len(records)
    dropped = sum(r.get("dropped_since_last", 0) for r in records)
    span_ms = records[-1]["capture_ts"] - records[0]["capture_ts"]

    # Phone -> laptop. The offset is near-constant within a session, so the
    # best (lowest-rtt) sample also covers frames processed before the first pong.
    samples = [r for r in records if r.get("clock_offset_ms") is not None]
    synced = bool(samples)
    if synced:
        offset = min(samples, key=lambda r: r.get("clock_rtt_ms") or 0)["clock_offset_ms"]
        transport = [r["recv_ts"] - (r["capture_ts"] - offset) for r in records]
        end_to_end = [r["infer_end"] - (r["capture_ts"] - offset) for r in records]
    else:
        raw = [r["recv_ts"] - r["capture_ts"] for r in records]
        floor = min(raw)
        transport = [x - floor for x in raw]
        end_to_end = [r["infer_end"] - r["capture_ts"] - floor for r in records]

    gaps = [
        (b["capture_ts"] - a["capture_ts"], a["capture_ts"], b["capture_ts"])
        for a, b in zip(records, records[1:])
    ]
    long_gaps = sorted((g for g in gaps if g[0] >= GAP_WARN_MS), reverse=True)

    def count(field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in records:
            key = str(r.get(field))
            out[key] = out.get(key, 0) + 1
        return out

    return {
        "session_id": records[0].get("session_id"),
        "frames": processed,
        "dropped": dropped,
        "drop_rate": dropped / (processed + dropped),
        "duration_s": span_ms / 1000,
        "processed_fps": (processed - 1) / (span_ms / 1000) if span_ms > 0 else None,
        "clock_synced": synced,
        "clock_offset_ms": offset if synced else None,
        "latency_ms": {
            "transport": stats(transport),
            "queue": stats([r["infer_start"] - r["recv_ts"] for r in records]),
            "inference": stats([r["infer_end"] - r["infer_start"] for r in records]),
            "end_to_end": stats(end_to_end),
        },
        "capture_gap_ms": stats([g[0] for g in gaps]),
        "long_gaps": [{"gap_ms": g, "after_capture_ts": a, "until_capture_ts": b} for g, a, b in long_gaps[:10]],
        "decisions": count("decision"),
        "levels": count("level"),
    }


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}"


def render(summary: dict[str, Any]) -> str:
    if not summary["frames"]:
        return "no frames recorded"
    lines = [
        f"session     {summary['session_id']}",
        f"frames      {summary['frames']} processed, {summary['dropped']} dropped "
        f"({summary['drop_rate']:.0%}) over {summary['duration_s']:.1f}s"
        + (f", {summary['processed_fps']:.1f} fps processed" if summary["processed_fps"] else ""),
        "",
        f"latency ms              p50    p95    max   ({'absolute' if summary['clock_synced'] else 'transport/end-to-end relative to fastest frame'})",
    ]
    for stage, s in summary["latency_ms"].items():
        lines.append(f"  {stage:<20} {_fmt(s['p50']):>6} {_fmt(s['p95']):>6} {_fmt(s['max']):>6}")
    g = summary["capture_gap_ms"]
    lines += ["", f"capture gap ms  p50 {_fmt(g['p50'])}  p95 {_fmt(g['p95'])}  max {_fmt(g['max'])}"]
    if summary["long_gaps"]:
        lines.append(f"blind gaps >= {GAP_WARN_MS} ms:")
        for gap in summary["long_gaps"]:
            lines.append(f"  {gap['gap_ms']:>6} ms after capture_ts {gap['after_capture_ts']}")
    lines += [
        "",
        "decisions   " + ", ".join(f"{k}={v}" for k, v in sorted(summary["decisions"].items())),
        "levels      " + ", ".join(f"{k}={v}" for k, v in sorted(summary["levels"].items())),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", nargs="?", type=Path, help="session dir or frames.jsonl (default: newest)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    path = find_jsonl(args.target)
    summary = summarize(load(path))
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
