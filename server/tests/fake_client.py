"""Scripted fake phone: send JPEGs, receive say/play. Proves the server + protocol.

Usage (server already running)::

    python -m server.tests.fake_client --url ws://127.0.0.1:8765/v1/stream

Or as a pytest that boots uvicorn in-process (see test_fake_client.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

from server import protocol as proto

# 1x1 JPEG
MINI_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb0043000808080808080808"
    "080808080808080808080808080808080808080808080808080808080808080808"
    "080808080808080808080808080808080808080808ffc000110800010001030122"
    "00021101031101ffc4001f00000105010101010101000000000000000001020304"
    "05060708090a0bffc400b5100002010303020403050504040000017d0102030004"
    "1105122131410613516107227114328191a1082342b1c11552d1f0243362728209"
    "0a161718191a25262728292a3435363738393a434445464748494a535455565758"
    "595a636465666768696a737475767778797a838485868788898a92939495969798"
    "999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5"
    "d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda000c03010002"
    "110311003f00aafe28a2803fffd9"
)


async def run_fake_client(
    url: str,
    frames: int = 20,
    fps: float = 8.0,
    wait_say_sec: float = 8.0,
) -> dict:
    received: list[dict] = []
    audio_clips = 0
    hello_ack = False

    async with websockets.connect(url, max_size=8_000_000) as ws:
        await ws.send(proto.encode_json(proto.MsgType.HELLO, role="phone", app="fake-client", app_ver="0.1.0"))
        await ws.send(
            proto.encode_json(
                proto.MsgType.CONFIG,
                width=640,
                height=480,
                fps=int(fps),
                quality=70,
                mic=False,
            )
        )

        interval = 1.0 / max(fps, 0.1)
        sent = 0

        async def sender() -> None:
            nonlocal sent
            for i in range(frames):
                payload = proto.encode_binary(proto.Kind.JPEG_FRAME, MINI_JPEG, timestamp_ms=proto.now_ms())
                await ws.send(payload)
                sent += 1
                if i + 1 < frames:
                    await asyncio.sleep(interval)

        async def receiver() -> None:
            nonlocal hello_ack, audio_clips
            deadline = time.monotonic() + wait_say_sec + frames / max(fps, 0.1)
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except TimeoutError:
                    break
                if isinstance(raw, bytes):
                    msg = proto.decode_binary(raw)
                    if msg.kind is proto.Kind.AUDIO_CLIP:
                        audio_clips += 1
                        received.append({"type": "audio", "bytes": len(msg.payload), "flags": msg.flags})
                else:
                    obj = proto.decode_json(raw)
                    received.append(obj)
                    if obj["type"] == proto.MsgType.HELLO_ACK:
                        hello_ack = True
                    if obj["type"] == proto.MsgType.SAY:
                        # One spoken demo line is enough to prove the loop.
                        if any(r.get("type") == proto.MsgType.SAY for r in received[:-1]):
                            return

        await asyncio.gather(sender(), receiver())

    says = [r for r in received if r.get("type") == "say"]
    result = {
        "hello_ack": hello_ack,
        "frames_sent": sent,
        "says": says,
        "audio_clips": audio_clips,
        "received": received,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fake SAW phone client")
    p.add_argument("--url", default="ws://127.0.0.1:8765/v1/stream")
    p.add_argument("--frames", type=int, default=20)
    p.add_argument("--fps", type=float, default=8)
    p.add_argument("--wait-say", type=float, default=8)
    args = p.parse_args(argv)

    result = asyncio.run(run_fake_client(args.url, args.frames, args.fps, args.wait_say))
    print(json.dumps({k: v for k, v in result.items() if k != "received"}, indent=2))
    print("messages:", json.dumps(result["received"], indent=2))
    ok = result["hello_ack"] and result["frames_sent"] > 0 and len(result["says"]) >= 1
    if not ok:
        print("FAIL: expected hello_ack + at least one say from the demo stub", file=sys.stderr)
        return 1
    print("OK: protocol loop works (hello_ack, frames, say)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
