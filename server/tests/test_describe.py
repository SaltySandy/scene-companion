"""When to describe a new obstacle, and when to drop the answer."""

from __future__ import annotations

import pytest

import server.ai_hook as ai_hook
from server.ai import describe
from server.ai.describe import DescribeGate, clean


def test_gate_center_near_sides_only_danger_and_cooldown() -> None:
    g = DescribeGate(cooldown_s=6)
    clear = {"left": "clear", "center": "clear", "right": "clear"}
    assert g.check(clear, {**clear, "left": "near"}, now=0) is None  # corridor wall
    assert g.check(clear, {**clear, "center": "near"}, now=0) == "center"
    g.done()
    assert g.check(clear, {**clear, "right": "danger"}, now=3) is None  # cooldown
    assert g.check(clear, {**clear, "right": "danger"}, now=7) == "right"
    # In flight: no second call even after the cooldown.
    assert g.check(clear, {**clear, "center": "danger"}, now=20) is None
    g.done()
    # Staying near is not new.
    assert g.check({**clear, "center": "near"}, {**clear, "center": "near"}, now=30) is None


def test_clean_labels() -> None:
    assert clean("An open door!") == "an open door"
    assert clean("none") is None
    assert clean("a b c d e f") == "a b c d"


@pytest.fixture
def spoken(monkeypatch):
    out: list[str] = []

    async def fake_say(text: str, urgent: bool = False) -> None:
        out.append(text)

    import server.hub as hub_module

    monkeypatch.setattr(hub_module, "say", fake_say)
    monkeypatch.setattr(ai_hook, "_gate", DescribeGate())
    return out


async def test_says_label_while_obstacle_is_still_there(spoken, monkeypatch) -> None:
    async def fake_describe(jpeg, sector):
        return "open door"

    monkeypatch.setattr(describe, "describe", fake_describe)
    monkeypatch.setattr(ai_hook, "_levels", {"center": "near"})
    await ai_hook._describe_and_say(b"j", "center", describe.monotonic())
    assert spoken == ["open door ahead"]
    assert not ai_hook._gate.in_flight


async def test_drops_label_if_cleared_or_late(spoken, monkeypatch) -> None:
    async def fake_describe(jpeg, sector):
        return "chair"

    monkeypatch.setattr(describe, "describe", fake_describe)
    monkeypatch.setattr(ai_hook, "_levels", {"left": "clear"})
    await ai_hook._describe_and_say(b"j", "left", describe.monotonic())
    monkeypatch.setattr(ai_hook, "_levels", {"left": "danger"})
    await ai_hook._describe_and_say(b"j", "left", describe.monotonic() - 10)
    assert spoken == []


async def test_missing_credentials_disable_quietly(monkeypatch) -> None:
    async def no_credentials(*args, **kwargs):
        raise TypeError("Could not resolve authentication method")  # what the SDK raises

    monkeypatch.setattr(describe, "_client", object())
    monkeypatch.setattr(describe, "_disabled_reason", None)
    monkeypatch.setattr(describe.vlm, "ask_json_async", no_credentials)
    assert await describe.describe(b"\xff\xd8", "center") is None
    assert not describe.available()
