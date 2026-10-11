"""Claude vision helpers shared by offline pre-labeling and live descriptions.

Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth
login` profile). The model defaults to claude-opus-5-5; set SAW_VLM_MODEL to
change it. Every request opts into server-side refusal fallbacks and asks for
JSON that matches a schema.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

log = logging.getLogger("saw.vlm")

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model_name() -> str:
    return (os.environ.get("SAW_VLM_MODEL") or "").strip() or DEFAULT_MODEL


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
        "model": model_name(),
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


def ask_json(client: Any, jpeg: bytes, prompt: str, schema: dict[str, Any], *, effort: str) -> dict[str, Any] | None:
    """Blocking call with an anthropic.Anthropic client. API errors propagate."""
    return parse_json(client.beta.messages.create(**build_request(jpeg, prompt, schema, effort=effort)))


async def ask_json_async(
    client: Any, jpeg: bytes, prompt: str, schema: dict[str, Any], *, effort: str
) -> dict[str, Any] | None:
    """Same with an anthropic.AsyncAnthropic client."""
    response = await client.beta.messages.create(**build_request(jpeg, prompt, schema, effort=effort))
    return parse_json(response)
