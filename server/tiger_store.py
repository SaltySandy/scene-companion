"""Optional TigerData (Timescale) event log. Off unless TIGER_DATABASE_URL is set.

Same contract as the Snowflake store: enqueue only, batch insert on a
background thread, drop when the queue is full, never raise into the stream.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger("saw.tiger")

QUEUE_MAX = 500
BATCH_SIZE = 40
FLUSH_SEC = 0.5
RECENT_LIMIT = 50


def _meta_json(meta: Any) -> str | None:
    if meta is None:
        return None
    try:
        return json.dumps(meta, default=str, separators=(",", ":"))
    except Exception:
        return "{}"


class TigerStore:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        connect: Callable[[], Any] | None = None,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        source = env if env is not None else os.environ
        self.url = (source.get("TIGER_DATABASE_URL") or "").strip()
        self._connect_override = connect
        self.enabled = bool(self.url)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0

    def start(self) -> None:
        if not self.enabled:
            log.info("TigerData is disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="saw-tiger", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def log_event(
        self,
        kind: str,
        text: str,
        *,
        hazard: bool | None = None,
        meta: Any = None,
        session_id: str = "",
        ts: datetime | None = None,
    ) -> None:
        if not self.enabled:
            return
        item = {
            "ts": ts or datetime.now(timezone.utc),
            "session_id": session_id,
            "kind": kind,
            "text": text,
            "hazard": hazard,
            "meta": _meta_json(meta),
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1
        except Exception:
            log.debug("tiger enqueue failed", exc_info=True)

    def ensure_schema(self, conn: Any) -> None:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS scene_events (
                    ts timestamptz NOT NULL,
                    session_id text,
                    kind text,
                    text text,
                    hazard boolean,
                    meta jsonb
                )
                """
            )
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                cur.execute(
                    "SELECT create_hypertable('scene_events', 'ts', if_not_exists => TRUE)"
                )
            except Exception:
                log.info("create_hypertable unavailable, using a plain table")
            conn.commit()
        finally:
            _close(cur)

    def recent_events(self, window: timedelta, limit: int = RECENT_LIMIT, conn: Any | None = None) -> list[tuple]:
        if not self.enabled:
            raise RuntimeError("TigerData is disabled")
        own = conn is None
        if own:
            conn = self.connect()
        cutoff = datetime.now(timezone.utc) - window
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT ts, kind, text, hazard
                FROM scene_events
                WHERE ts >= %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (cutoff, limit),
            )
            return list(cur.fetchall() or [])
        finally:
            _close(cur)
            if own:
                _close(conn)

    def recall(self, question: str, window: timedelta | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("TigerData is disabled")
        span = window or timedelta(minutes=30)
        conn = self.connect()
        try:
            self.ensure_schema(conn)
            rows = self.recent_events(span, conn=conn)
        finally:
            _close(conn)
        # pgvector / pgai later:
        # ALTER TABLE scene_events ADD COLUMN IF NOT EXISTS embedding vector(768);
        # SELECT text FROM scene_events
        # ORDER BY embedding <=> ai.embedding_openai('text-embedding-3-small', question)
        # LIMIT 8;
        return _summarize(question, rows)

    def connect(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override()
        if not self.url:
            raise RuntimeError("TigerData is disabled")
        import psycopg

        return psycopg.connect(self.url)

    def _run(self) -> None:
        try:
            conn = self.connect()
            self.ensure_schema(conn)
        except Exception:
            log.exception("tiger setup failed")
            return
        try:
            while not self._stop.is_set():
                batch = self._take_batch()
                if not batch:
                    continue
                try:
                    self._insert(conn, batch)
                except Exception:
                    log.exception("tiger insert failed")
        finally:
            _close(conn)

    def _take_batch(self) -> list[dict[str, Any]]:
        try:
            first = self._queue.get(timeout=FLUSH_SEC)
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _insert(self, conn: Any, batch: list[dict[str, Any]]) -> None:
        rows = [
            (item["ts"], item["session_id"], item["kind"], item["text"], item["hazard"], item["meta"])
            for item in batch
        ]
        cur = conn.cursor()
        try:
            cur.executemany(
                """
                INSERT INTO scene_events (ts, session_id, kind, text, hazard, meta)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                rows,
            )
            conn.commit()
        finally:
            _close(cur)


def _summarize(question: str, rows: list[tuple]) -> str:
    if not rows:
        return "No recent events."
    words = {w.lower() for w in question.split() if len(w) > 2}
    picked = []
    for row in rows:
        text = str(row[2] or "")
        if words and any(word in text.lower() for word in words):
            picked.append(row)
    chosen = picked or rows[:8]
    lines = []
    for ts, kind, text, hazard in chosen:
        flag = " hazard" if hazard else ""
        lines.append(f"{ts} {kind}{flag}: {text}")
    return "Recent events:\n" + "\n".join(lines)


def _close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass
