"""Latest-frame slot: lineage (capture_ts, recv_ts) and drop accounting."""

from __future__ import annotations

import asyncio

import pytest

from server import protocol as proto
from server.hub import Hub


def test_first_frame_is_not_a_drop() -> None:
    hub = Hub()
    hub.submit_frame(b"a", capture_ts=1000, recv_ts=1040)
    assert hub.frames_dropped == 0
    assert hub.latest_frame == (b"a", 1000, 1040)


def test_overwriting_an_unseen_frame_counts_as_drop() -> None:
    hub = Hub()
    hub.submit_frame(b"a", 1000, 1040)
    hub.submit_frame(b"b", 1125, 1160)
    hub.submit_frame(b"c", 1250, 1290)
    assert hub.frames_dropped == 2
    frame = hub.take_latest()
    assert frame is not None
    assert (frame.jpeg, frame.capture_ts, frame.recv_ts) == (b"c", 1250, 1290)
    assert frame.dropped_before == 2


def test_take_empties_slot_and_resets_per_take_count() -> None:
    hub = Hub()
    hub.submit_frame(b"a", 1000, 1040)
    hub.submit_frame(b"b", 1125, 1160)
    assert hub.take_latest().dropped_before == 1
    assert hub.take_latest() is None
    hub.submit_frame(b"c", 1250, 1290)  # slot was empty: not a drop
    frame = hub.take_latest()
    assert frame.dropped_before == 0
    assert hub.frames_dropped == 1  # lifetime total is kept


def test_recv_ts_defaults_to_laptop_clock() -> None:
    hub = Hub()
    before = proto.now_ms()
    hub.submit_frame(b"a", 1000)
    after = proto.now_ms()
    assert before <= hub.latest_frame[2] <= after


@pytest.mark.asyncio
async def test_slow_model_drops_frames_end_to_end(monkeypatch) -> None:
    """A model slower than the camera must drop frames, never queue them."""
    import httpx
    import uvicorn
    import websockets

    import server.hub as hub_module
    from server.app import app
    from server.hub import hub

    calls: list[int] = []

    async def slow_on_frame(jpeg: bytes, ts: int):
        calls.append(ts)
        await asyncio.sleep(0.3)
        return None

    monkeypatch.setattr(hub_module, "on_frame", slow_on_frame)
    # The process-wide hub's Event binds to the first loop that waits on it;
    # each pytest-asyncio test gets a fresh loop, so give it a fresh Event.
    hub.frame_event = asyncio.Event()
    dropped_at_start = hub.frames_dropped

    config = uvicorn.Config(app, host="127.0.0.1", port=18767, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with websockets.connect("ws://127.0.0.1:18767/v1/stream") as ws:
            await ws.recv()  # hello_ack
            for i in range(15):  # ~50 fps against a ~3 fps model
                await ws.send(proto.encode_binary(proto.Kind.JPEG_FRAME, b"jpeg", timestamp_ms=1000 + i))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.4)
        async with httpx.AsyncClient() as client:
            health = (await client.get("http://127.0.0.1:18767/health")).json()
    finally:
        server.should_exit = True
        await task

    processed = len(calls)
    dropped = health["frames_dropped"] - dropped_at_start
    assert processed < 15
    assert dropped >= 10
    # Every frame is either processed, dropped, or the one left in the slot.
    assert processed + dropped in (15, 14)
    assert calls[-1] >= 1010  # the worker keeps up with the newest frame
