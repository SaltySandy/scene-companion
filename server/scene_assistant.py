"""Voice question answering: "What is this?" -> Gemini vision + Nimble web search.

Flow for one question
---------------------
1. Grab the newest camera frame.
2. Gemini (vision, generateContent) answers the question about the frame in one
   or two short spoken sentences, and, if it sees something specific (a product,
   brand, sign, book, landmark, artwork, medicine box...), returns a web search
   query for it. The first answer is spoken right away.
3. If there is a search query and NIMBLE_API_KEY is set, Nimble's Search API
   fetches live web results, Gemini condenses them into one or two sentences,
   and the phone speaks a follow-up ("More about it: ...").

Wall alerts stay in charge: they are sent with ``urgent=True`` and cut off any
answer that is being read out. Answers are never urgent.

Voice "what's in front of me" (see ``server/voice_intent.py``) uses this same
path with ``ack=True``: a short spoken "Looking.", a ~6 s describe timeout, and
a cooldown so Whisper duplicates do not re-fire. Prefer the fast vision model
(``gemini-3.5-flash-lite``). Nimble still runs only when Gemini returns a
search query, same as typed "What is this?".

Environment variables
---------------------
GEMINI_API_KEY              required (shared with obstacle describe and the wall detector)
NIMBLE_API_KEY              optional; without it you get step 2 only
SAW_GEMINI_VISION_MODEL     default ``gemini-3.5-flash-lite`` (fast). ``gemini-3.8-flash`` is smarter.
SAW_NIMBLE_MAX_RESULTS      default 5
SAW_ASSISTANT               "0" to turn voice questions off
SAW_DESCRIBE_TIMEOUT_S      default 6 — spoken fallback if Gemini is slower
SAW_DESCRIBE_COOLDOWN_S     default 2.5 — drop repeated describe-scene triggers
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("saw.assistant")

NIMBLE_SEARCH_URL = "https://sdk.nimbleway.com/v2/search"

DESCRIBE_PROMPT = """\
You are the eyes of a blind person. The image is from their phone camera, held \
at chest height and pointed where they are facing. They asked: "{question}"

Answer for someone who cannot see:
- Lead with the direct answer. Max 2 short sentences, under 35 words.
- Name the specific thing if you can (brand, product, title, sign text).
- Read short visible text aloud when it helps (labels, signs, screens).
- Mention a hazard first if there is one (stairs, traffic, obstacle).
- If the image is too dark or blurry, say so and suggest moving the phone.
- No "the image shows" or "I can see". Speak naturally, like a friend.

Also decide whether a web search would add useful facts the user would want, \
e.g. what a product is for, a book's summary, a medication's purpose, a \
landmark's history, a restaurant's hours. If yes, give a precise search query \
naming the exact item (include brand and any visible text). If the scene is \
generic (a room, a street, a person, a wall), leave search_query empty.
"""

SUMMARIZE_PROMPT = """\
A blind user pointed their camera at something and asked: "{question}"
It was identified as: "{answer}"

Here are live web search results about it:
{results}

Write ONE or TWO short spoken sentences (under 40 words) with the most useful \
extra facts for them (what it is for, key details, warnings). Do not repeat \
the identification, do not mention search results, links or sources. If the \
results are unrelated or unhelpful, reply with exactly: NONE
"""


@dataclass
class Description:
    answer: str
    search_query: str = ""


SpeakFn = Callable[[str, bool], Awaitable[None]]
FrameFn = Callable[[], "tuple[bytes, int] | None"]
StatusFn = Callable[[dict], Awaitable[None]]

LOOKING_ACK = "Looking."
UNAVAILABLE = "Description isn't available right now."
TIMEOUT_FALLBACK = "I couldn't describe that in time."
NO_FRAME = "I can't see anything yet. Press Start so the camera is on."
DEFAULT_TIMEOUT_S = 6.0
DEFAULT_COOLDOWN_S = 2.5


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Backend calls (module-level so tests can monkeypatch them)
# ---------------------------------------------------------------------------

_client = None


def _gemini():
    global _client
    if _client is None:
        from google import genai
        from server.ai.vlm import gemini_key

        _client = genai.Client(api_key=gemini_key())
    return _client


def vision_model() -> str:
    return os.environ.get("SAW_GEMINI_VISION_MODEL", "gemini-3.5-flash-lite").strip()


async def describe(question: str, jpeg: bytes) -> Description:
    from google.genai import types

    resp = await _gemini().aio.models.generate_content(
        model=vision_model(),
        contents=[
            types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
            DESCRIBE_PROMPT.format(question=question),
        ],
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=300,
            response_mime_type="application/json",
            response_schema=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "answer": types.Schema(type=types.Type.STRING),
                    "search_query": types.Schema(type=types.Type.STRING),
                },
                required=["answer", "search_query"],
            ),
        ),
    )
    return parse_description(resp.text or "")


def parse_description(text: str) -> Description:
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return Description(
                answer=str(obj.get("answer") or "").strip(),
                search_query=str(obj.get("search_query") or "").strip(),
            )
    except json.JSONDecodeError:
        pass
    return Description(answer=text)


async def nimble_search(query: str) -> list[dict]:
    key = os.environ.get("NIMBLE_API_KEY", "").strip()
    if not key:
        return []
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.post(
            NIMBLE_SEARCH_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "query": query,
                "search_depth": "lite",
                "max_results": _env_int("SAW_NIMBLE_MAX_RESULTS", 5),
                "output_format": "plain_text",
            },
        )
        r.raise_for_status()
        data = r.json()
    results = data.get("results") or []
    return [r for r in results if isinstance(r, dict)]


def format_results(results: list[dict], limit: int = 5) -> str:
    lines = []
    for i, r in enumerate(results[:limit], 1):
        title = str(r.get("title") or "").strip()
        desc = str(r.get("description") or r.get("content") or "").strip().replace("\n", " ")
        lines.append(f"{i}. {title} — {desc[:400]}")
    return "\n".join(lines)


async def summarize(question: str, answer: str, results: list[dict]) -> str:
    from google.genai import types

    resp = await _gemini().aio.models.generate_content(
        model=vision_model(),
        contents=SUMMARIZE_PROMPT.format(question=question, answer=answer, results=format_results(results)),
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=200),
    )
    text = (resp.text or "").strip()
    return "" if text.upper().startswith("NONE") else text


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class SceneAssistant:
    def __init__(self) -> None:
        from server.ai.vlm import gemini_key

        has_key = bool(gemini_key())
        self.enabled = has_key and os.environ.get("SAW_ASSISTANT", "1").strip().lower() not in {"0", "false", "off", "no"}
        self.nimble_enabled = bool(os.environ.get("NIMBLE_API_KEY", "").strip())
        self.timeout_s = _env_float("SAW_DESCRIBE_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        self.cooldown_s = _env_float("SAW_DESCRIBE_COOLDOWN_S", DEFAULT_COOLDOWN_S)
        self._speak: SpeakFn | None = None
        self._frame: FrameFn | None = None
        self._status: StatusFn | None = None
        self._task: asyncio.Task | None = None
        self._last_trigger = 0.0

    def bind(self, speak: SpeakFn, frame: FrameFn, status: StatusFn | None = None) -> None:
        self._speak, self._frame, self._status = speak, frame, status

    def reset_triggers(self) -> None:
        """Test helper: drop cooldown and cancel an in-flight answer."""
        self._last_trigger = 0.0
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def _in_cooldown(self) -> bool:
        return (time.monotonic() - self._last_trigger) < self.cooldown_s

    def ask(self, question: str, *, ack: bool = False, timeout_s: float | None = None) -> asyncio.Task | None:
        """Start answering. A newer question cancels an older one still running.

        ``ack=True`` is the voice "what's in front of me" path: spoken Looking.,
        cooldown, timeout, and a spoken fallback when the assistant is off.
        """
        question = (question or "").strip()
        if not question:
            return None
        if ack and self._in_cooldown():
            log.info("describe-scene skipped (cooldown)")
            return None
        if ack:
            self._last_trigger = time.monotonic()
        if not self.enabled:
            if ack:
                if self._task and not self._task.done():
                    self._task.cancel()
                self._task = asyncio.create_task(self._say(UNAVAILABLE), name="saw-assistant")
                return self._task
            return None
        if self._task and not self._task.done():
            self._task.cancel()
        limit = self.timeout_s if timeout_s is None else timeout_s
        self._task = asyncio.create_task(
            self._answer(question, ack=ack, timeout_s=limit),
            name="saw-assistant",
        )
        return self._task

    async def _say(self, text: str) -> None:
        if self._speak and text:
            await self._speak(text, False)

    async def _emit(self, **payload) -> None:
        if self._status:
            try:
                await self._status(payload)
            except Exception:
                log.debug("assistant status failed", exc_info=True)

    async def _answer(self, question: str, *, ack: bool = False, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        t0 = time.monotonic()
        frame = self._frame() if self._frame else None
        if frame is None:
            await self._say(NO_FRAME)
            return
        jpeg, _ts = frame
        if ack:
            await self._say(LOOKING_ACK)
        await self._emit(stage="looking", question=question)
        try:
            if timeout_s and timeout_s > 0:
                desc = await asyncio.wait_for(describe(question, jpeg), timeout=timeout_s)
            else:
                desc = await describe(question, jpeg)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("Gemini describe timed out after %.1fs", timeout_s)
            await self._emit(stage="error", question=question, error="timeout")
            await self._say(TIMEOUT_FALLBACK)
            return
        except Exception as exc:
            log.warning("Gemini describe failed: %s", exc)
            await self._emit(stage="error", question=question, error=str(exc)[:200])
            await self._say("Sorry, I couldn't look at that right now.")
            return
        log.info("assistant answer (%.1fs): %s | search=%r", time.monotonic() - t0, desc.answer, desc.search_query)
        await self._emit(stage="answer", question=question, answer=desc.answer, search_query=desc.search_query)
        await self._say(desc.answer or "I'm not sure what that is. Try holding the phone a little closer.")

        if not desc.search_query or not self.nimble_enabled:
            return
        try:
            results = await nimble_search(desc.search_query)
            if not results:
                return
            extra = await summarize(question, desc.answer, results)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Nimble follow-up failed: %s", exc)
            await self._emit(stage="error", question=question, error=f"nimble: {str(exc)[:180]}")
            return
        if extra:
            log.info("assistant nimble (%.1fs): %s", time.monotonic() - t0, extra)
            await self._emit(
                stage="more",
                question=question,
                more=extra,
                search_query=desc.search_query,
                sources=[r.get("url") for r in results[:3]],
            )
            await self._say("More about it: " + extra)


assistant = SceneAssistant()
