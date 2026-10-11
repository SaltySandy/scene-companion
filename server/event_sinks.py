"""Fan-out for optional event stores. The server calls log_event once."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from server.snowflake_store import SnowflakeStore
from server.tiger_store import TigerStore

log = logging.getLogger("saw.events")


class EventSinks:
    def __init__(
        self,
        snowflake: SnowflakeStore | None = None,
        tiger: TigerStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.snowflake = snowflake if snowflake is not None else SnowflakeStore()
        self.tiger = tiger if tiger is not None else TigerStore()
        self.session_id = session_id or uuid.uuid4().hex

    def start(self) -> None:
        self.snowflake.start()
        self.tiger.start()

    def stop(self) -> None:
        self.snowflake.stop()
        self.tiger.stop()

    def log_event(
        self,
        kind: str,
        text: str,
        *,
        hazard: bool | None = None,
        meta: Any = None,
    ) -> None:
        """Enqueue on every configured sink. Never raises."""
        for name, sink in (("snowflake", self.snowflake), ("tiger", self.tiger)):
            try:
                sink.log_event(
                    kind,
                    text,
                    hazard=hazard,
                    meta=meta,
                    session_id=self.session_id,
                )
            except Exception:
                log.debug("%s log_event failed", name, exc_info=True)

    def recall(self, question: str, backend: str | None = None) -> tuple[str, str]:
        """Return (text, backend_name). Raises if the chosen store is off."""
        chosen = (backend or "").strip().lower() or None
        if chosen == "snowflake":
            return self.snowflake.recall(question), "snowflake"
        if chosen == "tiger":
            return self.tiger.recall(question), "tiger"
        if chosen not in (None, ""):
            raise ValueError("backend must be snowflake or tiger")
        if self.snowflake.enabled:
            return self.snowflake.recall(question), "snowflake"
        if self.tiger.enabled:
            return self.tiger.recall(question), "tiger"
        raise RuntimeError(
            "No event store is configured. Set Snowflake env vars or TIGER_DATABASE_URL."
        )


sinks = EventSinks()
