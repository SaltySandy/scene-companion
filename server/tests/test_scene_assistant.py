"""Tests for "What is this?" (Gemini + Nimble). No network or keys needed."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server import scene_assistant as sa


def run(coro):
    return asyncio.run(coro)


def make_assistant(frame=(b"jpeg", 1)) -> tuple[sa.SceneAssistant, list[str], list[dict]]:
    a = sa.SceneAssistant()
    a.enabled = True
    a.nimble_enabled = True
    spoken: list[str] = []
    events: list[dict] = []

    async def speak(text: str, urgent: bool) -> None:
        assert urgent is False  # answers never pre-empt wall alerts
        spoken.append(text)

    async def status(payload: dict) -> None:
        events.append(payload)

    a.bind(speak=speak, frame=lambda: frame, status=status)
    return a, spoken, events


def test_answer_then_nimble_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    async def fake_describe(q, jpeg):
        calls["describe"] = (q, jpeg)
        return sa.Description("A bottle of Advil, 200 milligram tablets.", "Advil ibuprofen 200mg tablets")

    async def fake_search(query):
        calls["search"] = query
        return [{"title": "Advil", "description": "Pain reliever and fever reducer.", "url": "https://x"}]

    async def fake_summarize(q, answer, results):
        calls["summarize"] = (answer, len(results))
        return "It's ibuprofen for pain and fever. Take with food."

    monkeypatch.setattr(sa, "describe", fake_describe)
    monkeypatch.setattr(sa, "nimble_search", fake_search)
    monkeypatch.setattr(sa, "summarize", fake_summarize)

    a, spoken, events = make_assistant()

    run(_ask(a, "What is this?"))
    assert calls["describe"] == ("What is this?", b"jpeg")
    assert calls["search"] == "Advil ibuprofen 200mg tablets"
    assert spoken == [
        "A bottle of Advil, 200 milligram tablets.",
        "More about it: It's ibuprofen for pain and fever. Take with food.",
    ]
    assert [e["stage"] for e in events] == ["looking", "answer", "more"]


def test_generic_scene_skips_nimble(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_describe(q, jpeg):
        return sa.Description("A hallway with a door on the left.", "")

    async def boom(query):
        raise AssertionError("Nimble should not be called")

    monkeypatch.setattr(sa, "describe", fake_describe)
    monkeypatch.setattr(sa, "nimble_search", boom)
    a, spoken, _ = make_assistant()
    run(_ask(a, "What's in front of me?"))
    assert spoken == ["A hallway with a door on the left."]


async def _ask(a, q):
    await a.ask(q)


def test_nimble_failure_keeps_first_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_describe(q, jpeg):
        return sa.Description("A can of Coke Zero.", "Coca-Cola Zero Sugar can")

    async def broken(query):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(sa, "describe", fake_describe)
    monkeypatch.setattr(sa, "nimble_search", broken)
    a, spoken, events = make_assistant()
    run(_ask(a, "What is this?"))
    assert spoken == ["A can of Coke Zero."]
    assert events[-1]["stage"] == "error"


def test_no_frame_yet() -> None:
    a, spoken, _ = make_assistant(frame=None)
    run(_ask(a, "What is this?"))
    assert "camera" in spoken[0].lower()


def test_gemini_failure_is_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(q, jpeg):
        raise RuntimeError("quota")

    monkeypatch.setattr(sa, "describe", broken)
    a, spoken, _ = make_assistant()
    run(_ask(a, "What is this?"))
    assert spoken == ["Sorry, I couldn't look at that right now."]


def test_parse_description() -> None:
    d = sa.parse_description('{"answer": "A red mug.", "search_query": ""}')
    assert (d.answer, d.search_query) == ("A red mug.", "")
    assert sa.parse_description("plain words").answer == "plain words"


def test_nimble_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_id": "x", "total_results": 1, "results": [
            {"title": "T", "description": "D", "url": "https://u", "content": "", "metadata": {}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("NIMBLE_API_KEY", "nk_test")
    results = run(sa.nimble_search("Advil 200mg"))
    assert seen["url"] == "https://sdk.nimbleway.com/v2/search"
    assert seen["auth"] == "Bearer nk_test"
    assert seen["body"]["query"] == "Advil 200mg"
    assert seen["body"]["search_depth"] == "lite"
    assert results[0]["title"] == "T"


def test_transcriber_gemini_backend_skips_partials(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.transcribe import LiveTranscriber
    from server.vad import SpeechPiece

    t = LiveTranscriber()
    t._backend = "gemini"
    sent: list[bytes] = []
    monkeypatch.setattr(t, "_gemini_transcribe", lambda pcm: sent.append(pcm) or "what is this")
    emitted: list[tuple] = []
    monkeypatch.setattr(t, "_schedule", lambda text, final, ts: emitted.append((text, final)))

    class FakeSplitter:
        def push(self, pcm, ts):
            return [SpeechPiece(b"p", False, ts), SpeechPiece(b"f", True, ts)]

        def reset(self):
            pass

    t._splitter = FakeSplitter()
    t._queue.put((b"x", 1))
    t._queue.put(None)
    t._run()
    assert sent == [b"f"]
    assert emitted == [("what is this", True)]
