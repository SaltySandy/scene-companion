"""Vision helpers shared by offline pre-labeling and live descriptions.

Prefers Google Gemini when GEMINI_API_KEY (or GOOGLE_API_KEY) is set, and
falls back to Claude when only ANTHROPIC_API_KEY is set (or an `ant auth
login` profile is used for pre-labeling). Set SAW_VLM_MODEL to override the
default model for the active provider.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

log = logging.getLogger("saw.vlm")

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def anthropic_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def active_provider() -> str | None:
    """'gemini', 'anthropic', or None. Gemini wins when both keys are set."""
    if gemini_key():
        return "gemini"
    if anthropic_key():
        return "anthropic"
    return None


def model_name(provider: str | None = None) -> str:
    override = (os.environ.get("SAW_VLM_MODEL") or "").strip()
    if override:
        return override
    which = provider or active_provider()
    if which == "gemini":
        return DEFAULT_GEMINI_MODEL
    return DEFAULT_CLAUDE_MODEL


def make_client(provider: str | None = None) -> tuple[Any, str | None]:
    """Build the async/sync-capable client for the chosen provider.

    Blocking: import the SDK here (startup thread), not on the event loop.
    """
    which = provider or active_provider()
    if which == "gemini":
        from google import genai
        from google.genai import types  # noqa: F401 — cache for the first describe()

        return genai.Client(api_key=gemini_key()), "gemini"
    if which == "anthropic":
        import anthropic

        return anthropic.AsyncAnthropic(max_retries=0, timeout=8.0), "anthropic"
    return None, None


def image_block(jpeg: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def build_request(
    jpeg: bytes,
    prompt: str,
    schema: dict[str, Any],
    *,
    effort: str,
    max_tokens: int = 16000,
) -> dict[str, Any]:
    """Keyword arguments for client.beta.messages.create."""
    return {
        "model": model_name("anthropic"),
        "max_tokens": max_tokens,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "messages": [{"role": "user", "content": [image_block(jpeg), {"type": "text", "text": prompt}]}],
    }


def parse_json(response: Any) -> dict[str, Any] | None:
    """The JSON answer, or None if the model (and its fallback) declined or ran out of tokens."""
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        log.warning("VLM declined (category=%s)", category)
        return None
    if response.stop_reason == "max_tokens":
        log.warning("VLM hit max_tokens")
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    return json.loads(text) if text else None


def _is_gemini_client(client: Any) -> bool:
    aio = getattr(client, "aio", None)
    return aio is not None and hasattr(aio, "models")


def _parse_gemini_json(text: str | None) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("VLM returned non-JSON")
        return None
    return obj if isinstance(obj, dict) else None


def _gemini_schema(schema: dict[str, Any]) -> Any:
    """JSON Schema dict -> google.genai.types.Schema (same shape main's assistant uses)."""
    from google.genai import types

    type_map = {
        "object": types.Type.OBJECT,
        "string": types.Type.STRING,
        "number": types.Type.NUMBER,
        "integer": types.Type.INTEGER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
    }
    props = schema.get("properties") or {}
    return types.Schema(
        type=type_map.get(str(schema.get("type") or "object").lower(), types.Type.OBJECT),
        properties={
            name: types.Schema(type=type_map.get(str((spec or {}).get("type") or "string").lower(), types.Type.STRING))
            for name, spec in props.items()
        },
        required=list(schema.get("required") or []),
    )


async def _ask_gemini(client: Any, jpeg: bytes, prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    from google.genai import types

    resp = await client.aio.models.generate_content(
        model=model_name("gemini"),
        contents=[
            types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=300,
            response_mime_type="application/json",
            response_schema=_gemini_schema(schema),
        ),
    )
    return _parse_gemini_json(getattr(resp, "text", None))


def ask_json(client: Any, jpeg: bytes, prompt: str, schema: dict[str, Any], *, effort: str) -> dict[str, Any] | None:
    """Blocking call with an anthropic.Anthropic client. API errors propagate."""
    return parse_json(client.beta.messages.create(**build_request(jpeg, prompt, schema, effort=effort)))


async def ask_json_async(
    client: Any, jpeg: bytes, prompt: str, schema: dict[str, Any], *, effort: str
) -> dict[str, Any] | None:
    """Gemini (google.genai.Client) or Claude (anthropic.AsyncAnthropic). API errors propagate."""
    if _is_gemini_client(client):
        return await _ask_gemini(client, jpeg, prompt, schema)
    response = await client.beta.messages.create(**build_request(jpeg, prompt, schema, effort=effort))
    return parse_json(response)
