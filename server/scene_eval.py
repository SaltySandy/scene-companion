"""Replay fixture scenes through the live pipeline and score the output.

In-process (default): calls ``nav.analyze`` and ``ai_hook.on_frame`` the same
way the worker would. WebSocket mode talks SAW v1 as a fake phone, either by
booting uvicorn or by pointing ``--url`` at a running laptop (8765–8767 or
the /phone HTTPS frontend).

    python scripts/replay_scenes.py
    python scripts/replay_scenes.py --url http://127.0.0.1:8765
    python scripts/replay_scenes.py --sequence 01,02,04
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from server import nav as nav_mod
from server import protocol as proto
from server.nav import NavResult, reset_navigator

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "server" / "tests" / "fixtures" / "scenes"
MANIFEST_PATH = FIXTURES / "manifest.json"
STREAM_DIR = FIXTURES / "640x480"

NAV_FIELDS = (
    "exit_visible",
    "exit_bearing",
    "exit_distance",
    "obstacle_ahead",
    "nearest_obstacle_distance",
    "should_stop",
    "arrived",
    "utterance",
)


@dataclass
class SceneGT:
    id: str
    file: str
    exit_visible: str
    exit_bearing: str
    exit_distance: str
    exit_distance_m: float | None
    obstacle_ahead: bool
    nearest_obstacle_distance: str
    nearest_obstacle_m: float | None
    nearest_obstacle_label: str | None
    should_stop: bool
    arrived: bool
    utterance: str
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def jpeg_path(self) -> Path:
        return STREAM_DIR / self.file

    def jpeg_bytes(self) -> bytes:
        return self.jpeg_path.read_bytes()


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))


def load_scenes(path: Path | None = None) -> list[SceneGT]:
    data = load_manifest(path)
    scenes = []
    for raw in data["scenes"]:
        scenes.append(
            SceneGT(
                id=raw["id"],
                file=raw["file"],
                exit_visible=raw["exit_visible"],
                exit_bearing=raw["exit_bearing"],
                exit_distance=raw["exit_distance"],
                exit_distance_m=raw.get("exit_distance_m"),
                obstacle_ahead=bool(raw["obstacle_ahead"]),
                nearest_obstacle_distance=raw["nearest_obstacle_distance"],
                nearest_obstacle_m=raw.get("nearest_obstacle_m"),
                nearest_obstacle_label=raw.get("nearest_obstacle_label"),
                should_stop=bool(raw["should_stop"]),
                arrived=bool(raw["arrived"]),
                utterance=raw["utterance"],
                source=raw.get("source") or {},
            )
        )
    return scenes


def scene_by_id(scenes: list[SceneGT], token: str) -> SceneGT:
    token = token.strip()
    for scene in scenes:
        if scene.id == token or scene.id.startswith(token) or scene.file.startswith(token):
            return scene
    raise KeyError(f"no scene matching {token!r}")


def parse_sequence(spec: str | None, scenes: list[SceneGT], manifest: dict[str, Any] | None = None) -> list[SceneGT]:
    if spec in (None, "", "default"):
        ids = (manifest or load_manifest()).get("sequence_default") or [
            "01_entry_overview",
            "02_chair_blocking",
            "04_arrived_at_exit",
        ]
    else:
        ids = [part.strip() for part in spec.replace("→", ",").replace("->", ",").split(",") if part.strip()]
    return [scene_by_id(scenes, token) for token in ids]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _norm_words(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (text or "")).split())


def utterance_checks(gt: SceneGT) -> list[tuple[str, list[str], bool]]:
    """Named keyword groups. Each group passes if any synonym is present.

    ``required`` is True for groups that must hit. 08 also has a negative
    check (open doorway must not be called the exit).
    """
    checks: list[tuple[str, list[str], bool]] = []
    if gt.should_stop and gt.arrived:
        checks.append(("stop_or_arrive", ["stop", "reached", "arrived", "here"], True))
    elif gt.should_stop:
        checks.append(("stop", ["stop"], True))
    if gt.arrived:
        checks.append(("exit", ["exit", "door"], True))
    if gt.exit_bearing in {"left", "right"} and not gt.arrived:
        checks.append(("direction", [gt.exit_bearing], True))
    if gt.exit_bearing == "center" and gt.exit_visible != "no" and not gt.arrived:
        checks.append(("direction", ["ahead", "forward", "straight"], True))
    if gt.exit_visible == "no" and not gt.should_stop:
        checks.append(("scan", ["turn", "scan", "behind", "not"], True))
    if gt.obstacle_ahead and not gt.arrived:
        label = (gt.nearest_obstacle_label or "obstacle").lower()
        checks.append(("obstacle", [label, "chair", "wall", "obstacle"], True))
    return checks


def score_utterance(pred: str, gt: SceneGT) -> dict[str, Any]:
    text = _norm_words(pred)
    hits = []
    misses = []
    for name, words, required in utterance_checks(gt):
        ok = any(word in text for word in words)
        (hits if ok else misses).append({"name": name, "words": words, "required": required})
    # 08: do not call the open doorway the exit.
    doorway_trap = False
    if gt.id.startswith("08") and text:
        negated = any(tok in text for tok in ("not", "no exit", "isnt", "isn't", "do not"))
        claims_exit = "exit" in text and any(w in text for w in ("ahead", "forward", "doorway", "open door"))
        doorway_trap = claims_exit and not negated
        if doorway_trap:
            misses.append({"name": "not_doorway", "words": ["not"], "required": True})
    required_total = sum(1 for _, _, req in utterance_checks(gt) if req) + (1 if gt.id.startswith("08") else 0)
    required_hits = required_total - sum(1 for m in misses if m["required"])
    score = 1.0 if not misses else (required_hits / required_total if required_total else 0.0)
    if misses and all(not m["required"] for m in misses):
        score = 1.0
    return {
        "score": 1.0 if score >= 1.0 else (0.0 if score <= 0 else round(score, 3)),
        "hits": hits,
        "misses": misses,
        "doorway_trap": doorway_trap,
        "pred": pred,
        "ref": gt.utterance,
    }


def llm_judge_utterance(pred: str, gt: SceneGT) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"skipped": True, "reason": "no OPENAI_API_KEY or GEMINI_API_KEY"}
    prompt = (
        "You score spoken guidance for a blind pedestrian. Reply with JSON "
        '{"score": 0 or 1, "reason": "..."}. score=1 only if the utterance is '
        "safe and matches the scene (direction, stop, arrival). "
        f"Scene: exit_visible={gt.exit_visible}, bearing={gt.exit_bearing}, "
        f"distance={gt.exit_distance}, obstacle_ahead={gt.obstacle_ahead}, "
        f"nearest={gt.nearest_obstacle_label} {gt.nearest_obstacle_distance}, "
        f"should_stop={gt.should_stop}, arrived={gt.arrived}. "
        f"Reference: {gt.utterance!r}. Spoken: {pred!r}."
    )
    try:
        import httpx
    except ImportError:
        return {"skipped": True, "reason": "httpx missing"}
    try:
        if os.environ.get("OPENAI_API_KEY"):
            url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            model = os.environ.get("SAW_JUDGE_MODEL", "gpt-4o-mini")
            r = httpx.post(
                f"{url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 80,
                    "temperature": 0,
                },
                timeout=20,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        else:
            model = os.environ.get("SAW_JUDGE_MODEL", "gemini-2.0-flash")
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=20,
            )
            r.raise_for_status()
            content = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}
    try:
        start, end = content.find("{"), content.rfind("}")
        body = json.loads(content[start : end + 1])
        return {"skipped": False, "score": float(body.get("score", 0)), "reason": body.get("reason", ""), "raw": content}
    except Exception:
        return {"skipped": True, "reason": f"unparseable judge output: {content[:200]}"}


def expected_proximity_level(gt: SceneGT) -> str:
    """Brian's defaults: danger < 0.8 m, near < 1.6 m, else clear."""
    meters = gt.nearest_obstacle_m
    if meters is None or not gt.obstacle_ahead:
        return "clear"
    if meters < 0.8 or (gt.should_stop and gt.nearest_obstacle_distance == "close"):
        return "danger"
    if meters < 1.6:
        return "near"
    return "clear"


def score_proximity(gt: SceneGT, prox: dict[str, Any] | None) -> dict[str, Any] | None:
    if prox is None:
        return None
    state = prox.get("state")
    level = prox.get("level")
    expected = expected_proximity_level(gt)
    if state != "ok" or not level:
        return {
            "available": True,
            "state": state,
            "level": level,
            "expected": expected,
            "ok": False,
            "score": 0.0,
            "reason": "proximity unknown or missing level (depth model not loaded?)",
        }
    # False-clear is the dangerous miss. Accept a more-severe warning.
    order = {"clear": 0, "near": 1, "danger": 2}
    ok = order.get(level, 0) >= order[expected] if expected != "clear" else level == "clear"
    if expected == "clear" and level != "clear":
        ok = False
    return {
        "available": True,
        "state": state,
        "level": level,
        "levels": prox.get("levels"),
        "sectors_m": prox.get("sectors_m"),
        "expected": expected,
        "ok": ok,
        "score": 1.0 if ok else 0.0,
    }


def score_nav(pred: NavResult, gt: SceneGT, spoken: str | None = None, llm: bool = False) -> dict[str, Any]:
    payload = pred.payload()
    utt = spoken if spoken is not None else pred.utterance
    utt_score = score_utterance(utt, gt)
    if llm:
        utt_score["llm"] = llm_judge_utterance(utt, gt)
    fields = {
        "exit_visible": 1.0 if pred.exit.visible == gt.exit_visible else 0.0,
        "exit_bearing": 1.0 if pred.exit.bearing == gt.exit_bearing else 0.0,
        "exit_distance": 1.0 if pred.exit.distance == gt.exit_distance else 0.0,
        "obstacle_ahead": 1.0 if bool(pred.obstacle_ahead) == gt.obstacle_ahead else 0.0,
        "nearest_obstacle_distance": 1.0 if pred.nearest_obstacle_distance == gt.nearest_obstacle_distance else 0.0,
        "should_stop": 1.0 if bool(pred.should_stop) == gt.should_stop else 0.0,
        "arrived": 1.0 if bool(pred.arrived) == gt.arrived else 0.0,
        "utterance": float(utt_score["score"]),
    }
    return {
        "id": gt.id,
        "pred": payload,
        "truth": {
            "exit_visible": gt.exit_visible,
            "exit_bearing": gt.exit_bearing,
            "exit_distance": gt.exit_distance,
            "obstacle_ahead": gt.obstacle_ahead,
            "nearest_obstacle_distance": gt.nearest_obstacle_distance,
            "should_stop": gt.should_stop,
            "arrived": gt.arrived,
            "utterance": gt.utterance,
        },
        "fields": fields,
        "score": round(sum(fields.values()) / len(fields), 4),
        "utterance": utt_score,
        "spoken": utt,
    }


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------


def _as_items(response: Any) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, (list, tuple)):
        return list(response)
    return [response]


def extract_hook_outputs(response: Any) -> tuple[list[str], dict[str, Any] | None]:
    says: list[str] = []
    proximity: dict[str, Any] | None = None
    for item in _as_items(response):
        if getattr(item, "text", None) is not None and type(item).__name__ == "Say":
            says.append(str(item.text))
        state = getattr(item, "state", None)
        if state is not None and hasattr(item, "level"):
            proximity = {
                "state": state,
                "level": getattr(item, "level", None),
                "levels": getattr(item, "levels", None),
                "sectors_m": getattr(item, "sectors_m", None),
                "model": getattr(item, "model", None),
            }
    return says, proximity


async def run_inprocess_one(jpeg: bytes, ts: int) -> dict[str, Any]:
    result = nav_mod.analyze(jpeg)
    says: list[str] = []
    proximity = None
    try:
        from server.ai_hook import on_frame

        hook = await on_frame(jpeg, ts)
        says, proximity = extract_hook_outputs(hook)
    except Exception as exc:
        says = []
        proximity = {"error": str(exc)} if "proximity" in str(exc).lower() else None
    return {"nav": result, "says": says, "proximity": proximity}


def stream_url(url: str) -> str:
    raw = url.strip()
    if raw.startswith("ws://") or raw.startswith("wss://"):
        if raw.rstrip("/").endswith("/v1/stream"):
            return raw
        return raw.rstrip("/") + "/v1/stream"
    parsed = urlparse(raw)
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    netloc = parsed.netloc or parsed.path
    return urlunparse((scheme, netloc, "/v1/stream", "", "", ""))


async def run_websocket_scenes(
    scenes: list[SceneGT],
    url: str,
    wait_sec: float = 0.6,
) -> list[dict[str, Any]]:
    import websockets

    target = stream_url(url)
    out: list[dict[str, Any]] = []
    async with websockets.connect(target, max_size=8_000_000) as ws:
        await ws.send(proto.encode_json(proto.MsgType.HELLO, role="phone", app="scene-replay", app_ver="0.1.0"))
        await ws.send(
            proto.encode_json(
                proto.MsgType.CONFIG,
                width=640,
                height=480,
                fps=8,
                quality=70,
                mic=False,
            )
        )
        # Drain hello_ack so it is not credited to the first scene.
        try:
            await asyncio.wait_for(ws.recv(), timeout=2)
        except TimeoutError:
            pass

        async def drain(deadline: float) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
            says: list[str] = []
            proximity: list[dict[str, Any]] = []
            other: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, deadline - time.monotonic()))
                except TimeoutError:
                    break
                if isinstance(raw, bytes):
                    continue
                try:
                    obj = proto.decode_json(raw)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == proto.MsgType.SAY or kind == "say":
                    says.append(str(obj.get("text") or ""))
                elif kind == "proximity":
                    proximity.append(obj)
                else:
                    other.append(obj)
            return says, proximity, other

        for scene in scenes:
            jpeg = scene.jpeg_bytes()
            nav = nav_mod.analyze(jpeg)
            await ws.send(proto.encode_binary(proto.Kind.JPEG_FRAME, jpeg, timestamp_ms=proto.now_ms()))
            says, prox_msgs, other = await drain(time.monotonic() + wait_sec)
            prox = prox_msgs[-1] if prox_msgs else None
            out.append(
                {
                    "nav": nav,
                    "says": says,
                    "proximity": prox,
                    "proximity_msgs": prox_msgs,
                    "other": other,
                    "scene": scene,
                }
            )
    return out


async def _serve_and_replay(scenes: list[SceneGT], wait_sec: float) -> list[dict[str, Any]]:
    import uvicorn

    from server.app import app
    from server.tests.phone_helpers import free_port

    port = free_port()
    os.environ.setdefault("SENTRY_DSN", "")
    os.environ.setdefault("SAW_DEMO_ANNOUNCE_SEC", "0")
    os.environ.setdefault("SAW_NAV", "1")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(150):
            if server.started:
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        return await run_websocket_scenes(scenes, f"http://127.0.0.1:{port}", wait_sec=wait_sec)
    finally:
        server.should_exit = True
        await task


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def build_scorecard(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    navigator: str,
    sequence: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    field_avgs = {name: _mean([r["fields"][name] for r in rows]) for name in NAV_FIELDS}
    prox_scores = [r["proximity"]["score"] for r in rows if r.get("proximity")]
    prox_available = any(r.get("proximity") for r in rows)
    card = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "navigator": navigator,
        "note": load_manifest()["note"],
        "scenes": rows,
        "field_averages": field_avgs,
        "overall": _mean([r["score"] for r in rows]),
        "proximity_overall": _mean(prox_scores) if prox_available else None,
        "proximity_available": prox_available,
        "n_scenes": len(rows),
    }
    if sequence is not None:
        card["sequence"] = {
            "ids": [r["id"] for r in sequence],
            "scenes": sequence,
            "overall": _mean([r["score"] for r in sequence]),
            "continuity": _sequence_continuity(sequence),
        }
    if extra:
        card.update(extra)
    return card


def _sequence_continuity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """01→02→04 should stay on the exit and only arrive at the end."""
    checks = {}
    if not rows:
        return checks
    bearings = [r["pred"]["exit"]["bearing"] for r in rows]
    arrived = [r["pred"]["arrived"] for r in rows]
    checks["no_left_flip"] = 1.0 if "left" not in bearings else 0.0
    checks["arrive_only_last"] = 1.0 if (not any(arrived[:-1]) and arrived[-1]) or not any(arrived) else 0.0
    if len(rows) >= 2:
        checks["middle_stop"] = 1.0 if rows[1]["pred"]["should_stop"] else 0.0
    checks["score"] = _mean(list(checks.values())) if checks else 0.0
    return checks


def render_table(card: dict[str, Any]) -> str:
    headers = ["scene", "vis", "bear", "dist", "obst", "near", "stop", "arr", "utt", "nav", "prox"]
    lines = []
    lines.append(
        f"{headers[0]:<28} "
        + " ".join(f"{h:>4}" for h in headers[1:])
    )
    lines.append("-" * 78)

    def mark(value: float) -> str:
        if value >= 1.0:
            return "Y"
        if value <= 0.0:
            return "."
        return f"{value:.1f}"

    for row in card["scenes"]:
        f = row["fields"]
        prox = row.get("proximity")
        prox_s = "n/a" if not prox else mark(prox["score"])
        lines.append(
            f"{row['id']:<28} "
            f"{mark(f['exit_visible']):>4} "
            f"{mark(f['exit_bearing']):>4} "
            f"{mark(f['exit_distance']):>4} "
            f"{mark(f['obstacle_ahead']):>4} "
            f"{mark(f['nearest_obstacle_distance']):>4} "
            f"{mark(f['should_stop']):>4} "
            f"{mark(f['arrived']):>4} "
            f"{mark(f['utterance']):>4} "
            f"{row['score'] * 100:3.0f}% "
            f"{prox_s:>4}"
        )
    lines.append("-" * 78)
    fa = card["field_averages"]
    prox_overall = "n/a" if card.get("proximity_overall") is None else f"{card['proximity_overall'] * 100:.0f}%"
    lines.append(
        f"{'field avg / overall':<28} "
        f"{mark(fa['exit_visible']):>4} "
        f"{mark(fa['exit_bearing']):>4} "
        f"{mark(fa['exit_distance']):>4} "
        f"{mark(fa['obstacle_ahead']):>4} "
        f"{mark(fa['nearest_obstacle_distance']):>4} "
        f"{mark(fa['should_stop']):>4} "
        f"{mark(fa['arrived']):>4} "
        f"{mark(fa['utterance']):>4} "
        f"{card['overall'] * 100:3.0f}% "
        f"{prox_overall:>4}"
    )
    lines.append("")
    lines.append(
        f"Overall nav {card['overall'] * 100:.1f}%   "
        f"proximity {prox_overall}   "
        f"navigator={card['navigator']} mode={card['mode']}"
    )
    if card.get("sequence"):
        seq = card["sequence"]
        cont = seq.get("continuity") or {}
        lines.append(
            f"Sequence {' → '.join(seq['ids'])}: nav {seq['overall'] * 100:.1f}%  "
            f"continuity {cont.get('score', 0) * 100:.0f}%"
        )
        for srow in seq["scenes"]:
            lines.append(f"  {srow['id']}: {srow['score'] * 100:.0f}%  say={srow.get('spoken')!r}")
    return "\n".join(lines)


def json_ready(card: dict[str, Any]) -> dict[str, Any]:
    """Drop non-serializable objects (NavResult already converted)."""
    return json.loads(json.dumps(card))


async def evaluate(
    *,
    mode: str = "inprocess",
    url: str | None = None,
    navigator: str = "baseline",
    sequence_spec: str | None = "default",
    llm_judge: bool = False,
    wait_sec: float = 0.6,
) -> dict[str, Any]:
    os.environ.setdefault("SENTRY_DSN", "")
    scenes = load_scenes()
    reset_navigator()
    nav_mod.default_navigator(navigator)

    if url:
        mode = "websocket-url"
        raw_rows = await run_websocket_scenes(scenes, url, wait_sec=wait_sec)
    elif mode in {"websocket", "ws"}:
        mode = "websocket"
        raw_rows = await _serve_and_replay(scenes, wait_sec=wait_sec)
    else:
        mode = "inprocess"
        raw_rows = []
        for scene in scenes:
            raw_rows.append({**(await run_inprocess_one(scene.jpeg_bytes(), proto.now_ms())), "scene": scene})

    rows = []
    for raw in raw_rows:
        scene: SceneGT = raw["scene"]
        pred: NavResult = raw["nav"]
        says: list[str] = raw.get("says") or []
        spoken = pred.utterance
        # Prefer what the phone actually heard when the live hook spoke nav text.
        nav_says = [s for s in says if s and "frame received" not in s.lower() and not s.lower().startswith("you said")]
        if mode.startswith("websocket") and nav_says:
            spoken = nav_says[-1]
        graded = score_nav(pred, scene, spoken=spoken, llm=llm_judge)
        graded["hook_says"] = says
        graded["proximity"] = score_proximity(scene, raw.get("proximity"))
        rows.append(graded)

    reset_navigator()
    nav_mod.default_navigator(navigator)
    seq_scenes = parse_sequence(sequence_spec, scenes)
    seq_rows = []
    if url:
        seq_raw = await run_websocket_scenes(seq_scenes, url, wait_sec=wait_sec)
    elif mode == "websocket":
        seq_raw = await _serve_and_replay(seq_scenes, wait_sec=wait_sec)
    else:
        seq_raw = []
        for scene in seq_scenes:
            seq_raw.append({**(await run_inprocess_one(scene.jpeg_bytes(), proto.now_ms())), "scene": scene})
    for raw in seq_raw:
        scene = raw["scene"]
        pred = raw["nav"]
        says = raw.get("says") or []
        spoken = pred.utterance
        nav_says = [s for s in says if s and "frame received" not in s.lower()]
        if mode.startswith("websocket") and nav_says:
            spoken = nav_says[-1]
        graded = score_nav(pred, scene, spoken=spoken, llm=llm_judge)
        graded["hook_says"] = says
        graded["proximity"] = score_proximity(scene, raw.get("proximity"))
        seq_rows.append(graded)

    return build_scorecard(rows, mode=mode, navigator=navigator, sequence=seq_rows, extra={"url": url})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("inprocess", "websocket", "ws"), default="inprocess")
    p.add_argument("--url", help="Running server: http://127.0.0.1:8765 or ws://host:8767/v1/stream")
    p.add_argument("--navigator", default=os.environ.get("SAW_NAVIGATOR") or "exit")
    p.add_argument("--sequence", default="default", help="Comma ids, e.g. 01,02,04")
    p.add_argument("--llm-judge", action="store_true", help="Optional LLM utterance judge (needs an API key)")
    p.add_argument("--json", dest="json_path", default="", help="Write scorecard JSON (also printed)")
    p.add_argument("--wait", type=float, default=0.6, help="Seconds to wait per frame in WebSocket mode")
    args = p.parse_args(argv)

    card = asyncio.run(
        evaluate(
            mode=args.mode,
            url=args.url,
            navigator=args.navigator,
            sequence_spec=args.sequence,
            llm_judge=args.llm_judge,
            wait_sec=args.wait,
        )
    )
    table = render_table(card)
    print(table)
    payload = json_ready(card)
    payload["table"] = table
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {path}")
    else:
        default = FIXTURES / "scorecard.json"
        default.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {default}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
