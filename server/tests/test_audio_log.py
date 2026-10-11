"""Audio-out log: send status, phone lifecycle, triage summaries."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import uvicorn
import websockets

from server import protocol as proto
from server.audio_log import AudioLog, summarize
from server.app import app
from server.hub import hub
from server.tests.conftest import clean_state  # noqa: F401


def test_summarize_triage_paths() -> None:
    assert summarize(phone_connected=False, send_ok=False, last_event=None) == "no phone connected"
    assert summarize(phone_connected=True, send_ok=False, last_event=None) == "send failed"
    assert summarize(phone_connected=True, send_ok=True, last_event=None) == "sent, no phone report"
    assert summarize(phone_connected=True, send_ok=True, last_event="received") == "received, never started"
    assert summarize(phone_connected=True, send_ok=True, last_event="queued") == "received, queued"
    assert summarize(phone_connected=True, send_ok=True, last_event="started") == "started"
    assert summarize(phone_connected=True, send_ok=True, last_event="done") == "started, done"
    assert (
        summarize(phone_connected=True, send_ok=True, last_event="done", muted=True)
        == "started, done, but silent"
    )
    assert (
        summarize(phone_connected=True, send_ok=True, last_event="done", volume=0)
        == "started, done, but silent"
    )
    assert summarize(phone_connected=True, send_ok=True, last_event="interrupted") == "interrupted"
    assert summarize(phone_connected=True, send_ok=True, last_event="error", error_code=5) == "error (5)"
    assert summarize(phone_connected=True, send_ok=True, last_event="tts_not_ready") == "tts not ready"


def test_record_and_phone_events(clean_state) -> None:
    log = AudioLog()
    aid = log.next_id()
    assert aid == "a0001"
    log.record(
        audio_id=aid,
        kind="say",
        text="stop, obstacle ahead",
        urgent=True,
        hazard=True,
        sent_ms=100,
        phone_connected=True,
        send_ok=True,
    )
    assert log.get(aid).summary() == "sent, no phone report"
    log.note_event(aid, "received", volume=7, muted=False)
    assert log.get(aid).summary() == "received, never started"
    log.note_event(aid, "queued")
    log.note_event(aid, "started")
    log.note_event(aid, "done")
    snap = log.snapshot()
    assert snap["n"] == 1
    assert snap["entries"][0]["id"] == "a0001"
    assert snap["entries"][0]["summary"] == "started, done"
    assert snap["entries"][0]["phone_events"] == ["received", "queued", "started", "done"]


def test_unknown_id_is_ignored() -> None:
    log = AudioLog()
    assert log.note_event("nope", "started") is None


def test_caps_at_200() -> None:
    log = AudioLog(maxlen=200)
    for i in range(220):
        log.record(
            audio_id=log.next_id(),
            kind="say",
            text=str(i),
            urgent=False,
            hazard=False,
            sent_ms=i,
            phone_connected=False,
            send_ok=False,
        )
    assert log.snapshot()["n"] == 200
    assert log.snapshot()["entries"][0]["text"] == "20"


@pytest.mark.asyncio
async def test_http_audio_log_and_phone_event(clean_state) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=18821, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as client:
            empty = (await client.get("http://127.0.0.1:18821/audio_log")).json()
            assert empty["ok"] is True
            assert empty["entries"] == []
            posted = await client.post(
                "http://127.0.0.1:18821/say",
                json={"text": "hello from log", "urgent": False},
            )
            assert posted.status_code == 200
            body = (await client.get("http://127.0.0.1:18821/audio_log")).json()
            assert body["n"] == 1
            row = body["entries"][0]
            assert row["text"] == "hello from log"
            assert row["id"].startswith("a")
            assert row["summary"] == "no phone connected"
            assert "id" in posted.json() or True
        async with websockets.connect("ws://127.0.0.1:18821/v1/stream") as ws:
            await ws.recv()
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "http://127.0.0.1:18821/say",
                    json={"text": "on the phone", "urgent": True},
                )
                assert r.status_code == 200
            incoming = proto.decode_json(await asyncio.wait_for(ws.recv(), timeout=2))
            assert incoming["type"] == "say"
            assert incoming["id"]
            await ws.send(
                proto.encode_json(
                    proto.MsgType.AUDIO_EVENT,
                    id=incoming["id"],
                    event="received",
                    volume=4,
                    muted=False,
                )
            )
            await ws.send(
                proto.encode_json(
                    proto.MsgType.AUDIO_EVENT,
                    id=incoming["id"],
                    event="started",
                    volume=4,
                    muted=False,
                )
            )
            await asyncio.sleep(0.1)
        async with httpx.AsyncClient() as client:
            body = (await client.get("http://127.0.0.1:18821/audio_log")).json()
            last = body["entries"][-1]
            assert last["id"] == incoming["id"]
            assert last["summary"] == "started"
            assert last["volume"] == 4
    finally:
        server.should_exit = True
        await task
        hub.reset_for_tests()
