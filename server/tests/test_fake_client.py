"""In-process fake-client test: boots the server, sends JPEGs, expects say."""

from __future__ import annotations

import os

import pytest

# Force the demo stub to speak quickly so the test stays short.
os.environ["SAW_DEMO_ANNOUNCE_SEC"] = "0.3"

from server.ai_hook import reset_demo_state  # noqa: E402


@pytest.mark.asyncio
async def test_fake_phone_receives_demo_say() -> None:
    import asyncio

    import uvicorn

    from server.app import app
    from server.tests.fake_client import run_fake_client

    reset_demo_state()
    config = uvicorn.Config(app, host="127.0.0.1", port=18765, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"
        result = await run_fake_client(
            "ws://127.0.0.1:18765/v1/stream",
            frames=12,
            fps=10,
            wait_say_sec=4.0,
        )
    finally:
        server.should_exit = True
        await task

    assert result["hello_ack"] is True
    assert result["frames_sent"] == 12
    assert len(result["says"]) >= 1
    assert "frame received" in result["says"][0]["text"]


@pytest.mark.asyncio
async def test_http_say_reaches_connected_phone() -> None:
    import asyncio

    import httpx
    import uvicorn
    import websockets

    from server import protocol as proto
    from server.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=18766, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with websockets.connect("ws://127.0.0.1:18766/v1/stream") as ws:
            first = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert first["type"] == "hello_ack"
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "http://127.0.0.1:18766/say",
                    json={"text": "curb in two steps", "urgent": True},
                )
            assert r.status_code == 200
            incoming = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert incoming["type"] == "say"
            assert incoming["text"] == "curb in two steps"
            assert incoming["urgent"] is True
    finally:
        server.should_exit = True
        await task
