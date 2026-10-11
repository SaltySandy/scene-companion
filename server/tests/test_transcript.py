"""VAD boundaries, half-duplex mute, and a real WAV through the WebSocket."""

from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path

import pytest

from server import protocol as proto
from server.transcribe import LiveTranscriber
from server.vad import FRAME_BYTES, UtteranceSplitter, frame_rms

FIXTURE = Path(__file__).parent / "fixtures" / "turn_left.wav"


def _tone(ms: int, amplitude: int) -> bytes:
    frames = SAMPLE_RATE * ms // 1000
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return sample * frames


SAMPLE_RATE = 16_000


def test_silence_and_short_blip_are_dropped() -> None:
    splitter = UtteranceSplitter(min_speech_ms=200, end_silence_ms=100, partial_every_ms=10_000)
    assert splitter.push(_tone(100, 0), 1) == []
    pieces = splitter.push(_tone(100, 8000) + _tone(200, 0), 2)
    assert pieces == []


def test_speech_then_silence_emits_one_final() -> None:
    splitter = UtteranceSplitter(min_speech_ms=200, end_silence_ms=100, partial_every_ms=10_000)
    pcm = _tone(300, 8000) + _tone(200, 0)
    pieces = splitter.push(pcm, 50)
    finals = [p for p in pieces if p.is_final]
    assert len(finals) == 1
    assert finals[0].ts == 50
    assert len(finals[0].pcm) >= 300 * SAMPLE_RATE // 1000 * 2


def test_partial_fires_before_final() -> None:
    splitter = UtteranceSplitter(min_speech_ms=200, end_silence_ms=100, partial_every_ms=400)
    pieces = splitter.push(_tone(900, 9000) + _tone(200, 0), 7)
    assert any(not p.is_final for p in pieces)
    assert pieces[-1].is_final
    assert frame_rms(_tone(FRAME_BYTES // 2 * 20, 0)) == 0


def test_phone_speaking_drops_audio() -> None:
    seen: list[bytes] = []

    def fake(pcm: bytes) -> str:
        seen.append(pcm)
        return "turn left"

    worker = LiveTranscriber(transcribe_fn=fake)
    worker.set_phone_speaking(True)
    worker.push_pcm(_tone(400, 9000), 1)
    assert worker._queue.qsize() == 0
    worker.set_phone_speaking(False)
    # The phone flag clears with a short tail so the speaker decay is not transcribed.
    worker.push_pcm(_tone(200, 9000), 2)
    assert worker._queue.qsize() == 0
    worker.mute_until = 0
    worker.push_pcm(_tone(400, 9000) + _tone(600, 0), 3)
    assert worker._queue.qsize() == 1


@pytest.mark.asyncio
async def test_wav_through_protocol_returns_final_transcript() -> None:
    os.environ["WHISPER_MODEL"] = "tiny.en"
    import server.ai_hook as hook

    previous_announce = hook.ANNOUNCE_EVERY_SEC
    previous_announce_env = os.environ.get("SAW_DEMO_ANNOUNCE_SEC")
    hook.ANNOUNCE_EVERY_SEC = 0
    os.environ["SAW_DEMO_ANNOUNCE_SEC"] = "0"

    import uvicorn
    import websockets

    from server.app import app
    from server.transcribe import transcriber

    # This process may already have a transcriber thread from another test,
    # and a previous say() may still be inside its echo-mute window.
    transcriber._model = None
    transcriber._model_name = ""
    transcriber.phone_speaking = False
    transcriber.mute_until = 0

    config = uvicorn.Config(app, host="127.0.0.1", port=18770, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started

        with wave.open(str(FIXTURE)) as wav:
            assert wav.getframerate() == 16_000
            assert wav.getnchannels() == 1
            pcm = wav.readframes(wav.getnframes())
        pcm += b"\x00" * (16_000 * 2 * 8 // 10)  # 800 ms silence so VAD can close

        async with websockets.connect("ws://127.0.0.1:18770/v1/viewer") as viewer, websockets.connect(
            "ws://127.0.0.1:18770/v1/stream"
        ) as phone:
            await asyncio.wait_for(phone.recv(), timeout=2)
            await asyncio.wait_for(viewer.recv(), timeout=2)
            await phone.send(proto.encode_json(proto.MsgType.HELLO, role="phone", app="fake-client"))
            view = asyncio.create_task(_collect_transcripts(viewer))
            for offset in range(0, len(pcm), 3200):
                chunk = pcm[offset : offset + 3200]
                await phone.send(proto.encode_binary(proto.Kind.MIC_PCM, chunk, timestamp_ms=proto.now_ms()))
            said = await _wait_for_say(phone, timeout=60)
            finals = await asyncio.wait_for(view, timeout=5)
    finally:
        hook.ANNOUNCE_EVERY_SEC = previous_announce
        if previous_announce_env is None:
            os.environ.pop("SAW_DEMO_ANNOUNCE_SEC", None)
        else:
            os.environ["SAW_DEMO_ANNOUNCE_SEC"] = previous_announce_env
        server.should_exit = True
        await task

    assert "you said:" in said.lower()
    assert "turn" in said.lower()
    assert "left" in said.lower()
    assert finals
    assert any("turn" in text.lower() and "left" in text.lower() for text in finals)


async def _wait_for_say(ws, timeout: float) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError("timed out waiting for You said")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            continue
        obj = proto.decode_json(raw)
        if obj["type"] == proto.MsgType.SAY and "you said" in str(obj.get("text", "")).lower():
            return str(obj["text"])


async def _collect_transcripts(ws) -> list[str]:
    found: list[str] = []
    while True:
        raw = await ws.recv()
        if isinstance(raw, bytes):
            continue
        obj = proto.decode_json(raw)
        if obj["type"] == proto.MsgType.TRANSCRIPT and obj.get("final"):
            found.append(str(obj.get("text") or ""))
            return found
