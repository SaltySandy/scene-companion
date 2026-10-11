"""Integration: injected latency, jitter, skew, reconnects, flash, TTS interrupt."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
import uvicorn
import websockets

from server import protocol as proto
from server.ai_hook import reset_demo_state
from server.app import app
from server.hub import hub
from server.latency import book
from server.proximity import FixedEstimator, engine
from server.proximity_frames import (
    approach_sequence,
    focus_meta_for_distance,
    render_obstacle,
    render_white_flash,
)
from server.trace import tracer
from server.tests.fake_client import CELL, USB, WIFI, NetProfile, SimulatedTts, OutgoingSay, run_simulation


async def _serve(port: int):
    reset_demo_state()
    hub.reset_for_tests()
    book.reset()
    engine.reset()
    os.environ["SAW_DEMO_ANNOUNCE_SEC"] = "0"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, f"uvicorn did not start on {port}"
    return server, task


async def _stop(server, task) -> None:
    server.should_exit = True
    await task
    for worker in (hub.worker_task, hub.prox_task):
        if worker is not None and not worker.done():
            worker.cancel()
    hub.worker_task = None
    hub.prox_task = None
    hub.reset_for_tests()
    book.reset()
    engine.reset()
    reset_demo_state()


@pytest.mark.asyncio
async def test_http_latency_and_flash_endpoints() -> None:
    server, task = await _serve(18801)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:18801/latency")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert "e2e_ms" in body and "clock" in body and "proximity" in body
            flash = await client.post("http://127.0.0.1:18801/flash", json={"t": 12345})
            assert flash.status_code == 200
            assert flash.json()["armed"] is True
            assert book.flash_armed is True
            health = await client.get("http://127.0.0.1:18801/health")
            assert health.json()["ok"] is True
            trace = await client.get("http://127.0.0.1:18801/proximity_trace")
            assert trace.status_code == 200
            assert trace.json()["ok"] is True
            assert "rows" in trace.json()
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_flash_detects_white_frame_on_laptop_clock() -> None:
    server, task = await _serve(18802)
    try:
        async with httpx.AsyncClient() as client:
            armed = await client.post("http://127.0.0.1:18802/flash", json={"t": proto.now_ms()})
            assert armed.json()["armed"] is True
        async with websockets.connect("ws://127.0.0.1:18802/v1/stream") as ws:
            await ws.recv()  # hello_ack
            await ws.send(proto.encode_binary(proto.Kind.JPEG_FRAME, render_white_flash(), timestamp_ms=proto.now_ms()))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and book.last_flash is None:
                await asyncio.sleep(0.05)
        assert book.last_flash is not None
        assert book.last_flash.scene_to_processed_ms >= 0
        async with httpx.AsyncClient() as client:
            snap = (await client.get("http://127.0.0.1:18802/latency")).json()
            assert snap["flash"]["last"]["scene_to_processed_ms"] >= 0
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_clock_sync_recovers_injected_skew() -> None:
    server, task = await _serve(18803)
    try:
        profile = NetProfile("skew", uplink_ms=8, downlink_ms=8, jitter_ms=0, skew_ms=180, tts_ms=5)
        frames = [(render_obstacle(2.0), 2.0, i * 0.05) for i in range(8)]
        result = await run_simulation(
            "ws://127.0.0.1:18803/v1/stream",
            frames,
            profile=profile,
            wait_after_s=0.8,
        )
        assert result.hello_ack is True
        assert result.pongs >= 1
        assert book.offset_ms is not None
        # laptop - phone ≈ -180. Allow jitter from scheduling.
        assert abs(book.offset_ms - (-180)) < 80
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_usb_wifi_cell_profiles_still_sync() -> None:
    server, task = await _serve(18804)
    try:
        frames = [(render_obstacle(1.8), 1.8, i * 0.04) for i in range(6)]
        for profile in (USB, WIFI, CELL):
            book.reset()
            engine.reset()
            result = await run_simulation(
                "ws://127.0.0.1:18804/v1/stream",
                frames,
                profile=profile,
                seed=3,
                wait_after_s=0.6,
            )
            assert result.hello_ack is True
            assert result.frames_sent + result.frames_dropped == 6
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_reconnect_mid_stream(clean_state) -> None:
    server, task = await _serve(18805)
    try:
        frames = [(render_obstacle(1.5), 1.5, i * 0.04) for i in range(8)]
        result = await run_simulation(
            "ws://127.0.0.1:18805/v1/stream",
            frames,
            profile=USB,
            reconnect_after=3,
            wait_after_s=0.6,
        )
        assert result.hello_ack is True
        assert result.frames_sent >= 6
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_quality_switch_gap_does_not_false_warn(clean_state) -> None:
    server, task = await _serve(18806)
    try:
        # Same distance before and after a 700 ms hole — must not invent a slam.
        frames = [(render_obstacle(1.4), 1.4, i * 0.1) for i in range(4)]
        result = await run_simulation(
            "ws://127.0.0.1:18806/v1/stream",
            frames,
            profile=USB,
            extra_gap_s=0.7,
            gap_after=2,
            wait_after_s=0.5,
        )
        warns = [s for s in result.says if s.get("urgent")]
        assert warns == []
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_latest_frame_drops_backlog_when_model_is_slow(clean_state) -> None:
    server, task = await _serve(18807)
    try:

        def slow() -> float:
            time.sleep(0.25)
            return 2.0

        engine.set_estimator(FixedEstimator(fn=slow))
        start = time.monotonic()
        frames = [(render_obstacle(2.0), 2.0, i * 0.03) for i in range(10)]
        await run_simulation(
            "ws://127.0.0.1:18807/v1/stream",
            frames,
            profile=NetProfile("fast", uplink_ms=1, downlink_ms=1, tts_ms=1),
            wait_after_s=0.8,
        )
        elapsed = time.monotonic() - start
        # 10 frames * 250 ms would be 2.5 s if they queued. Latest-only stays shorter.
        assert engine.processed < 10
        assert elapsed < 2.3
    finally:
        engine.set_estimator(engine.estimator)
        from server.proximity import CorridorLoomingEstimator

        engine.set_estimator(CorridorLoomingEstimator())
        await _stop(server, task)


@pytest.mark.asyncio
async def test_urgent_say_carries_capture_ts(clean_state) -> None:
    server, task = await _serve(18808)
    try:
        async with websockets.connect("ws://127.0.0.1:18808/v1/stream") as ws:
            await ws.recv()
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "http://127.0.0.1:18808/say",
                    json={"text": "stop, obstacle ahead", "urgent": True, "capture_ts": 4242},
                )
            assert r.status_code == 200
            incoming = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert incoming["type"] == "say"
            assert incoming["urgent"] is True
            assert incoming["capture_ts"] == 4242
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_tts_start_updates_e2e(clean_state) -> None:
    server, task = await _serve(18809)
    try:
        async with websockets.connect("ws://127.0.0.1:18809/v1/stream") as ws:
            await ws.recv()
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://127.0.0.1:18809/say",
                    json={"text": "hello", "urgent": False, "capture_ts": 1000},
                )
            await asyncio.wait_for(ws.recv(), timeout=2)
            await ws.send(
                proto.encode_json(
                    proto.MsgType.TTS_START,
                    capture_ts=1000,
                    tts_start_ts=1280,
                    say_recv_ts=1200,
                    e2e_ms=280,
                )
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and book.e2e.summary()["n"] == 0:
                await asyncio.sleep(0.05)
        assert book.e2e.summary()["p50"] == 280
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_approach_warns_early_not_late_under_latency(clean_state) -> None:
    server, task = await _serve(18810)
    try:
        for _ in range(10):
            book.remaining.add(350)
        seq = approach_sequence(start_m=1.8, end_m=0.12, speed_mps=1.4, fps=10, width=160, height=120)
        frames = [(f.jpeg, f.distance_m, f.t_scene_s) for f in seq]
        profile = NetProfile("lag", uplink_ms=40, downlink_ms=30, jitter_ms=0, tts_ms=20)
        result = await run_simulation(
            "ws://127.0.0.1:18810/v1/stream",
            frames,
            profile=profile,
            wait_after_s=1.0,
        )
        warns = [s for s in result.says if s.get("urgent") and "obstacle" in s.get("text", "")]
        assert warns, f"expected a proximity warning, got {result.says}"
        assert result.first_warn_true_distance_m is not None
        # Warned while the object was still farther than one foot in the scene.
        assert result.first_warn_true_distance_m > 0.3
        # Cooldown must stop a flap of urgent repeats.
        assert len(warns) <= 2
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_ping_pong_roundtrip(clean_state) -> None:
    server, task = await _serve(18811)
    try:
        async with websockets.connect("ws://127.0.0.1:18811/v1/stream") as ws:
            await ws.recv()
            t0 = proto.now_ms()
            await ws.send(proto.encode_json(proto.MsgType.PING, t=t0, t0=t0))
            pong = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert pong["type"] == "pong"
            assert pong["t0"] == t0
            assert "t1" in pong and "t2" in pong
            t3 = proto.now_ms()
            await ws.send(
                proto.encode_json(proto.MsgType.SYNC, t0=t0, t1=pong["t1"], t2=pong["t2"], t3=t3)
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and book.offset_ms is None:
                await asyncio.sleep(0.05)
        assert book.offset_ms is not None
        assert book.offset_rtt_ms is not None
        assert book.offset_rtt_ms < 200
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_bright_wall_af_approach_warns_before_one_foot(clean_state) -> None:
    server, task = await _serve(18813)
    try:
        for _ in range(10):
            book.remaining.add(200)
        seq = approach_sequence(start_m=0.6, end_m=0.2, speed_mps=0.45, fps=8, kind="bright_wall")
        frames = [
            (f.jpeg, f.distance_m, f.t_scene_s, focus_meta_for_distance(f.distance_m)) for f in seq
        ]
        profile = NetProfile("usb-wall", uplink_ms=12, downlink_ms=12, jitter_ms=0, tts_ms=20)
        result = await run_simulation(
            "ws://127.0.0.1:18813/v1/stream",
            frames,
            profile=profile,
            wait_after_s=1.0,
        )
        warns = [s for s in result.says if s.get("urgent") and "obstacle" in s.get("text", "")]
        assert warns, f"expected a proximity warning on the bright wall, got {result.says}"
        assert result.first_warn_true_distance_m is not None
        assert result.first_warn_true_distance_m > 0.3
        async with httpx.AsyncClient() as client:
            trace = (await client.get("http://127.0.0.1:18813/proximity_trace?n=40")).json()
            assert trace["ok"] is True
            assert trace["n"] >= 1
            assert any(row.get("af_m") is not None for row in trace["rows"])
    finally:
        await _stop(server, task)


def test_simulated_tts_urgent_interrupts_backlog() -> None:
    tts = SimulatedTts(tts_ms=0)
    tts.enqueue(OutgoingSay("frame received, 8 per second", False, 1, 1))
    tts.enqueue(OutgoingSay("nice day", False, 2, 2))
    tts.enqueue(OutgoingSay("stop, obstacle ahead", True, 3, 3))
    assert tts.interrupted == 1
    assert len(tts.queue) == 1
    assert tts.queue[0].text.startswith("stop")


@pytest.mark.asyncio
async def test_dropped_frames_are_counted() -> None:
    server, task = await _serve(18812)
    try:
        frames = [(render_obstacle(1.0), 1.0, i * 0.02) for i in range(5)]
        profile = NetProfile("drop", uplink_ms=1, downlink_ms=1, drop_rate=1.0, tts_ms=1)
        result = await run_simulation(
            "ws://127.0.0.1:18812/v1/stream",
            frames,
            profile=profile,
            wait_after_s=0.2,
        )
        assert result.frames_sent == 0
        assert result.frames_dropped == 5
    finally:
        await _stop(server, task)
