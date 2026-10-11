"""Check TigerData, create scene_events, insert one row, and run recall.

Usage (from the repo root, with TIGER_DATABASE_URL or a .env file):

    python scripts/tiger_smoke.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
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
        from server.tiger_store import TigerStore
    except ImportError as exc:
        print(f"TigerData smoke failed: cannot import the store ({exc}).", file=sys.stderr)
        print("Install dependencies with: pip install -r server/requirements.txt", file=sys.stderr)
        return 1

    store = TigerStore()
    if not store.enabled:
        print("TigerData is disabled. Set TIGER_DATABASE_URL.", file=sys.stderr)
        return 1
    try:
        conn = store.connect()
        try:
            store.ensure_schema(conn)
        finally:
            conn.close()
        store.log_event("smoke", "tiger smoke event", session_id="smoke")
        store.start()
        store.stop(timeout=8)
        answer = store.recall("smoke", window=timedelta(hours=1))
    except Exception as exc:
        print(f"TigerData smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(answer or "(recall returned empty text)")
    print("TigerData smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
