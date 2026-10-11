"""Scripted fake phone + replay/simulation harness.

Usage (server already running)::

    python -m server.tests.fake_client --url ws://127.0.0.1:8765/v1/stream

The harness can inject uplink/downlink delay, jitter, clock skew, frame drops,
and synthetic approaching-object JPEGs with known ground truth. It also models
a TTS queue so tests can prove urgent speech interrupts a backlog.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

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


@dataclass
class NetProfile:
    name: str
    uplink_ms: float
    downlink_ms: float
    jitter_ms: float = 0.0
    drop_rate: float = 0.0
    skew_ms: float = 0.0
    drift_ppm: float = 0.0
    tts_ms: float = 40.0
    asymmetric_down_extra_ms: float = 0.0


USB = NetProfile("usb", uplink_ms=12, downlink_ms=12, jitter_ms=2, tts_ms=30)
WIFI = NetProfile("wifi", uplink_ms=40, downlink_ms=45, jitter_ms=25, drop_rate=0.03, skew_ms=80, tts_ms=40)
CELL = NetProfile(
    "cell",
    uplink_ms=70,
    downlink_ms=70,
    jitter_ms=50,
    drop_rate=0.06,
    skew_ms=250,
    asymmetric_down_extra_ms=80,
    tts_ms=50,
)


@dataclass
class OutgoingSay:
    text: str
    urgent: bool
    capture_ts: int | None
    recv_phone_ms: int


class SimulatedTts:
    """In-process stand-in for SpeechQueue: urgent flushes and interrupts."""

    def __init__(self, tts_ms: float = 40.0) -> None:
        self.tts_ms = tts_ms
        self.queue: list[OutgoingSay] = []
        self.started: list[dict[str, Any]] = []
        self.interrupted = 0
        self.speaking: OutgoingSay | None = None
        self._task: asyncio.Task[None] | None = None

    def enqueue(self, item: OutgoingSay) -> None:
        if item.urgent:
            if self.queue or self.speaking is not None:
                self.interrupted += 1
            self.queue.clear()
            self.speaking = None
            if self._task and not self._task.done():
                self._task.cancel()
        self.queue.append(item)

    async def drain(self, on_start: Callable[[OutgoingSay, int], Any] | None = None) -> None:
        while self.queue:
            item = self.queue.pop(0)
            self.speaking = item
            try:
                await asyncio.sleep(self.tts_ms / 1000.0)
            except asyncio.CancelledError:
                self.speaking = None
                return
            if self.speaking is not item:
                return
            wall = proto.now_ms()
            self.started.append(
                {
                    "text": item.text,
                    "capture_ts": item.capture_ts,
                    "tts_start_ts": wall,
                    "urgent": item.urgent,
                }
            )
            if on_start is not None:
                await on_start(item, wall)
            self.speaking = None


@dataclass
class SimResult:
    hello_ack: bool = False
    frames_sent: int = 0
    frames_dropped: int = 0
    says: list[dict] = field(default_factory=list)
    tts_started: list[dict] = field(default_factory=list)
    interrupted: int = 0
    received: list[dict] = field(default_factory=list)
    audio_clips: int = 0
    pongs: int = 0
    first_warn_capture_ts: int | None = None
    first_warn_true_distance_m: float | None = None
    first_warn_true_at_tts_m: float | None = None


def _jitter(profile: NetProfile, rng: random.Random) -> float:
    if profile.jitter_ms <= 0:
        return 0.0
    return rng.uniform(-profile.jitter_ms, profile.jitter_ms)


class PhoneClock:
    """Phone wall clock = laptop wall + skew + optional linear drift."""

    def __init__(self, skew_ms: float = 0.0, drift_ppm: float = 0.0) -> None:
        self.skew_ms = skew_ms
        self.drift_ppm = drift_ppm
        self.origin = time.time()

    def now_ms(self) -> int:
        elapsed = time.time() - self.origin
        drifted = elapsed * (1.0 + self.drift_ppm / 1e6)
        return int((self.origin + drifted) * 1000.0 + self.skew_ms)


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


async def run_simulation(
    url: str,
    frames: Iterable[tuple[bytes, float, float]],
    *,
    profile: NetProfile = USB,
    seed: int = 1,
    ping: bool = True,
    report_tts: bool = True,
    extra_gap_s: float = 0.0,
    gap_after: int = -1,
    reconnect_after: int = -1,
    wait_after_s: float = 1.2,
    latest_only_hold_s: float = 0.0,
) -> SimResult:
    """Replay a sequence of ``(jpeg, true_distance_m, t_scene_s)``.

    ``t_scene_s`` is scene time (seconds from the start). Capture timestamps
    are ``phone_clock`` at send time minus the (already elapsed) scene time
    so a delayed send still carries the *true* capture moment.
    """
    rng = random.Random(seed)
    clock = PhoneClock(skew_ms=profile.skew_ms, drift_ppm=profile.drift_ppm)
    tts = SimulatedTts(tts_ms=profile.tts_ms)
    result = SimResult()
    seq = list(frames)
    truth_by_ts: dict[int, tuple[float, float]] = {}

    async def open_socket():
        ws = await websockets.connect(url, max_size=8_000_000)
        await ws.send(proto.encode_json(proto.MsgType.HELLO, role="phone", app="sim", app_ver="0.1.0"))
        await ws.send(
            proto.encode_json(
                proto.MsgType.CONFIG,
                width=160,
                height=120,
                fps=8,
                quality=70,
                mic=False,
                level="low",
            )
        )
        return ws

    async def handle_text(ws, obj: dict) -> None:
        result.received.append(obj)
        if obj["type"] == proto.MsgType.HELLO_ACK:
            result.hello_ack = True
        elif obj["type"] == proto.MsgType.PONG:
            result.pongs += 1
            t0 = obj.get("t0", obj.get("t"))
            t1 = obj.get("t1")
            t2 = obj.get("t2")
            if t0 is not None and t1 is not None and t2 is not None:
                t3 = clock.now_ms()
                await ws.send(
                    proto.encode_json(
                        proto.MsgType.SYNC,
                        t0=t0,
                        t1=t1,
                        t2=t2,
                        t3=t3,
                    )
                )
        elif obj["type"] == proto.MsgType.SAY:
            result.says.append(obj)
            recv = clock.now_ms()
            item = OutgoingSay(
                text=str(obj.get("text") or ""),
                urgent=bool(obj.get("urgent", False)),
                capture_ts=obj.get("capture_ts"),
                recv_phone_ms=recv,
            )
            tts.enqueue(item)
            if item.urgent and item.capture_ts is not None and result.first_warn_capture_ts is None:
                result.first_warn_capture_ts = int(item.capture_ts)
                truth = truth_by_ts.get(int(item.capture_ts))
                if truth is not None:
                    result.first_warn_true_distance_m = truth[0]

            async def on_start(spoken: OutgoingSay, wall: int) -> None:
                if spoken.capture_ts is not None and report_tts:
                    await ws.send(
                        proto.encode_json(
                            proto.MsgType.TTS_START,
                            capture_ts=int(spoken.capture_ts),
                            tts_start_ts=wall,
                            say_recv_ts=spoken.recv_phone_ms,
                            e2e_ms=wall - int(spoken.capture_ts),
                        )
                    )
                if spoken.urgent and spoken.capture_ts is not None:
                    truth = truth_by_ts.get(int(spoken.capture_ts))
                    if truth is not None and result.first_warn_true_at_tts_m is None:
                        # Distance at hear-time = distance_at_capture - speed * e2e
                        # speed is recovered from the sequence spacing by the caller.
                        result.first_warn_true_at_tts_m = truth[0]

            await tts.drain(on_start)

    ws = await open_socket()
    try:
        origin = time.time()
        for i, item in enumerate(seq):
            if len(item) == 4:
                jpeg, dist_m, t_scene, focus = item
            else:
                jpeg, dist_m, t_scene = item
                focus = None
            if reconnect_after >= 0 and i == reconnect_after:
                await ws.close()
                ws = await open_socket()
            if gap_after >= 0 and i == gap_after and extra_gap_s > 0:
                await asyncio.sleep(extra_gap_s)
            if profile.drop_rate > 0 and rng.random() < profile.drop_rate:
                result.frames_dropped += 1
                continue
            # Wait until scene time so fps is realistic (scaled by caller if desired).
            target = origin + t_scene
            delay = target - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            up = max(0.0, (profile.uplink_ms + _jitter(profile, rng)) / 1000.0)
            capture_ts = clock.now_ms()
            truth_by_ts[capture_ts] = (dist_m, t_scene)
            if latest_only_hold_s > 0 and i == 0:
                # Hold the first frame on the client to simulate a slow sender;
                # later frames still replace it on the server (latest-only).
                await asyncio.sleep(latest_only_hold_s)
            await asyncio.sleep(up)
            if focus:
                meta = dict(focus)
                meta.setdefault("ts", capture_ts)
                await ws.send(proto.encode_json(proto.MsgType.FRAME_META, **meta))
            await ws.send(proto.encode_binary(proto.Kind.JPEG_FRAME, jpeg, timestamp_ms=capture_ts))
            result.frames_sent += 1
            if ping and i % 4 == 0:
                await ws.send(proto.encode_json(proto.MsgType.PING, t=clock.now_ms(), t0=clock.now_ms()))

            # Drain inbound without blocking the send loop for long.
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    if isinstance(raw, bytes):
                        msg = proto.decode_binary(raw)
                        if msg.kind is proto.Kind.AUDIO_CLIP:
                            result.audio_clips += 1
                    else:
                        down = max(0.0, (profile.downlink_ms + profile.asymmetric_down_extra_ms) / 1000.0)
                        if down:
                            await asyncio.sleep(down)
                        await handle_text(ws, proto.decode_json(raw))
            except TimeoutError:
                pass

        deadline = time.monotonic() + wait_after_s
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
            except TimeoutError:
                continue
            if isinstance(raw, str):
                down = max(0.0, (profile.downlink_ms + profile.asymmetric_down_extra_ms) / 1000.0)
                if down:
                    await asyncio.sleep(down)
                await handle_text(ws, proto.decode_json(raw))
    finally:
        result.tts_started = list(tts.started)
        result.interrupted = tts.interrupted
        try:
            await ws.close()
        except Exception:
            pass
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
