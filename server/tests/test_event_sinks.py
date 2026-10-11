"""Snowflake and TigerData sinks, with the connector mocked. No real network."""

from __future__ import annotations

import logging
import time

import pytest

from server.event_sinks import EventSinks
from server.snowflake_store import SnowflakeStore
from server.tiger_store import TigerStore


class Cursor:
    def __init__(self, fail_on: str = "") -> None:
        self.sql: list[str] = []
        self.params: list = []
        self.fail_on = fail_on

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("extension missing")

    def executemany(self, sql, params):
        self.sql.append(sql)
        self.params.append(list(params))

    def fetchall(self):
        return [("t0", "say", "curb ahead", False)]

    def fetchone(self):
        return ("curb ahead, slow down",)

    def close(self):
        return None


class Conn:
    def __init__(self, fail_on: str = "") -> None:
        self.cursor_obj = Cursor(fail_on)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


SF_ENV = {
    "SNOWFLAKE_ACCOUNT": "acct",
    "SNOWFLAKE_USER": "user",
    "SNOWFLAKE_PASSWORD": "pw",
    "SNOWFLAKE_WAREHOUSE": "wh",
    "SNOWFLAKE_DATABASE": "db",
    "SNOWFLAKE_SCHEMA": "public",
    "SNOWFLAKE_ROLE": "role",
}


def test_snowflake_disabled_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    store = SnowflakeStore(env={})
    assert store.enabled is False
    with caplog.at_level(logging.INFO):
        store.start()
    store.log_event("say", "hello")
    assert store._queue.qsize() == 0
    assert "Snowflake is disabled" in caplog.text


def test_snowflake_key_path_enables_without_password() -> None:
    store = SnowflakeStore(env={"SNOWFLAKE_ACCOUNT": "a", "SNOWFLAKE_USER": "u", "SNOWFLAKE_PRIVATE_KEY_PATH": "k.p8"})
    assert store.enabled is True


def test_snowflake_queue_drops_when_full() -> None:
    store = SnowflakeStore(env=SF_ENV, queue_max=1)
    store.log_event("say", "one")
    store.log_event("say", "two")
    assert store._queue.qsize() == 1
    assert store.dropped == 1


def test_snowflake_schema_and_recall_use_connector() -> None:
    conn = Conn()
    store = SnowflakeStore(env=SF_ENV, connect=lambda: conn)
    text = store.recall("what is ahead")
    joined = "\n".join(conn.cursor_obj.sql)
    assert "CREATE TABLE IF NOT EXISTS SCENE_EVENTS" in joined
    assert "SNOWFLAKE.CORTEX.COMPLETE" in joined
    assert "SEARCH_PREVIEW" not in joined
    assert text == "curb ahead, slow down"
    assert conn.closed is True


def test_snowflake_worker_batch_inserts() -> None:
    conn = Conn()
    store = SnowflakeStore(env=SF_ENV, connect=lambda: conn)
    store.start()
    try:
        store.log_event("connect", "phone connected", session_id="abc")
        deadline = time.time() + 3
        while time.time() < deadline and not any("INSERT INTO SCENE_EVENTS" in sql for sql in conn.cursor_obj.sql):
            time.sleep(0.05)
        assert any("INSERT INTO SCENE_EVENTS" in sql for sql in conn.cursor_obj.sql)
        assert any("PARSE_JSON" in sql for sql in conn.cursor_obj.sql)
    finally:
        store.stop()


def test_tiger_disabled_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    store = TigerStore(env={})
    assert store.enabled is False
    with caplog.at_level(logging.INFO):
        store.start()
    store.log_event("say", "hello")
    assert "TigerData is disabled" in caplog.text


def test_tiger_hypertable_falls_back_to_plain_table() -> None:
    conn = Conn(fail_on="create_hypertable")
    store = TigerStore(env={"TIGER_DATABASE_URL": "postgresql://example"}, connect=lambda: conn)
    store.ensure_schema(conn)
    joined = "\n".join(conn.cursor_obj.sql)
    assert "CREATE TABLE IF NOT EXISTS scene_events" in joined
    assert "timestamptz" in joined
    assert conn.commits == 1


def test_tiger_recall_reads_recent_rows() -> None:
    conn = Conn()
    store = TigerStore(env={"TIGER_DATABASE_URL": "postgresql://example"}, connect=lambda: conn)
    text = store.recall("curb")
    joined = "\n".join(conn.cursor_obj.sql)
    assert "SELECT ts, kind, text, hazard" in joined
    assert "embedding" not in joined
    assert "curb ahead" in text
    assert conn.closed is True


def test_tiger_worker_batch_inserts() -> None:
    conn = Conn()
    store = TigerStore(env={"TIGER_DATABASE_URL": "postgresql://example"}, connect=lambda: conn)
    store.start()
    try:
        store.log_event("say", "door", hazard=False)
        deadline = time.time() + 3
        while time.time() < deadline and not any("INSERT INTO scene_events" in sql for sql in conn.cursor_obj.sql):
            time.sleep(0.05)
        assert any("INSERT INTO scene_events" in sql for sql in conn.cursor_obj.sql)
    finally:
        store.stop()


def test_fanout_logs_once_to_each_enabled_sink() -> None:
    snow = SnowflakeStore(env=SF_ENV, queue_max=5)
    tiger = TigerStore(env={"TIGER_DATABASE_URL": "postgresql://example"}, queue_max=5)
    sinks = EventSinks(snowflake=snow, tiger=tiger, session_id="sess")
    sinks.log_event("say", "hello", meta={"urgent": True})
    assert snow._queue.qsize() == 1
    assert tiger._queue.qsize() == 1
    assert snow._queue.get_nowait()["session_id"] == "sess"


def test_fanout_swallows_sink_errors() -> None:
    class Boom:
        enabled = True

        def log_event(self, *args, **kwargs):
            raise RuntimeError("nope")

    class Ok:
        def __init__(self) -> None:
            self.n = 0

        def log_event(self, *args, **kwargs):
            self.n += 1

    ok = Ok()
    sinks = EventSinks(snowflake=Boom(), tiger=ok)  # type: ignore[arg-type]
    sinks.log_event("disconnect", "gone")
    assert ok.n == 1


def test_fanout_recall_picks_configured_backend() -> None:
    class Store:
        def __init__(self, name: str, enabled: bool) -> None:
            self.name = name
            self.enabled = enabled

        def recall(self, question: str) -> str:
            return f"{self.name}:{question}"

    both = EventSinks(snowflake=Store("snow", True), tiger=Store("tig", True))  # type: ignore[arg-type]
    assert both.recall("q") == ("snow:q", "snowflake")
    assert both.recall("q", "tiger") == ("tig:q", "tiger")

    only_tiger = EventSinks(snowflake=Store("snow", False), tiger=Store("tig", True))  # type: ignore[arg-type]
    assert only_tiger.recall("q") == ("tig:q", "tiger")

    none = EventSinks(snowflake=Store("snow", False), tiger=Store("tig", False))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="No event store"):
        none.recall("q")
