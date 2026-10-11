"""Go/no-go check before a walk. Run it with the phone strapped on and streaming.

    python scripts/preflight.py                  # server on 127.0.0.1:8765
    python scripts/preflight.py --host 192.168.1.20:8765 --seconds 5

Reads /health, then listens as a viewer and measures the real proximity
heartbeat: rate, longest gap, unknown share, and frame age. Exit code 0 = GO.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx
import websockets

FAIL, WARN, OK = "FAIL", "WARN", "ok"


async def listen(url: str, seconds: float) -> list[tuple[float, dict]]:
    beats: list[tuple[float, dict]] = []
    async with websockets.connect(url, max_size=8_000_000) as ws:
        stop = time.monotonic() + seconds
        while (left := stop - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, str):
                msg = json.loads(raw)
                if msg.get("type") == "proximity":
                    beats.append((time.monotonic(), msg))
    return beats


def checks(health: dict, beats: list[tuple[float, dict]], seconds: float) -> list[tuple[str, str, str]]:
    out = []

    def add(status: str, name: str, detail: str) -> None:
        out.append((status, name, detail))

    add(OK if health.get("depth_model") else FAIL, "depth model", str(health.get("depth_model") or "not loaded"))
    add(OK if health.get("phone_connected") else FAIL, "phone", "connected" if health.get("phone_connected") else "not connected")
    offset = health.get("clock_offset_ms")
    add(OK if offset is not None else WARN, "clock sync",
        f"offset {offset} ms, rtt {health.get('clock_rtt_ms')} ms" if offset is not None else "no pong yet (old APK?)")

    rate = len(beats) / seconds
    gaps = [(b[0] - a[0]) * 1000 for a, b in zip(beats, beats[1:])]
    max_gap = max(gaps) if gaps else float("inf")
    add(OK if rate >= 3 else WARN if rate >= 2 else FAIL, "heartbeat rate", f"{rate:.1f}/s")
    add(OK if max_gap < 700 else WARN if max_gap < 1000 else FAIL, "longest gap",
        f"{max_gap:.0f} ms (phone says sensor lost at 1000)" if gaps else "no heartbeats")
    unknown = sum(1 for _, m in beats if m.get("state") != "ok")
    share = unknown / len(beats) if beats else 1.0
    add(OK if share <= 0.05 else WARN if share <= 0.2 else FAIL, "unknown beats", f"{unknown}/{len(beats)}")
    ages = sorted(m["age_ms"] for _, m in beats if m.get("age_ms") is not None)
    if ages:
        med = ages[len(ages) // 2]
        add(OK if med <= 300 else WARN, "frame age at send", f"median {med} ms")
    cue = health.get("last_cue_latency_ms")
    add(OK if cue is not None and cue <= 400 else WARN, "camera-to-cue",
        f"{cue} ms" if cue is not None else "no acks yet (old APK?)")
    levels = {}
    for _, m in beats:
        levels[str(m.get("level"))] = levels.get(str(m.get("level")), 0) + 1
    add(OK, "levels seen", ", ".join(f"{k}={v}" for k, v in sorted(levels.items())) or "-")
    add(OK, "recording", str(health.get("recording") or "off (set SAW_RECORD=1 for rehearsals)"))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1:8765")
    p.add_argument("--seconds", type=float, default=3.0)
    args = p.parse_args(argv)
    try:
        health = httpx.get(f"http://{args.host}/health", timeout=3).json()
    except httpx.HTTPError as exc:
        print(f"FAIL  server        not reachable at {args.host}: {exc}")
        return 1
    beats = asyncio.run(listen(f"ws://{args.host}/v1/viewer", args.seconds))
    results = checks(health, beats, args.seconds)
    for status, name, detail in results:
        print(f"{status:<5} {name:<18} {detail}")
    failed = any(s == FAIL for s, _, _ in results)
    print("\nNO-GO: fix the FAIL lines before anyone walks." if failed else "\nGO (spotter still required).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
