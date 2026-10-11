"""Viewer socket and /latest.jpg: a fake phone's JPEG must reach a late watcher."""

from __future__ import annotations

import asyncio

import pytest

from server import protocol as proto
from server.tests.fake_client import MINI_JPEG


async def _start(port: int):
    import uvicorn

    from server.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    return server, task


async def _stop(server, task) -> None:
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_viewer_socket_receives_frame_from_fake_phone() -> None:
    import httpx
    import websockets

    from server.hub import hub

    port = 18771
    server, task = await _start(port)
    try:
        hub.last_jpeg = None
        hub.viewers.clear()
        async with websockets.connect(f"ws://127.0.0.1:{port}/v1/stream") as phone:
            await phone.recv()  # hello_ack
            await phone.send(proto.encode_binary(proto.Kind.JPEG_FRAME, MINI_JPEG, timestamp_ms=1_700_000_000_001))
            await asyncio.sleep(0.05)

            async with httpx.AsyncClient() as client:
                latest = await client.get(f"http://127.0.0.1:{port}/latest.jpg")
                health = (await client.get(f"http://127.0.0.1:{port}/health")).json()
            assert latest.status_code == 200
            assert latest.content == MINI_JPEG
            assert health["latest_jpeg_bytes"] == len(MINI_JPEG)
            assert health["viewers"] == 0  # HTTP path works with no viewer socket

            async with websockets.connect(f"ws://127.0.0.1:{port}/v1/viewer", max_size=8_000_000) as viewer:
                hello = proto.decode_json(await asyncio.wait_for(viewer.recv(), timeout=2))
                while hello.get("type") != "hello_ack":
                    hello = proto.decode_json(await asyncio.wait_for(viewer.recv(), timeout=2))
                assert hello["role"] == "viewer"
                assert "fov" in hello and "roi_top" in hello["fov"]

                got = None
                for _ in range(12):
                    raw = await asyncio.wait_for(viewer.recv(), timeout=2)
                    if isinstance(raw, bytes):
                        got = proto.decode_binary(raw)
                        break
                assert got is not None
                assert got.kind is proto.Kind.JPEG_FRAME
                assert got.payload == MINI_JPEG

                health2 = (await httpx.AsyncClient().get(f"http://127.0.0.1:{port}/health")).json()
                assert health2["viewers"] >= 1
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_live_viewer_gets_the_next_phone_frame() -> None:
    import websockets

    port = 18772
    server, task = await _start(port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/v1/viewer", max_size=8_000_000) as viewer:
            await asyncio.wait_for(viewer.recv(), timeout=2)  # hello_ack
            async with websockets.connect(f"ws://127.0.0.1:{port}/v1/stream") as phone:
                await phone.recv()
                await phone.send(
                    proto.encode_binary(proto.Kind.JPEG_FRAME, MINI_JPEG, timestamp_ms=1_700_000_000_002)
                )
            got = None
            for _ in range(12):
                raw = await asyncio.wait_for(viewer.recv(), timeout=2)
                if isinstance(raw, bytes):
                    got = proto.decode_binary(raw)
                    break
            assert got is not None and got.payload == MINI_JPEG
    finally:
        await _stop(server, task)
