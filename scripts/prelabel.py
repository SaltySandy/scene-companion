"""Pre-label recorded frames with Claude: nearest obstacle per sector, in metres.

    python scripts/prelabel.py                       # newest session, every 5th frame
    python scripts/prelabel.py sessions/<id> --every 1 --workers 4
    python scripts/prelabel.py --dry-run             # print one request, call nothing

Writes sessions/<id>/labels.jsonl, one line per labeled frame:

    {"frame_key": ..., "labeler": "claude-opus-5-5", "reviewed": false,
     "left": {"nearest_m": 3.2, "obstacle": "wall"}, "center": {...}, "right": {...}}

Labels hold distances, not levels, so scripts/eval_proximity.py can sweep the
near/danger thresholds without re-labeling. Frames already in labels.jsonl are
skipped, so the script can be stopped and resumed. These are pre-labels: spot
check them (set "reviewed": true after fixing) before trusting a threshold.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.session_report import find_jsonl, load  # noqa: E402
from server.ai import vlm  # noqa: E402
from server.ai.proximity import SECTORS  # noqa: E402

MAX_M = 5.0

PROMPT = f"""This photo comes from a phone strapped to the chest of a person who is walking forward. \
It is used to tune an obstacle warning for a visually impaired walker, so err toward the nearer \
estimate when unsure.

Split the image into three equal vertical strips: left, center, and right. For each strip, estimate \
the distance in metres from the camera to the nearest thing the person could walk into or trip over: \
walls, doors, people, chairs, tables and other furniture, poles, open cabinet doors, railings, and \
anything standing or lying on the floor such as bags and boxes. The bare floor itself is not an \
obstacle. Steps and curbs do count.

If nothing is within {MAX_M:g} metres in a strip, give {MAX_M:g} and obstacle "none". Otherwise give \
the obstacle as one or two plain words."""

SECTOR_SCHEMA = {
    "type": "object",
    "properties": {"nearest_m": {"type": "number"}, "obstacle": {"type": "string"}},
    "required": ["nearest_m", "obstacle"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {name: SECTOR_SCHEMA for name in SECTORS},
    "required": list(SECTORS),
    "additionalProperties": False,
}


def label_path(session_dir: Path) -> Path:
    return session_dir / "labels.jsonl"


def already_labeled(path: Path) -> set[int]:
    if not path.exists():
        return set()
    return {json.loads(line)["frame_key"] for line in path.read_text().splitlines() if line.strip()}


def pick_frames(session_dir: Path, every: int, limit: int | None) -> list[int]:
    keys = [r["frame_key"] for r in load(session_dir / "frames.jsonl")]
    keys = [k for k in keys[:: max(1, every)] if (session_dir / "frames" / f"{k}.jpg").exists()]
    done = already_labeled(label_path(session_dir))
    todo = [k for k in keys if k not in done]
    return todo[:limit] if limit else todo


def clamp(answer: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name in SECTORS:
        sector = answer[name]
        out[name] = {
            "nearest_m": round(min(max(float(sector["nearest_m"]), 0.0), MAX_M), 2),
            "obstacle": str(sector["obstacle"]).strip() or "none",
        }
    return out


def label_session(session_dir: Path, client: Any, *, every: int = 5, limit: int | None = None, workers: int = 4) -> int:
    """Label pending frames. Returns how many labels were written."""
    import anthropic

    todo = pick_frames(session_dir, every, limit)
    out_path = label_path(session_dir)
    lock = threading.Lock()
    written = 0

    def one(key: int) -> None:
        nonlocal written
        jpeg = (session_dir / "frames" / f"{key}.jpg").read_bytes()
        try:
            answer = vlm.ask_json(client, jpeg, PROMPT, SCHEMA, effort="high")
        except anthropic.APIStatusError as exc:
            print(f"frame {key}: API error {exc.status_code}: {exc.message}", file=sys.stderr)
            return
        except anthropic.APIConnectionError as exc:
            print(f"frame {key}: connection error: {exc}", file=sys.stderr)
            return
        if answer is None:
            print(f"frame {key}: no answer (declined or truncated)", file=sys.stderr)
            return
        line = {"frame_key": key, "labeler": vlm.model_name(), "reviewed": False, **clamp(answer)}
        with lock:
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
            written += 1
            print(f"frame {key}: " + ", ".join(f"{s}={line[s]['nearest_m']}m {line[s]['obstacle']}" for s in SECTORS))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, todo))
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", nargs="?", type=Path, help="session dir (default: newest)")
    p.add_argument("--every", type=int, default=5, help="label every Nth frame (default 5)")
    p.add_argument("--limit", type=int, help="stop after N frames")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="print one request and exit")
    args = p.parse_args(argv)
    session_dir = find_jsonl(args.target).parent
    todo = pick_frames(session_dir, args.every, args.limit)
    print(f"{session_dir}: {len(todo)} frames to label with {vlm.model_name()}")
    if args.dry_run:
        if todo:
            req = vlm.build_request((session_dir / "frames" / f"{todo[0]}.jpg").read_bytes(), PROMPT, SCHEMA, effort="high")
            req["messages"][0]["content"][0]["source"]["data"] = "<base64 jpeg elided>"
            print(json.dumps(req, indent=2))
        return 0
    import anthropic

    written = label_session(session_dir, anthropic.Anthropic(), every=args.every, limit=args.limit, workers=args.workers)
    print(f"wrote {written} labels to {label_path(session_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
