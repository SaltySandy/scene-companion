"""When to describe a new obstacle, and when to drop the answer."""

from __future__ import annotations

import sys
import types as ty

import pytest

import server.ai_hook as ai_hook
from server.ai import describe, vlm
from server.ai.describe import DescribeGate, clean


@pytest.fixture(autouse=True)
def _reset_describe():
    describe._client = None
    describe._provider = None
    describe._disabled_reason = None
    yield
    describe._client = None
    describe._provider = None
    describe._disabled_reason = None


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


async def test_no_key_disables_without_building_a_client(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert describe.status().startswith("idle")
    assert describe.available()
    assert await describe.describe(b"\xff\xd8", "center") is None
    assert not describe.available()
    assert "no API key" in describe.status()


def test_provider_prefers_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    assert vlm.active_provider() == "gemini"
    assert describe.status() == "on (gemini)"


def test_provider_falls_back_to_anthropic(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    assert vlm.active_provider() == "anthropic"
    assert describe.status() == "on (anthropic)"


def test_provider_idle_when_neither_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert vlm.active_provider() is None
    assert "idle" in describe.status()


def test_saw_vlm_model_overrides_gemini_default(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.delenv("SAW_VLM_MODEL", raising=False)
    assert vlm.model_name() == "gemini-3.5-flash-lite"
    monkeypatch.setenv("SAW_VLM_MODEL", "gemini-3.8-flash")
    assert vlm.model_name() == "gemini-3.8-flash"


def test_fov_describe_reports_provider(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setattr(describe, "_provider", "gemini")
    assert ai_hook.fov_params()["describe"] == "on (gemini)"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(describe, "_provider", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    assert ai_hook.fov_params()["describe"] == "on (anthropic)"


def test_warmup_uses_gemini_client_from_vlm(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(vlm, "make_client", lambda provider=None: (sentinel, "gemini"))
    describe.warmup()
    assert describe._client is sentinel
    assert describe._provider == "gemini"


def test_make_client_prefers_gemini_and_skips_anthropic(monkeypatch) -> None:
    created: dict[str, str] = {}

    class FakeClient:
        def __init__(self, api_key=None):
            created["key"] = api_key

    google = ty.ModuleType("google")
    genai_mod = ty.ModuleType("google.genai")
    genai_mod.Client = FakeClient
    types_mod = ty.ModuleType("google.genai.types")
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    client, provider = vlm.make_client()
    assert provider == "gemini"
    assert created["key"] == "gk"
    assert client is not None


class _FakeGeminiClient:
    """Shape of google.genai.Client: aio.models.generate_content."""

    def __init__(self, text: str = '{"label": "open door"}') -> None:
        self.calls: list[dict] = []
        async def generate_content(**kwargs):
            self.calls.append(kwargs)
            return ty.SimpleNamespace(text=text)

        self.aio = ty.SimpleNamespace(models=ty.SimpleNamespace(generate_content=generate_content))


async def test_describe_uses_mocked_gemini_client(monkeypatch) -> None:
    client = _FakeGeminiClient()

    async def fake_ask(c, jpeg, prompt, schema):
        assert c is client
        assert jpeg.startswith(b"\xff\xd8")
        assert "visually impaired" in prompt
        return {"label": "open door"}

    monkeypatch.setattr(vlm, "_ask_gemini", fake_ask)
    monkeypatch.setattr(describe, "_client", client)
    monkeypatch.setattr(describe, "_provider", "gemini")
    assert await describe.describe(b"\xff\xd8", "center") == "open door"
    assert describe.available()


async def test_ask_json_async_dispatches_to_gemini(monkeypatch) -> None:
    seen: dict = {}

    async def fake_ask(client, jpeg, prompt, schema):
        seen["args"] = (jpeg, prompt, schema)
        return {"label": "chair"}

    monkeypatch.setattr(vlm, "_ask_gemini", fake_ask)
    assert await vlm.ask_json_async(_FakeGeminiClient(), b"j", "p", describe.SCHEMA, effort="low") == {
        "label": "chair"
    }
    assert seen["args"][0] == b"j"


async def test_ask_gemini_calls_generate_content(monkeypatch) -> None:
    pytest.importorskip("google.genai")
    monkeypatch.setenv("SAW_VLM_MODEL", "gemini-3.5-flash-lite")
    client = _FakeGeminiClient('{"label": "glass wall"}')
    answer = await vlm._ask_gemini(client, b"\xff\xd8\xff\xd9", "name it", describe.SCHEMA)
    assert answer == {"label": "glass wall"}
    call = client.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].max_output_tokens == 300


def test_warmup_does_not_import_on_describe_module_load() -> None:
    """The client stays unset until warmup(); describe() must not construct one."""
    assert describe._client is None
    assert describe._provider is None
