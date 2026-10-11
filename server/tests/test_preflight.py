"""preflight.checks: healthy stream is GO; silence, no model, or no phone is NO-GO."""

from __future__ import annotations

from scripts.preflight import FAIL, checks

HEALTHY = {"depth_model": "m", "phone_connected": True, "clock_offset_ms": 10, "clock_rtt_ms": 4, "last_cue_latency_ms": 150}


def beats(n: int, every: float, state: str = "ok") -> list[tuple[float, dict]]:
    return [(i * every, {"state": state, "level": "clear", "age_ms": 120}) for i in range(n)]


def failed(results) -> set[str]:
    return {name for status, name, _ in results if status == FAIL}


def test_healthy_stream_is_go() -> None:
    assert failed(checks(HEALTHY, beats(24, 0.125), seconds=3)) == set()


def test_no_model_no_phone_or_silence_fails() -> None:
    assert "depth model" in failed(checks({**HEALTHY, "depth_model": None}, beats(24, 0.125), 3))
    assert "phone" in failed(checks({**HEALTHY, "phone_connected": False}, beats(24, 0.125), 3))
    silent = failed(checks(HEALTHY, [], 3))
    assert {"heartbeat rate", "longest gap", "unknown beats"} <= silent
    assert "unknown beats" in failed(checks(HEALTHY, beats(24, 0.125, state="unknown"), 3))
