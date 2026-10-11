"""Spoken "what is it" for a new obstacle, from Gemini or Claude vision.

Proximity tones are the primary signal; this adds a few words ("open door
ahead") when something new comes close. It never blocks the heartbeat: the
call runs as a background task, at most one at a time with a cooldown, and an
answer that arrives late or after the sector cleared is dropped.

Prefers GEMINI_API_KEY; falls back to ANTHROPIC_API_KEY. Client construction
happens in warmup() on a worker thread so the first obstacle does not stall
the event loop.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from server.ai import vlm

log = logging.getLogger("saw.describe")

ENABLED = (os.environ.get("SAW_DESCRIBE") or "1").strip() != "0"
COOLDOWN_S = float(os.environ.get("SAW_DESCRIBE_COOLDOWN_S") or 6.0)
MAX_AGE_S = float(os.environ.get("SAW_DESCRIBE_MAX_AGE_S") or 4.0)
WHERE = {"left": "on your left", "center": "ahead", "right": "on your right"}
SEVERITY = {"clear": 0, "near": 1, "danger": 2}

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}


def prompt(sector: str) -> str:
    part = {"left": "left third", "center": "middle third", "right": "right third"}[sector]
    return (
        "This photo is from a chest-mounted camera of a visually impaired person who is walking. "
        f"Something is close in the {part} of the image. Name the nearest obstacle there in at most "
        'four plain words, for example "open door", "person", "chair", "glass wall", "stairs down". '
        'If you cannot tell, answer "obstacle".'
    )


def clean(label: str) -> str | None:
    words = re.sub(r"[^\w\s'-]", "", label).split()[:4]
    text = " ".join(words).lower()
    return text if text and text != "none" else None


class DescribeGate:
    """Decides when a frame shows a new obstacle worth naming."""

    def __init__(self, cooldown_s: float = COOLDOWN_S) -> None:
        self.cooldown_s = cooldown_s
        self.in_flight = False
        self.last_start = -1e9

    def check(self, prev: dict[str, str], levels: dict[str, str], now: float) -> str | None:
        """Sector to describe, or None. Center counts at near; sides only at danger."""
        if self.in_flight or now - self.last_start < self.cooldown_s:
            return None
        for sector, floor in (("center", "near"), ("left", "danger"), ("right", "danger")):
            new, old = levels.get(sector, "clear"), prev.get(sector, "clear")
            if SEVERITY[new] >= SEVERITY[floor] and SEVERITY[new] > SEVERITY[old]:
                self.in_flight = True
                self.last_start = now
                return sector
        return None

    def done(self) -> None:
        self.in_flight = False


_client: Any = None
_provider: str | None = None
_disabled_reason: str | None = None


def warmup() -> None:
    """Import the SDK and create the client. Blocking: call from a thread, not the event loop."""
    global _client, _provider
    if not ENABLED or _client is not None:
        return
    _client, _provider = vlm.make_client()
    if _provider:
        log.info("describe using %s (%s)", _provider, vlm.model_name(_provider))


def available() -> bool:
    return ENABLED and _disabled_reason is None


def status() -> str:
    """Short label for /health fov.describe."""
    if not ENABLED:
        return "off (SAW_DESCRIBE=0)"
    if _disabled_reason:
        return f"disabled ({_disabled_reason})"
    which = _provider or vlm.active_provider()
    if which:
        return f"on ({which})"
    return "idle (no GEMINI_API_KEY or ANTHROPIC_API_KEY; first obstacle will disable it)"


def _on_error(exc: BaseException) -> None:
    """Auth / missing-credentials disable the feature; transient API errors do not."""
    global _disabled_reason
    name = type(exc).__name__
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    auth_names = {"AuthenticationError", "PermissionDeniedError"}
    transient_names = {"APIStatusError", "APIConnectionError", "ServerError", "APIError", "ClientError"}
    if name in auth_names or status_code in (401, 403):
        _disabled_reason = f"auth: {status_code or name}"
        log.warning(
            "describe disabled for this session (%s); set GEMINI_API_KEY or ANTHROPIC_API_KEY",
            _disabled_reason,
        )
        return
    if name in transient_names:
        log.warning("describe failed: %s %s", status_code or name, message)
        return
    _disabled_reason = f"{name}: {exc}"
    log.warning("describe disabled for this session (%s)", _disabled_reason)


async def describe(jpeg: bytes, sector: str) -> str | None:
    """One short label for the obstacle in `sector`, or None. Disables itself on auth errors.

    Does not import SDKs or construct clients: that is warmup()'s job.
    """
    global _disabled_reason
    if _client is None:
        # No key (or warmup never ran). Stay off the event loop; disable like a missing key.
        _disabled_reason = "no API key"
        log.warning("describe disabled for this session (%s); set GEMINI_API_KEY or ANTHROPIC_API_KEY", _disabled_reason)
        return None
    try:
        answer = await vlm.ask_json_async(_client, jpeg, prompt(sector), SCHEMA, effort="low")
    except Exception as exc:
        _on_error(exc)
        return None
    return clean(str(answer.get("label", ""))) if answer else None


def monotonic() -> float:
    return time.monotonic()
