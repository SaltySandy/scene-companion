"""AI hook — put vision / LLM logic here.

``on_frame`` is called from a latest-frame worker: a slow model never stalls
the WebSocket. If frames arrive faster than you can think, older ones are
dropped and only the newest JPEG is processed.

Return a ``Say`` / ``Play`` (or a list of them) to talk back to the phone,
or call ``await say(...)`` / ``await play(...)`` from ``broadcast``.

The default implementation is a demo stub that proves the loop: every few
seconds it speaks ``frame received, N per second``.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Sequence

# ---------------------------------------------------------------------------
# Response types the worker understands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Say:
    text: str
    urgent: bool = False


@dataclass(frozen=True)
class Play:
    audio_bytes: bytes
    mime: str = "audio/wav"
    urgent: bool = False


Response = Say | Play | Sequence[Say | Play] | None

# Demo: announce roughly every N seconds so testers hear the loop.
# Off by default when the Gemini wall detector is on, so it doesn't talk over alerts.
_default_announce = "0" if (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")) else "5"
ANNOUNCE_EVERY_SEC = float(os.environ.get("SAW_DEMO_ANNOUNCE_SEC", _default_announce))

_frame_times: deque[float] = deque()
_last_announce = 0.0


async def on_frame(jpeg_bytes: bytes, ts: int) -> Response:
    """Called with the latest JPEG. Keep this non-blocking or await I/O.

    ``jpeg_bytes`` is a complete JPEG. ``ts`` is the phone's capture time in
    Unix epoch milliseconds (clock skew vs the laptop is expected).
    """
    del jpeg_bytes  # demo stub does not inspect pixels
    now = time.time()
    _frame_times.append(now)
    cutoff = now - 1.0
    while _frame_times and _frame_times[0] < cutoff:
        _frame_times.popleft()
    fps = len(_frame_times)

    global _last_announce
    if ANNOUNCE_EVERY_SEC > 0 and now - _last_announce >= ANNOUNCE_EVERY_SEC:
        _last_announce = now
        return Say(f"frame received, {fps} per second")

    # --- Vision LLM example (commented; needs a key, not required to run) ---
    # import base64
    # import httpx
    #
    # api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    # if not api_key:
    #     return None
    #
    # # OpenAI-compatible vision (OPENAI_BASE_URL defaults to api.openai.com)
    # if os.environ.get("OPENAI_API_KEY"):
    #     b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    #     url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    #     model = os.environ.get("SAW_VISION_MODEL", "gpt-4o-mini")
    #     async with httpx.AsyncClient(timeout=20) as client:
    #         r = await client.post(
    #             f"{url.rstrip('/')}/chat/completions",
    #             headers={"Authorization": f"Bearer {api_key}"},
    #             json={
    #                 "model": model,
    #                 "messages": [{
    #                     "role": "user",
    #                     "content": [
    #                         {"type": "text", "text": "Describe hazards and nearby objects for a blind pedestrian in one short sentence."},
    #                         {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    #                     ],
    #                 }],
    #                 "max_tokens": 80,
    #             },
    #         )
    #         r.raise_for_status()
    #         text = r.json()["choices"][0]["message"]["content"]
    #         return Say(text)
    #
    # # Gemini (GEMINI_API_KEY)
    # # import google.generativeai as genai  # pip install google-generativeai
    # # genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    # # model = genai.GenerativeModel(os.environ.get("SAW_VISION_MODEL", "gemini-2.0-flash"))
    # # # run in a thread if the SDK is sync: result = await asyncio.to_thread(model.generate_content, [...])
    # # return Say(result.text)

    return None


async def on_transcript(text: str, is_final: bool, ts: int) -> None:
    """Question hook. ``text`` is a partial or final utterance. ``ts`` is capture time in ms.

    The default stub proves the loop: finals are spoken back. Replace this
    with the real assistant. Do not do slow work before the first await if
    you can help it; the caller already runs off the camera thread.
    """
    import logging

    logging.getLogger("saw.transcribe").info("transcript final=%s ts=%s: %s", is_final, ts, text)
    if not is_final or not text.strip():
        return
    from server.event_sinks import sinks
    from server.hub import say

    sinks.log_event("transcript", text.strip(), meta={"ts": ts, "final": True})

    from server.scene_assistant import assistant

    question = text.strip()
    if assistant.enabled and len(question) >= 3:
        # Gemini looks at the current frame; Nimble adds web facts when useful.
        assistant.ask(question)
    else:
        await say(f"You said: {question}")


def reset_demo_state() -> None:
    """Test helper."""
    global _last_announce
    _frame_times.clear()
    _last_announce = 0.0
