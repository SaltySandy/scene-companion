"""SAW protocol v1 — binary envelope + JSON control messages.

Binary WebSocket frames (phone <-> laptop)::

    Offset  Size  Field
    0       4     magic = b"SAW1"
    4       1     kind
    5       1     flags
    6       2     reserved (0)
    8       8     timestamp_ms (uint64 big-endian, Unix epoch ms)
    16      4     payload_len (uint32 big-endian)
    20      N     payload

Kinds:
    JPEG_FRAME = 0x01   phone -> laptop   payload = JPEG bytes
    MIC_PCM    = 0x02   phone -> laptop   payload = PCM16LE mono 16 kHz
    AUDIO_CLIP = 0x03   laptop -> phone   flags: 0=wav, 1=mp3, 2=pcm16le_16khz_mono

JSON text frames always include ``{"v": 1, "type": "..."}``.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

PROTO_VERSION = 1
MAGIC = b"SAW1"
HEADER_SIZE = 20
HEADER_STRUCT = struct.Struct("!4sBBHQI")  # magic, kind, flags, reserved, ts, len


class Kind(IntEnum):
    JPEG_FRAME = 0x01
    MIC_PCM = 0x02
    AUDIO_CLIP = 0x03


class AudioFlag(IntEnum):
    WAV = 0
    MP3 = 1
    PCM16LE_16KHZ_MONO = 2


class MsgType:
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    PING = "ping"
    PONG = "pong"
    SAY = "say"
    CONFIG = "config"
    STATUS = "status"
    STATS = "stats"
    MIC = "mic"
    SPEAKING = "speaking"
    TRANSCRIPT = "transcript"
    WALL = "wall"  # laptop -> viewer: wall detector status
    ASSISTANT = "assistant"  # laptop -> viewer: "What is this?" progress
    ERROR = "error"
    PROXIMITY = "proximity"
    ACK = "ack"  # phone -> laptop: {frame_key, cue_start_ts (phone clock), state}
    FOV = "fov"  # laptop -> viewer only: effective depth band the sensor used


# Proximity heartbeat (laptop -> phone and viewers). state "unknown" means the
# laptop cannot vouch for the path ahead; the phone must treat it like silence.
PROXIMITY_OK = "ok"
PROXIMITY_UNKNOWN = "unknown"
LEVELS = ("clear", "near", "danger")
SECTORS = ("left", "center", "right")


@dataclass(frozen=True)
class BinaryMessage:
    kind: Kind
    flags: int
    timestamp_ms: int
    payload: bytes

    def encode(self) -> bytes:
        return encode_binary(self.kind, self.payload, self.timestamp_ms, self.flags)


def encode_binary(
    kind: Kind | int,
    payload: bytes,
    timestamp_ms: int | None = None,
    flags: int = 0,
) -> bytes:
    if timestamp_ms is None:
        timestamp_ms = now_ms()
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    header = HEADER_STRUCT.pack(MAGIC, int(kind), int(flags) & 0xFF, 0, int(timestamp_ms), len(payload))
    return header + payload


def decode_binary(data: bytes) -> BinaryMessage:
    if len(data) < HEADER_SIZE:
        raise ValueError(f"binary frame too short: {len(data)} < {HEADER_SIZE}")
    magic, kind, flags, _reserved, ts, payload_len = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
    if magic != MAGIC:
        raise ValueError(f"bad magic: {magic!r}")
    if len(data) < HEADER_SIZE + payload_len:
        raise ValueError(f"truncated payload: have {len(data) - HEADER_SIZE}, want {payload_len}")
    try:
        kind_enum = Kind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown kind: {kind}") from exc
    payload = data[HEADER_SIZE : HEADER_SIZE + payload_len]
    return BinaryMessage(kind=kind_enum, flags=flags, timestamp_ms=ts, payload=payload)


def encode_json(msg_type: str, **fields: Any) -> str:
    body: dict[str, Any] = {"v": PROTO_VERSION, "type": msg_type}
    body.update(fields)
    return json.dumps(body, separators=(",", ":"))


def decode_json(text: str) -> dict[str, Any]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("JSON message must be an object")
    if obj.get("v") != PROTO_VERSION:
        raise ValueError(f"unsupported protocol version: {obj.get('v')}")
    if "type" not in obj:
        raise ValueError("JSON message missing type")
    return obj


def encode_proximity(
    seq: int,
    state: str,
    level: str | None = None,
    levels: dict[str, str] | None = None,
    sectors_m: dict[str, float] | None = None,
    frame_key: int | None = None,
    age_ms: int | None = None,
) -> str:
    if state not in (PROXIMITY_OK, PROXIMITY_UNKNOWN):
        raise ValueError(f"bad proximity state: {state}")
    if state == PROXIMITY_OK and level not in LEVELS:
        raise ValueError(f"bad proximity level: {level}")
    return encode_json(
        MsgType.PROXIMITY,
        seq=seq,
        state=state,
        level=level if state == PROXIMITY_OK else None,
        levels=levels,
        sectors_m=sectors_m,
        frame_key=frame_key,
        age_ms=age_ms,
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def mime_to_flag(mime: str) -> int:
    mime = (mime or "").lower()
    if "mpeg" in mime or mime.endswith("mp3") or mime == "audio/mp3":
        return AudioFlag.MP3
    if "wav" in mime or "wave" in mime:
        return AudioFlag.WAV
    if "pcm" in mime or "l16" in mime or "raw" in mime:
        return AudioFlag.PCM16LE_16KHZ_MONO
    return AudioFlag.WAV


def flag_to_mime(flags: int) -> str:
    if flags == AudioFlag.MP3:
        return "audio/mpeg"
    if flags == AudioFlag.PCM16LE_16KHZ_MONO:
        return "audio/l16"
    return "audio/wav"
