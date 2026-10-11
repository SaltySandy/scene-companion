"""Spoken-intent matcher for "what's in front of me".

Keep this a regex over normalized STT text — no LLM. Add a line to
``DESCRIBE_SCENE_PATTERNS`` to teach a new phrase.
"""

from __future__ import annotations

import os
import re
import unicodedata

# Optional words Whisper often prepends. Stripped before matching.
_FILLER = re.compile(
    r"^(?:(?:please|um+|uh+|er+|ah+|hey|so|okay|ok|like|well|"
    r"can you|could you|would you|will you)\s+)+"
)

# Each pattern is searched against normalized text (lowercase, no punctuation).
# Keep them readable: a new phrase is one more raw string.
DESCRIBE_SCENE_PATTERNS: tuple[str, ...] = (
    r"(?:tell me )?(?:what(?:s| is)) in front(?: of(?: me| us| here|(?: (?:the|a|an))? \w+){0,5})?",
    r"(?:tell me )?(?:what(?:s| is)) ahead(?: of me)?",
    r"what do you see",
    r"what can you see",
    r"describe (?:what(?:s| is) )?(?:ahead|in front(?: of me)?|the (?:scene|view))",
    r"what am i looking at",
    r"tell me what (?:you see|i(?:m| am)? looking at)",
)

_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{pat}\b") for pat in DESCRIBE_SCENE_PATTERNS
)


def listen_default() -> bool:
    """Demo default: Listen/mic ON unless SAW_LISTEN_DEFAULT is off."""
    raw = os.environ.get("SAW_LISTEN_DEFAULT", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def normalize(text: str) -> str:
    """Lowercase, fold curly quotes, strip punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower().replace("\u2019", "'").replace("`", "'").replace("´", "'")
    text = text.replace("what's", "whats").replace("i'm", "im")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_fillers(text: str) -> str:
    prev = text
    while True:
        nxt = _FILLER.sub("", prev)
        if nxt == prev:
            return prev
        prev = nxt


def is_describe_scene(text: str) -> bool:
    """True when STT looks like a 'what's in front of me' request."""
    norm = strip_fillers(normalize(text))
    if not norm:
        return False
    return any(pat.search(norm) for pat in _COMPILED)
