"""Tests for the Gemini Live wall detector. No network or API key needed."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server import wall_detector as wd


# ---------------------------------------------------------------------------
# Alert policy
# ---------------------------------------------------------------------------


def V(level: str, conf: float = 0.9) -> wd.Verdict:
    return wd.Verdict(level=level, confidence=conf)


def test_too_close_alerts_urgently_then_repeats_after_cooldown() -> None:
    p = wd.AlertPolicy(cooldown_sec=2.5)
    assert p.update(V(wd.TOO_CLOSE), now=100.0) == ("Stop. Wall ahead. Back up.", True)
    assert p.update(V(wd.TOO_CLOSE), now=101.0) is None  # inside cooldown
    assert p.update(V(wd.TOO_CLOSE), now=103.0) == ("Still too close. Back up.", True)


def test_backing_off_gives_reassurance_once() -> None:
    p = wd.AlertPolicy()
    p.update(V(wd.TOO_CLOSE), now=0.0)
    assert p.update(V(wd.APPROACHING), now=1.0) == ("Okay, a little more room now.", False)
    assert p.update(V(wd.CLEAR), now=2.0) is None


def test_clear_after_too_close() -> None:
    p = wd.AlertPolicy()
    p.update(V(wd.TOO_CLOSE), now=0.0)
    assert p.update(V(wd.CLEAR), now=1.0) == ("Clear.", False)
    assert p.update(V(wd.CLEAR), now=2.0) is None


def test_approaching_is_rate_limited_and_optional() -> None:
    p = wd.AlertPolicy(approach_cooldown_sec=6.0)
    assert p.update(V(wd.APPROACHING), now=0.0) == ("Wall ahead, slow down.", False)
    assert p.update(V(wd.APPROACHING), now=3.0) is None
    assert p.update(V(wd.APPROACHING), now=7.0) == ("Wall ahead, slow down.", False)
    quiet = wd.AlertPolicy(speak_approach=False)
    assert quiet.update(V(wd.APPROACHING), now=0.0) is None


def test_confirm_requires_consecutive_frames() -> None:
    p = wd.AlertPolicy(confirm=2)
    assert p.update(V(wd.TOO_CLOSE), now=0.0) is None
    assert p.update(V(wd.CLEAR), now=1.0) is None  # streak broken
    assert p.update(V(wd.TOO_CLOSE), now=2.0) is None
    assert p.update(V(wd.TOO_CLOSE), now=3.0) == ("Stop. Wall ahead. Back up.", True)


def test_low_confidence_is_ignored() -> None:
    p = wd.AlertPolicy(min_confidence=0.35)
    assert p.update(V(wd.TOO_CLOSE, conf=0.1), now=0.0) is None


def test_parse_args_and_text() -> None:
    assert wd.parse_args({"level": "too_close", "confidence": 0.8}).level == wd.TOO_CLOSE
    assert wd.parse_args({"level": "Too-Close", "confidence": "2"}).confidence == 1.0
    assert wd.parse_args({"level": "banana"}).level == wd.CLEAR
    assert wd.parse_text("Too close!").level == wd.TOO_CLOSE
    assert wd.parse_text("approaching") .level == wd.APPROACHING
    assert wd.parse_text("") is None


# ---------------------------------------------------------------------------
# End-to-end against a fake Gemini Live session
# ---------------------------------------------------------------------------


class FakeSession:
    """Mimics google.genai AsyncSession: answers each CHECK with a tool call."""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.frames: list[bytes] = []
        self.tool_responses: list = []

    async def send_realtime_input(self, *, video=None, text=None, **_kw) -> None:
        if video is not None:
            self.frames.append(video.data)
        if text == "CHECK":
            level = self.script.pop(0) if self.script else "clear"
            fc = SimpleNamespace(id=f"call-{len(self.frames)}", name=wd.TOOL_NAME,
                                 args={"level": level, "confidence": 0.9, "note": "test"})
            await self.inbox.put(SimpleNamespace(tool_call=SimpleNamespace(function_calls=[fc]),
                                                 server_content=None, go_away=None))

    async def send_tool_response(self, *, function_responses) -> None:
        self.tool_responses.extend(function_responses)
        done = SimpleNamespace(turn_complete=True, output_transcription=None)
        await self.inbox.put(SimpleNamespace(tool_call=None, server_content=done, go_away=None))

    async def receive(self):
        while True:
            msg = await self.inbox.get()
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return


def test_end_to_end_with_fake_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    genai = pytest.importorskip("google.genai")
    session = FakeSession(["clear", "too_close", "too_close", "clear"])

    class FakeConnect:
        def __init__(self, *, model, config) -> None:
            assert config.tools[0].function_declarations[0].name == wd.TOOL_NAME

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            self.aio = SimpleNamespace(live=SimpleNamespace(connect=FakeConnect))

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setenv("GEMINI_API_KEY", "test")

    async def scenario() -> list[tuple[str, bool]]:
        det = wd.WallDetector()
        det.interval = 1.0
        det.policy.cooldown_sec = 0.0
        spoken: list[tuple[str, bool]] = []

        async def speak(text: str, urgent: bool) -> None:
            spoken.append((text, urgent))

        det._speak = speak
        task = asyncio.create_task(det._run_session())
        for i in range(4):
            det.submit(b"jpeg-%d" % i, ts=1000 + i)
            await asyncio.sleep(1.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(session.frames) == 4
        assert len(session.tool_responses) == 4
        return spoken

    spoken = asyncio.run(scenario())
    assert spoken == [
        ("Stop. Wall ahead. Back up.", True),
        ("Still too close. Back up.", True),
        ("Clear.", False),
    ]


def test_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert wd.WallDetector().enabled is False
