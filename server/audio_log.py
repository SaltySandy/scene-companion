"""Intended audio output log: laptop send → phone lifecycle.

Each ``say`` / clip gets an ``id`` that rides in the protocol message. The
phone reports ``received`` / ``queued`` / ``started`` / ``done`` /
``interrupted`` / ``error`` / ``tts_not_ready`` for that id. Older phones
that never report leave the row as ``sent, no phone report``.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_ENTRIES = 200


def summarize(
    *,
    phone_connected: bool,
    send_ok: bool,
    last_event: str | None,
    muted: bool | None = None,
    volume: int | None = None,
    error_code: int | None = None,
) -> str:
    """One-line triage: software path vs volume/hardware."""
    if not phone_connected:
        return "no phone connected"
    if not send_ok:
        return "send failed"
    if last_event is None:
        return "sent, no phone report"
    if last_event == "tts_not_ready":
        return "tts not ready"
    if last_event == "error":
        return f"error ({error_code})" if error_code is not None else "error"
    if last_event == "interrupted":
        return "interrupted"
    if last_event == "received":
        return "received, never started"
    if last_event == "queued":
        return "received, queued"
    silent = bool(muted) or (volume is not None and volume <= 0)
    if last_event == "started":
        return "started, silent" if silent else "started"
    if last_event == "done":
        return "started, done, but silent" if silent else "started, done"
    return last_event


@dataclass
class AudioEntry:
    id: str
    kind: str
    text: str
    urgent: bool
    hazard: bool
    sent_ms: int
    phone_connected: bool
    send_ok: bool
    events: list[str] = field(default_factory=list)
    volume: int | None = None
    muted: bool | None = None
    error_code: int | None = None
    mime: str | None = None

    @property
    def last_event(self) -> str | None:
        return self.events[-1] if self.events else None

    def summary(self) -> str:
        return summarize(
            phone_connected=self.phone_connected,
            send_ok=self.send_ok,
            last_event=self.last_event,
            muted=self.muted,
            volume=self.volume,
            error_code=self.error_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "urgent": self.urgent,
            "hazard": self.hazard,
            "sent_ms": self.sent_ms,
            "phone_connected": self.phone_connected,
            "send_ok": self.send_ok,
            "status": self.last_event,
            "phone_events": list(self.events),
            "volume": self.volume,
            "muted": self.muted,
            "error_code": self.error_code,
            "mime": self.mime,
            "summary": self.summary(),
        }


class AudioLog:
    def __init__(self, maxlen: int = MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._entries: deque[AudioEntry] = deque(maxlen=maxlen)
        self._by_id: dict[str, AudioEntry] = {}
        self._seq = 0

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_id.clear()
            self._seq = 0

    def next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"a{self._seq:04d}"

    def record(
        self,
        *,
        audio_id: str,
        kind: str,
        text: str,
        urgent: bool,
        hazard: bool,
        sent_ms: int,
        phone_connected: bool,
        send_ok: bool,
        mime: str | None = None,
    ) -> AudioEntry:
        entry = AudioEntry(
            id=audio_id,
            kind=kind,
            text=text,
            urgent=urgent,
            hazard=hazard,
            sent_ms=sent_ms,
            phone_connected=phone_connected,
            send_ok=send_ok,
            mime=mime,
        )
        with self._lock:
            self._entries.append(entry)
            self._by_id[audio_id] = entry
            extra = set(self._by_id) - {e.id for e in self._entries}
            for key in extra:
                self._by_id.pop(key, None)
        return entry

    def note_event(
        self,
        audio_id: str,
        event: str,
        *,
        volume: int | None = None,
        muted: bool | None = None,
        error_code: int | None = None,
    ) -> AudioEntry | None:
        with self._lock:
            entry = self._by_id.get(str(audio_id))
            if entry is None:
                return None
            entry.events.append(str(event))
            if volume is not None:
                entry.volume = int(volume)
            if muted is not None:
                entry.muted = bool(muted)
            if error_code is not None:
                entry.error_code = int(error_code)
            return entry

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = [e.to_dict() for e in self._entries]
        return {"ok": True, "n": len(entries), "entries": entries}

    def get(self, audio_id: str) -> AudioEntry | None:
        with self._lock:
            return self._by_id.get(audio_id)


audio_log = AudioLog()
