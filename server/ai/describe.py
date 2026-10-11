"""Spoken "what is it" for a new obstacle, from Claude vision.

Proximity tones are the primary signal; this adds a few words ("open door
ahead") when something new comes close. It never blocks the heartbeat: the
call runs as a background task, at most one at a time with a cooldown, and an
answer that arrives late or after the sector cleared is dropped.
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
_disabled_reason: str | None = None


def available() -> bool:
    return ENABLED and _disabled_reason is None


async def describe(jpeg: bytes, sector: str) -> str | None:
    """One short label for the obstacle in `sector`, or None. Disables itself on auth errors."""
    global _client, _disabled_reason
    import anthropic

    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=0, timeout=8.0)
    try:
        answer = await vlm.ask_json_async(_client, jpeg, prompt(sector), SCHEMA, effort="low")
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        _disabled_reason = f"auth: {exc.status_code}"
        log.warning("describe disabled for this session (%s); set ANTHROPIC_API_KEY", _disabled_reason)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("describe failed: %s %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("describe failed: %s", exc)
        return None
    except Exception as exc:  # anthropic may raise before any request, e.g. no credentials
        _disabled_reason = f"{type(exc).__name__}: {exc}"
        log.warning("describe disabled for this session (%s)", _disabled_reason)
        return None
    return clean(str(answer.get("label", ""))) if answer else None


def monotonic() -> float:
    return time.monotonic()
