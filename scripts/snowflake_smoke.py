"""Check Snowflake, create SCENE_EVENTS, insert one row, and run recall.

Usage (from the repo root, with env vars or a .env file):

    python scripts/snowflake_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in __import__("os").environ:
            __import__("os").environ[key] = value


def main() -> int:
    _load_dotenv()
    try:
        from server.snowflake_store import SnowflakeStore
    except ImportError as exc:
        print(f"Snowflake smoke failed: cannot import the store ({exc}).", file=sys.stderr)
        print("Install dependencies with: pip install -r server/requirements.txt", file=sys.stderr)
        return 1

    store = SnowflakeStore()
    if not store.enabled:
        print(
            "Snowflake is disabled. Set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, and "
            "SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_PATH.",
            file=sys.stderr,
        )
        return 1
    try:
        conn = store.connect()
        try:
            store.ensure_schema(conn)
        finally:
            conn.close()
        store.log_event("smoke", "snowflake smoke event", session_id="smoke")
        store.start()
        store.stop(timeout=8)
        answer = store.recall("what was the smoke event")
    except Exception as exc:
        print(f"Snowflake smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(answer or "(recall returned empty text)")
    print("Snowflake smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
