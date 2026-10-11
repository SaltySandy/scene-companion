"""proximity message encoding and POST /debug/proximity reaching a phone."""

from __future__ import annotations

import asyncio
import json

import pytest

from server import protocol as proto


def test_encode_ok_and_unknown() -> None:
    ok = json.loads(proto.encode_proximity(1, "ok", "near", {"left": "clear", "center": "near", "right": "clear"}))
    assert (ok["type"], ok["state"], ok["level"]) == ("proximity", "ok", "near")
    unknown = json.loads(proto.encode_proximity(2, "unknown", "near"))
    assert unknown["level"] is None  # unknown never carries a level
    with pytest.raises(ValueError):
        proto.encode_proximity(3, "ok", "fine")


@pytest.mark.asyncio
async def test_debug_proximity_reaches_the_phone() -> None:
    import httpx
    import uvicorn
    import websockets

    from server.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=18769, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with websockets.connect("ws://127.0.0.1:18769/v1/stream") as ws:
            await ws.recv()  # hello_ack
            async with httpx.AsyncClient() as client:
                r = await client.post("http://127.0.0.1:18769/debug/proximity", json={"level": "danger"})
                bad = await client.post("http://127.0.0.1:18769/debug/proximity", json={"level": "fine"})
            msg = None
            while msg is None or msg["type"] != "proximity":
                msg = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
    finally:
        server.should_exit = True
        await task
    assert r.json()["ok"] and bad.status_code == 400
    assert msg["level"] == "danger" and msg["levels"]["left"] == "danger"
