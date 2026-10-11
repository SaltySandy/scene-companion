"""Per-session proximity traces. JSONL under server/traces/ (gitignored)."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("saw.trace")

TRACES_DIR = Path(__file__).resolve().parent / "traces"
JPEG_EVERY = int(os.environ.get("SAW_TRACE_JPEG_EVERY", "10"))
MAX_ROWS = int(os.environ.get("SAW_TRACE_MAX_ROWS", "250"))


class ProximityTrace:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or TRACES_DIR
        self._lock = threading.Lock()
        self.rows: deque[dict[str, Any]] = deque(maxlen=MAX_ROWS)
        self.path: Path | None = None
        self.session_id: str | None = None
        self.n = 0

    def start_session(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.directory / f"session-{stamp}.jsonl"
        with self._lock:
            self.session_id = stamp
            self.path = path
            self.rows.clear()
            self.n = 0
        log.info("proximity trace %s", path)
        return path

    def record(self, row: dict[str, Any], jpeg: bytes | None = None) -> dict[str, Any]:
        with self._lock:
            self.n += 1
            stored = dict(row)
            stored["i"] = self.n
            stored["session"] = self.session_id
            self.rows.append(stored)
            if self.path is not None:
                try:
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(stored, separators=(",", ":")) + "\n")
                except OSError as exc:
                    log.warning("trace write failed: %s", exc)
                every = JPEG_EVERY
                if jpeg and every > 0 and self.n % every == 0:
                    jpg = self.path.with_name(f"{self.path.stem}-{self.n:05d}.jpg")
                    try:
                        jpg.write_bytes(jpeg)
                    except OSError as exc:
                        log.warning("trace jpeg failed: %s", exc)
            return stored

    def recent(self, n: int = 80) -> list[dict[str, Any]]:
        limit = max(1, min(int(n), MAX_ROWS))
        with self._lock:
            return list(self.rows)[-limit:]

    def snapshot(self, n: int = 80) -> dict[str, Any]:
        rows = self.recent(n)
        return {
            "ok": True,
            "session": self.session_id,
            "path": None if self.path is None else str(self.path.name),
            "n": self.n,
            "rows": rows,
        }

    def reset(self) -> None:
        with self._lock:
            self.rows.clear()
            self.path = None
            self.session_id = None
            self.n = 0


tracer = ProximityTrace()
