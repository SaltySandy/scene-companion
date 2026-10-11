"""Per-frame JSONL recorder. Off unless SAW_RECORD=1.

Writes sessions/<session_id>/frames.jsonl (one FrameRecord per processed frame)
and, unless SAW_RECORD_JPEG=0, the frame itself as frames/<frame_key>.jpg.
The event loop only enqueues; a background thread does all disk I/O. When the
queue is full the record is dropped and counted, never waited on.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
from pathlib import Path
from typing import IO

from server.frame_record import FrameRecord

log = logging.getLogger("saw.recorder")

QUEUE_MAX = 300  # ~30 s at 10 fps; bounds memory when JPEGs are in flight
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "sessions"


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def new_session_id() -> str:
    """Sortable by start time, unique enough for one laptop."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class Recorder:
    def __init__(
        self,
        root: Path,
        session_id: str,
        enabled: bool = True,
        save_jpeg: bool = True,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.enabled = enabled
        self.save_jpeg = save_jpeg
        self._queue: queue.Queue[tuple[FrameRecord, bytes | None] | None] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._file: IO[str] | None = None
        self.records_written = 0
        self.records_dropped = 0

    @classmethod
    def disabled(cls) -> Recorder:
        return cls(root=DEFAULT_ROOT, session_id="", enabled=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Recorder:
        source = env if env is not None else os.environ
        root = (source.get("SAW_SESSIONS_DIR") or "").strip()
        return cls(
            root=Path(root) if root else DEFAULT_ROOT,
            session_id=(source.get("SAW_SESSION_ID") or "").strip() or new_session_id(),
            enabled=_flag(source.get("SAW_RECORD"), False),
            save_jpeg=_flag(source.get("SAW_RECORD_JPEG"), True),
        )

    @property
    def session_dir(self) -> Path:
        return self.root / self.session_id

    @property
    def path(self) -> Path:
        return self.session_dir / "frames.jsonl"

    def start(self) -> None:
        if not self.enabled:
            log.info("recorder is disabled (set SAW_RECORD=1)")
            return
        if self._thread and self._thread.is_alive():
            return
        if self.save_jpeg:
            (self.session_dir / "frames").mkdir(parents=True, exist_ok=True)
        else:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._thread = threading.Thread(target=self._run, name="saw-recorder", daemon=True)
        self._thread.start()
        log.info("recording to %s (jpeg=%s)", self.path, self.save_jpeg)

    def stop(self, timeout: float = 5.0) -> None:
        """Write everything already queued, then close the file."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None  # record() stops accepting from here on
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            log.warning("recorder did not drain in %.1fs", timeout)
            return
        thread.join(timeout=timeout)

    def record(self, rec: FrameRecord, jpeg: bytes | None = None) -> None:
        """Enqueue one record. Never blocks, never raises."""
        if self._thread is None:
            return
        try:
            self._queue.put_nowait((rec, jpeg if self.save_jpeg else None))
        except queue.Full:
            self.records_dropped += 1
        except Exception:
            log.debug("recorder enqueue failed", exc_info=True)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            rec, jpeg = item
            try:
                self._write(rec, jpeg)
            except Exception:
                log.warning("recorder write failed", exc_info=True)
        if self._file is not None:
            self._file.close()
            self._file = None

    def _write(self, rec: FrameRecord, jpeg: bytes | None) -> None:
        # Image first: a reader that sees the line can trust the JPEG exists.
        if jpeg is not None:
            (self.session_dir / "frames" / f"{rec.frame_key}.jpg").write_bytes(jpeg)
        assert self._file is not None
        self._file.write(rec.to_json() + "\n")
        self._file.flush()
        self.records_written += 1
