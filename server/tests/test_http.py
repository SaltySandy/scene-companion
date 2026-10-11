"""HTTP /say JSON errors (SIGHTLINE-SERVER-5) and phone TTS confirmation."""

from __future__ import annotations

import asyncio

import pytest

from server import protocol as proto
from server.hub import AUDIO_LOG_MAX
from server.tests.phone_helpers import free_port


@pytest.mark.asyncio
async def test_say_rejects_invalid_json_and_empty_text() -> None:
    import httpx
    import uvicorn

    from server.app import app

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    origin = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"
        async with httpx.AsyncClient() as client:
            bad = await client.post(
                f"{origin}/say",
                content='{"text":',
                headers={"content-type": "application/json"},
            )
            assert bad.status_code == 400
            assert bad.json() == {"ok": False, "error": "invalid JSON"}

            empty = await client.post(f"{origin}/say", json={"text": "   "})
            assert empty.status_code == 400
            assert empty.json()["error"] == "empty text"

            missing = await client.post(f"{origin}/say", json={"urgent": False})
            assert missing.status_code == 400
            assert missing.json()["error"] == "empty text"

            recall_bad = await client.post(
                f"{origin}/recall",
                content="{",
                headers={"content-type": "application/json"},
            )
            assert recall_bad.status_code == 400
            assert recall_bad.json()["error"] == "invalid JSON"

            prox_bad = await client.post(
                f"{origin}/debug/proximity",
                content="{",
                headers={"content-type": "application/json"},
            )
            assert prox_bad.status_code == 400
            assert prox_bad.json()["error"] == "invalid JSON"

            play_empty = await client.post(f"{origin}/play", content=b"")
            assert play_empty.status_code == 400
            assert play_empty.json()["error"] == "empty body"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_audio_log_records_that_the_phone_spoke() -> None:
    """A say is not confirmed until the phone reports tts_start / audio_event."""
    import httpx
    import uvicorn
    import websockets

    from server.app import app
    from server.hub import hub

    hub.audio_events.clear()
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    origin = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn did not start"
        async with websockets.connect(f"ws://127.0.0.1:{port}/v1/stream") as ws:
            first = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert first["type"] == "hello_ack"
            async with httpx.AsyncClient() as client:
                posted = await client.post(f"{origin}/say", json={"text": "curb in two steps"})
                assert posted.status_code == 200
                incoming = None
                while incoming is None or incoming.get("type") != "say":
                    incoming = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
                say_id = incoming["id"]
                assert incoming["text"] == "curb in two steps"
                await ws.send(
                    proto.encode_json(
                        proto.MsgType.TTS_START,
                        id=say_id,
                        text=incoming["text"],
                        tts_start_ts=proto.now_ms(),
                    )
                )
                await ws.send(
                    proto.encode_json(
                        proto.MsgType.AUDIO_EVENT,
                        id=say_id,
                        event="playing",
                        text=incoming["text"],
                    )
                )
                await ws.send(
                    proto.encode_json(
                        proto.MsgType.AUDIO_EVENT,
                        id=say_id,
                        event="ended",
                        text=incoming["text"],
                    )
                )
                events = []
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    events = (await client.get(f"{origin}/audio_log")).json()["events"]
                    if any(e.get("id") == say_id and e.get("event") == "ended" for e in events):
                        break
                assert any(e.get("type") == "say" and e.get("id") == say_id for e in events)
                assert any(e.get("type") == "tts_start" and e.get("id") == say_id for e in events)
                assert any(e.get("event") == "ended" and e.get("id") == say_id for e in events)
    finally:
        server.should_exit = True
        await task


def test_audio_log_drops_oldest() -> None:
    from server.hub import Hub

    h = Hub()
    for i in range(AUDIO_LOG_MAX + 3):
        h.note_audio({"type": "say", "id": f"say-{i}"})
    assert len(h.audio_events) == AUDIO_LOG_MAX
    assert h.audio_events[0]["id"] == "say-3"
    assert h.audio_events[-1]["id"] == f"say-{AUDIO_LOG_MAX + 2}"
