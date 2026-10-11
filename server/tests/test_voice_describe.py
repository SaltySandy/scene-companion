"""Voice "what's in front of me": matcher, ack, timeout, cooldown, e2e."""

from __future__ import annotations

import asyncio
import time

import pytest

from server import scene_assistant as sa
from server.ai_hook import on_transcript
from server.voice_intent import is_describe_scene, listen_default, normalize


POSITIVES = [
    "tell me what's in front of me",
    "what's in front of me",
    "what is in front of me",
    "what do you see",
    "describe what's ahead",
    "what am I looking at",
    "whats in front of me",
    "What's in front of me?",
    "what is in front of me.",
    "um what's in front of me",
    "please tell me what's in front of me",
    "can you tell me what's in front of me",
    "what's in front of the store",
    "what is in front of the store",
    "describe whats ahead",
    "what can you see",
    "tell me what you see",
]

NEGATIVES = [
    "I'm in front of the door",
    "i am in front of the door",
    "I'm in front of the store",
    "turn left",
    "what time is it",
    "what's the weather",
    "the door in front of me is open",
    "Looking.",
    "looking",
    "",
    "front",
]


@pytest.mark.parametrize("text", POSITIVES)
def test_describe_scene_positives(text: str) -> None:
    assert is_describe_scene(text), text


@pytest.mark.parametrize("text", NEGATIVES)
def test_describe_scene_negatives(text: str) -> None:
    assert not is_describe_scene(text), text


def test_normalize_tolerates_punctuation_and_curly_quotes() -> None:
    assert normalize("What’s in front of me?") == "whats in front of me"
    assert is_describe_scene("What’s in front of me?")


def test_listen_default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAW_LISTEN_DEFAULT", raising=False)
    assert listen_default() is True
    monkeypatch.setenv("SAW_LISTEN_DEFAULT", "0")
    assert listen_default() is False
    monkeypatch.setenv("SAW_LISTEN_DEFAULT", "yes")
    assert listen_default() is True


def _bind(a: sa.SceneAssistant, frame=(b"jpeg", 1)):
    spoken: list[str] = []
    urgent_flags: list[bool] = []

    async def speak(text: str, urgent: bool) -> None:
        spoken.append(text)
        urgent_flags.append(urgent)

    a.bind(speak=speak, frame=lambda: frame)
    return spoken, urgent_flags


@pytest.mark.asyncio
async def test_ack_says_looking_then_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_describe(q, jpeg):
        return sa.Description("A hallway with a chair.", "")

    monkeypatch.setattr(sa, "describe", fake_describe)
    a = sa.SceneAssistant()
    a.enabled = True
    a.nimble_enabled = False
    spoken, urgent = _bind(a)
    t0 = time.monotonic()
    await a.ask("what's in front of me", ack=True)
    assert spoken == ["Looking.", "A hallway with a chair."]
    assert urgent == [False, False]
    assert time.monotonic() - t0 < 1.0


@pytest.mark.asyncio
async def test_no_key_speaks_unavailable() -> None:
    a = sa.SceneAssistant()
    a.enabled = False
    spoken, urgent = _bind(a)
    await a.ask("what do you see", ack=True)
    assert spoken == [sa.UNAVAILABLE]
    assert urgent == [False]


@pytest.mark.asyncio
async def test_describe_timeout_speaks_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(q, jpeg):
        await asyncio.sleep(2)
        return sa.Description("too late", "")

    monkeypatch.setattr(sa, "describe", slow)
    a = sa.SceneAssistant()
    a.enabled = True
    a.nimble_enabled = False
    spoken, _ = _bind(a)
    await a.ask("what's in front of me", ack=True, timeout_s=0.05)
    assert spoken[0] == sa.LOOKING_ACK
    assert spoken[-1] == sa.TIMEOUT_FALLBACK


@pytest.mark.asyncio
async def test_cooldown_drops_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    n = {"n": 0}

    async def fake_describe(q, jpeg):
        n["n"] += 1
        return sa.Description("A desk.", "")

    monkeypatch.setattr(sa, "describe", fake_describe)
    a = sa.SceneAssistant()
    a.enabled = True
    a.nimble_enabled = False
    a.cooldown_s = 2.5
    spoken, _ = _bind(a)
    first = a.ask("what's in front of me", ack=True)
    second = a.ask("what's in front of me", ack=True)
    assert first is not None
    assert second is None
    await first
    assert n["n"] == 1
    assert spoken.count(sa.LOOKING_ACK) == 1


@pytest.mark.asyncio
async def test_on_transcript_describe_scene_e2e(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transcript in → mocked Gemini describe + spoken Looking. and answer."""
    described: list[tuple[str, bytes]] = []

    async def fake_describe(q, jpeg):
        described.append((q, jpeg))
        return sa.Description("A kitchen counter.", "")

    monkeypatch.setattr(sa, "describe", fake_describe)
    prev_enabled = sa.assistant.enabled
    prev_nimble = sa.assistant.nimble_enabled
    sa.assistant.enabled = True
    sa.assistant.nimble_enabled = False
    sa.assistant.reset_triggers()
    sa.assistant.cooldown_s = 0
    spoken: list[str] = []

    async def speak(text: str, urgent: bool) -> None:
        assert urgent is False
        spoken.append(text)

    sa.assistant.bind(speak=speak, frame=lambda: (b"jpeg-bytes", 99))
    t0 = time.monotonic()
    try:
        await on_transcript("tell me what's in front of me", True, 1)
        task = sa.assistant._task
        assert task is not None
        await task
        elapsed = time.monotonic() - t0
    finally:
        sa.assistant.reset_triggers()
        sa.assistant.enabled = prev_enabled
        sa.assistant.nimble_enabled = prev_nimble
        async def noop(text: str, urgent: bool) -> None:
            return None

        sa.assistant.bind(speak=noop, frame=lambda: None)

    assert described == [("tell me what's in front of me", b"jpeg-bytes")]
    assert spoken == ["Looking.", "A kitchen counter."]
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_on_transcript_statement_is_not_describe_scene(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(q, jpeg):
        raise AssertionError("describe should not run for a statement")

    monkeypatch.setattr(sa, "describe", boom)
    prev_enabled = sa.assistant.enabled
    sa.assistant.enabled = False
    sa.assistant.reset_triggers()
    spoken: list[str] = []

    async def speak(text: str, urgent: bool = False) -> None:
        spoken.append(text)

    monkeypatch.setattr("server.hub.say", speak)
    try:
        await on_transcript("I'm in front of the door", True, 1)
        assert sa.assistant._task is None or sa.assistant._task.done()
        assert spoken == ["You said: I'm in front of the door"]
    finally:
        sa.assistant.enabled = prev_enabled


@pytest.mark.asyncio
async def test_pcm_transcript_triggers_describe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom STT (stand-in for a WAV) → on_transcript → describe + say."""
    from server.transcribe import LiveTranscriber
    from server.vad import SpeechPiece

    described: list[str] = []
    spoken: list[str] = []

    async def fake_describe(q, jpeg):
        described.append(q)
        return sa.Description("An open doorway.", "")

    monkeypatch.setattr(sa, "describe", fake_describe)
    prev_enabled = sa.assistant.enabled
    sa.assistant.enabled = True
    sa.assistant.nimble_enabled = False
    sa.assistant.reset_triggers()
    sa.assistant.cooldown_s = 0

    async def speak(text: str, urgent: bool) -> None:
        spoken.append(text)

    sa.assistant.bind(speak=speak, frame=lambda: (b"frame", 1))

    worker = LiveTranscriber(transcribe_fn=lambda pcm: "what's in front of me")

    class InstantFinal:
        def push(self, pcm, ts):
            return [SpeechPiece(pcm, True, ts)]

        def reset(self):
            pass

    worker._splitter = InstantFinal()
    loop = asyncio.get_running_loop()
    worker.bind(loop, on_transcript)
    worker._queue.put((b"\x00\x00" * 100, 7))
    worker._queue.put(None)
    try:
        await asyncio.to_thread(worker._run)
        for _ in range(50):
            if sa.assistant._task and sa.assistant._task.done():
                break
            await asyncio.sleep(0.02)
        if sa.assistant._task:
            await sa.assistant._task
    finally:
        sa.assistant.reset_triggers()
        sa.assistant.enabled = prev_enabled

    assert described == ["what's in front of me"]
    assert spoken == ["Looking.", "An open doorway."]


def test_note_say_mutes_echo_of_looking() -> None:
    """Half-duplex: sending Looking. must not be heard as a new command."""
    from server.transcribe import LiveTranscriber

    worker = LiveTranscriber(transcribe_fn=lambda pcm: "what's in front of me")
    worker.note_say(sa.LOOKING_ACK)
    worker.push_pcm(b"\x00\x01" * 200, 1)
    assert worker._queue.qsize() == 0
    assert not is_describe_scene(sa.LOOKING_ACK)
