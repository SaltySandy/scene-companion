"""ClockSync math and the ping/pong round trip through the real server."""

from __future__ import annotations

import asyncio

import pytest

from server import protocol as proto
from server.clock import ClockSync


def test_offset_from_symmetric_round_trip() -> None:
    clock = ClockSync()
    # Phone is 5000 ms ahead; each leg takes 20 ms.
    sample = clock.add(t0=1000, tp=1000 + 20 + 5000, t1=1040)
    assert (sample.offset_ms, sample.rtt_ms) == (5000, 40)
    assert clock.to_laptop(10_000) == 5000


def test_lowest_rtt_sample_wins() -> None:
    clock = ClockSync()
    clock.add(t0=0, tp=5000 + 150, t1=200)  # slow, asymmetric: offset 5050
    clock.add(t0=1000, tp=6000 + 10, t1=1020)  # fast: offset 5000
    clock.add(t0=2000, tp=7000 + 300, t1=2400)
    assert (clock.offset_ms, clock.rtt_ms) == (5000, 20)


def test_unsynced_and_reset() -> None:
    clock = ClockSync()
    assert clock.offset_ms is None and clock.to_laptop(1) is None
    assert clock.add(t0=100, tp=0, t1=50) is None  # negative rtt is rejected
    clock.add(t0=0, tp=10, t1=20)
    clock.reset()
    assert clock.offset_ms is None


@pytest.mark.asyncio
async def test_phone_answering_pings_gets_synced() -> None:
    import httpx
    import uvicorn
    import websockets

    from server.app import app
    from server.hub import hub

    config = uvicorn.Config(app, host="127.0.0.1", port=18768, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    skew = 7_000  # this fake phone's clock runs 7 s ahead
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with websockets.connect("ws://127.0.0.1:18768/v1/stream") as ws:
            answered = 0
            while answered < 3:
                msg = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
                if msg["type"] == "ping":
                    await ws.send(proto.encode_json("pong", t0=msg["t0"], tp=proto.now_ms() + skew))
                    answered += 1
            await asyncio.sleep(0.1)
            async with httpx.AsyncClient() as client:
                health = (await client.get("http://127.0.0.1:18768/health")).json()
    finally:
        server.should_exit = True
        await task
    assert abs(health["clock_offset_ms"] - skew) <= health["clock_rtt_ms"] / 2 + 1
