"""Optional Snowflake event log. Off unless the env vars are set.

Writes never run on the camera path: ``log_event`` only enqueues. A background
thread batch-inserts. A full queue drops events. Nothing here raises into
the WebSocket stream.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("saw.snowflake")

TABLE = "SCENE_EVENTS"
QUEUE_MAX = 500
BATCH_SIZE = 40
FLUSH_SEC = 0.5
RECALL_LIMIT = 20
DEFAULT_MODEL = "llama3.1-8b"


def _meta_json(meta: Any) -> str | None:
    if meta is None:
        return None
    try:
        return json.dumps(meta, default=str, separators=(",", ":"))
    except Exception:
        return "{}"


class SnowflakeStore:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        connect: Callable[[], Any] | None = None,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        source = env if env is not None else os.environ
        self.account = (source.get("SNOWFLAKE_ACCOUNT") or "").strip()
        self.user = (source.get("SNOWFLAKE_USER") or "").strip()
        self.password = source.get("SNOWFLAKE_PASSWORD") or ""
        self.private_key_path = (source.get("SNOWFLAKE_PRIVATE_KEY_PATH") or "").strip()
        self.warehouse = (source.get("SNOWFLAKE_WAREHOUSE") or "").strip()
        self.database = (source.get("SNOWFLAKE_DATABASE") or "").strip()
        self.schema = (source.get("SNOWFLAKE_SCHEMA") or "").strip()
        self.role = (source.get("SNOWFLAKE_ROLE") or "").strip()
        self.model = (source.get("SNOWFLAKE_CORTEX_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._connect_override = connect
        self.enabled = bool(self.account and self.user and (self.password or self.private_key_path))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0

    def start(self) -> None:
        if not self.enabled:
            log.info("Snowflake is disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="saw-snowflake", daemon=True)
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
            log.debug("snowflake enqueue failed", exc_info=True)

    def ensure_schema(self, conn: Any) -> None:
        cur = conn.cursor()
        try:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    ts TIMESTAMP_TZ,
                    session_id VARCHAR,
                    kind VARCHAR,
                    text VARCHAR,
                    hazard BOOLEAN,
                    meta VARIANT
                )
                """
            )
            conn.commit()
        finally:
            _close(cur)

    def recall(self, question: str, limit: int = RECALL_LIMIT) -> str:
        if not self.enabled:
            raise RuntimeError("Snowflake is disabled")
        conn = self.connect()
        try:
            self.ensure_schema(conn)
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT ts, kind, text, hazard
                    FROM {TABLE}
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall() or []
                lines = [_format_row(row) for row in rows]
                context = "\n".join(lines) if lines else "(no events yet)"
                prompt = (
                    "You help a blind pedestrian. Use only these recent events. "
                    "Answer in one or two short sentences.\n"
                    f"{context}\nQuestion: {question}"
                )
                # Cortex Search later:
                # cur.execute(
                #     "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)",
                #     (os.environ["SNOWFLAKE_CORTEX_SEARCH_SERVICE"], question),
                # )
                cur.execute(
                    "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)",
                    (self.model, prompt),
                )
                found = cur.fetchone()
            finally:
                _close(cur)
        finally:
            _close(conn)
        if not found:
            return ""
        value = found[0]
        return "" if value is None else str(value)

    def connect(self) -> Any:
        if self._connect_override is not None:
            return self._connect_override()
        import snowflake.connector

        kwargs: dict[str, Any] = {"account": self.account, "user": self.user}
        if self.warehouse:
            kwargs["warehouse"] = self.warehouse
        if self.database:
            kwargs["database"] = self.database
        if self.schema:
            kwargs["schema"] = self.schema
        if self.role:
            kwargs["role"] = self.role
        if self.private_key_path:
            kwargs["private_key"] = _private_key_der(self.private_key_path)
        elif self.password:
            kwargs["password"] = self.password
        else:
            raise RuntimeError("Snowflake is disabled")
        return snowflake.connector.connect(**kwargs)

    def _run(self) -> None:
        try:
            conn = self.connect()
            self.ensure_schema(conn)
        except Exception:
            log.exception("snowflake setup failed")
            return
        try:
            while not self._stop.is_set():
                batch = self._take_batch()
                if not batch:
                    continue
                try:
                    self._insert(conn, batch)
                except Exception:
                    log.exception("snowflake insert failed")
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
                f"""
                INSERT INTO {TABLE} (ts, session_id, kind, text, hazard, meta)
                SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s)
                """,
                rows,
            )
            conn.commit()
        finally:
            _close(cur)


def _format_row(row: Any) -> str:
    ts, kind, text, hazard = row[0], row[1], row[2], row[3]
    flag = " hazard" if hazard else ""
    return f"{ts} {kind}{flag}: {text}"


def _private_key_der(path: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _close(obj: Any) -> None:
    try:
        obj.close()
    except Exception:
        pass
