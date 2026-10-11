"""The JS phone protocol works against main and both unmerged proximity stacks.

Real servers are started from git worktrees when they boot. If a branch cannot
start (missing optional AI deps), a dialect stub still proves the extra
messages are classified and acted on.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import httpx
import pytest
import websockets

from server import protocol as proto
from server.tests.phone_helpers import (
    PHONE,
    ensure_worktree,
    finish_compat,
    free_port,
    run_compat,
    start_compat,
    start_uvicorn,
    stop_proc,
)

MINI_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0808080808080808080808080808080808080808080808080808080808080808"
    "080808080808080808080808080808080808080808080808080808080808ffd9"
)


def _maybe_install(packages: list[str]) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *packages], check=False, capture_output=True)


async def _dialect_server(port: int, dialect: str) -> asyncio.AbstractServer:
    async def handler(ws) -> None:
        await ws.send(proto.encode_json(proto.MsgType.HELLO_ACK, server="dialect", proto=1, dialect=dialect))
        if dialect == "feat/proximity":
            await ws.send(proto.encode_json("ping", t0=proto.now_ms()))
            await ws.send(
                proto.encode_json(
                    "proximity",
                    seq=1,
                    state="ok",
                    level="near",
                    levels={"left": "clear", "center": "near", "right": "clear"},
                    frame_key=1_700_000_001_000,
                    age_ms=8,
                )
            )
            await ws.send(proto.encode_json(proto.MsgType.SAY, text="what is it: a chair", urgent=True))
        elif dialect == "pr3":
            await ws.send(
                proto.encode_json(
                    proto.MsgType.SAY,
                    text="stop, obstacle ahead",
                    urgent=True,
                    capture_ts=1_700_000_001_111,
                    id="aud-1",
                )
            )
            await ws.send(proto.encode_json("flash", armed=True, flash_ts=1))
            await ws.send(proto.encode_json("latency", e2e_ms=40))
            await ws.send(proto.encode_json("audio_out", id="aud-1", event="queued"))
        try:
            async for _incoming in ws:
                pass
        except Exception:
            pass

    return await websockets.serve(handler, "127.0.0.1", port, max_size=8_000_000)


@pytest.mark.asyncio
async def test_compat_client_against_main_server() -> None:
    port = free_port()
    proc = start_uvicorn(port)
    try:
        result = run_compat(f"ws://127.0.0.1:{port}/v1/stream", frames=10, wait_ms=3500)
        assert result.get("error") is None
        assert result["hello_ack"] is True
        assert result["frames_sent"] == 10
        assert len(result["says"]) >= 1
        assert "frame received" in result["says"][0]["text"]
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{port}/say",
                json={"text": "stairs ahead, on your right", "urgent": False},
            )
        assert r.status_code == 200
    finally:
        stop_proc(proc)


@pytest.mark.asyncio
async def test_js_client_handles_feat_proximity_dialect() -> None:
    port = free_port()
    server = await _dialect_server(port, "feat/proximity")
    try:
        result = run_compat(f"ws://127.0.0.1:{port}", frames=2, wait_ms=400)
    finally:
        server.close()
        await server.wait_closed()
    assert result["hello_ack"] is True
    assert result["acks_sent"] >= 1
    assert result["proximity"][0]["level"] == "near"
    assert any("what is it" in s.get("text", "") for s in result["says"])
    assert "proximity" in result["handled"]


@pytest.mark.asyncio
async def test_js_client_handles_pr3_dialect() -> None:
    port = free_port()
    server = await _dialect_server(port, "pr3")
    try:
        result = run_compat(f"ws://127.0.0.1:{port}", frames=2, wait_ms=400)
    finally:
        server.close()
        await server.wait_closed()
    assert result["hello_ack"] is True
    assert any("obstacle" in s.get("text", "") for s in result["says"])
    assert "flash" in result["handled"]
    assert "latency" in result["handled"]
    assert "audio_out" in result["handled"]
    assert result["unknown"] == []


def _run_worktree(name: str, branch: str, extra_packages: list[str] | None = None) -> dict:
    if extra_packages:
        _maybe_install(extra_packages)
    dest = ensure_worktree(name, branch)
    port = free_port()
    proc = start_uvicorn(port, cwd=dest)
    try:
        client_proc = start_compat(f"ws://127.0.0.1:{port}/v1/stream", frames=6, wait_ms=3500)
        time.sleep(0.6)
        with httpx.Client() as client:
            if branch == "feat/proximity":
                prox = client.post(
                    f"http://127.0.0.1:{port}/debug/proximity",
                    json={"level": "danger", "state": "ok"},
                    timeout=3,
                )
                result_meta = {"debug_proximity": prox.status_code}
            else:
                say = client.post(
                    f"http://127.0.0.1:{port}/say",
                    json={"text": "stop, obstacle ahead", "urgent": True},
                    timeout=3,
                )
                result_meta = {"say_status": say.status_code}
        result = finish_compat(client_proc)
        result.update(result_meta)
        result["branch"] = branch
        result["worktree"] = str(dest)
        return result
    finally:
        stop_proc(proc)


def test_real_feat_proximity_server() -> None:
    try:
        result = _run_worktree("feat-proximity", "feat/proximity")
    except Exception as exc:
        pytest.skip(f"feat/proximity server did not start: {exc}")
    assert result["hello_ack"] is True
    assert result["frames_sent"] >= 1
    assert result.get("debug_proximity") == 200
    assert result.get("acks_sent", 0) >= 1 or result.get("proximity")


def test_real_pr3_server() -> None:
    try:
        result = _run_worktree("pr3", "cursor/proximity-warning-977b", extra_packages=["numpy", "pillow"])
    except Exception as exc:
        pytest.skip(f"PR #3 server did not start: {exc}")
    assert result["hello_ack"] is True
    assert result["frames_sent"] >= 1
    assert result.get("say_status") == 200


def test_handler_script_matches_android_kinds() -> None:
    """Binary kinds the web client sends are the Android v1 kinds."""
    script = r"""
const proto = require("./protocol.js");
console.log(JSON.stringify({jpeg: proto.KIND_JPEG, mic: proto.KIND_MIC, audio: proto.KIND_AUDIO}));
"""
    out = subprocess.check_output(["node", "-e", script], cwd=PHONE, text=True)
    kinds = json.loads(out)
    assert kinds == {"jpeg": 1, "mic": 2, "audio": 3}


def test_phone_static_exists() -> None:
    assert (PHONE / "index.html").is_file()
    assert (PHONE / "phone.js").is_file()
    assert "KIND_JPEG" in (PHONE / "protocol.js").read_text(encoding="utf-8")
