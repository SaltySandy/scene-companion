"""Unit tests for SAW protocol v1 framing."""

from __future__ import annotations

import pytest

from server import protocol as proto


MINI_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + bytes([8] * 64) + b"\xff\xd9"
)


def test_roundtrip_jpeg_frame() -> None:
    raw = proto.encode_binary(proto.Kind.JPEG_FRAME, MINI_JPEG, timestamp_ms=1_700_000_000_123)
    msg = proto.decode_binary(raw)
    assert msg.kind is proto.Kind.JPEG_FRAME
    assert msg.flags == 0
    assert msg.timestamp_ms == 1_700_000_000_123
    assert msg.payload == MINI_JPEG
    assert raw[:4] == proto.MAGIC
    assert len(raw) == proto.HEADER_SIZE + len(MINI_JPEG)


def test_roundtrip_audio_clip_flags() -> None:
    wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
    raw = proto.encode_binary(proto.Kind.AUDIO_CLIP, wav, timestamp_ms=42, flags=proto.AudioFlag.WAV)
    msg = proto.decode_binary(raw)
    assert msg.kind is proto.Kind.AUDIO_CLIP
    assert msg.flags == proto.AudioFlag.WAV
    assert msg.payload == wav
    assert proto.flag_to_mime(msg.flags) == "audio/wav"


def test_mic_pcm_roundtrip() -> None:
    pcm = bytes(range(256))
    msg = proto.decode_binary(proto.encode_binary(proto.Kind.MIC_PCM, pcm, timestamp_ms=9))
    assert msg.kind is proto.Kind.MIC_PCM
    assert msg.payload == pcm


def test_bad_magic() -> None:
    raw = proto.encode_binary(proto.Kind.JPEG_FRAME, b"x", timestamp_ms=1)
    raw = b"NOPE" + raw[4:]
    with pytest.raises(ValueError, match="bad magic"):
        proto.decode_binary(raw)


def test_truncated_header() -> None:
    with pytest.raises(ValueError, match="too short"):
        proto.decode_binary(b"SAW1xxxx")


def test_truncated_payload() -> None:
    raw = proto.encode_binary(proto.Kind.JPEG_FRAME, b"hello-jpeg", timestamp_ms=1)
    with pytest.raises(ValueError, match="truncated"):
        proto.decode_binary(raw[:-1])


def test_unknown_kind() -> None:
    raw = proto.encode_binary(proto.Kind.JPEG_FRAME, b"x", timestamp_ms=1)
    raw = raw[:4] + bytes([0x99]) + raw[5:]
    with pytest.raises(ValueError, match="unknown kind"):
        proto.decode_binary(raw)


def test_json_roundtrip() -> None:
    text = proto.encode_json(proto.MsgType.SAY, text="door on the left", urgent=True)
    obj = proto.decode_json(text)
    assert obj["v"] == 1
    assert obj["type"] == "say"
    assert obj["text"] == "door on the left"
    assert obj["urgent"] is True


def test_json_rejects_wrong_version() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        proto.decode_json('{"v":2,"type":"say"}')


def test_json_requires_type() -> None:
    with pytest.raises(ValueError, match="missing type"):
        proto.decode_json('{"v":1}')


def test_binary_message_encode_helper() -> None:
    msg = proto.BinaryMessage(proto.Kind.JPEG_FRAME, 0, 99, b"abc")
    assert proto.decode_binary(msg.encode()).payload == b"abc"


def test_mime_flag_mapping() -> None:
    assert proto.mime_to_flag("audio/mpeg") == proto.AudioFlag.MP3
    assert proto.mime_to_flag("audio/wav") == proto.AudioFlag.WAV
    assert proto.mime_to_flag("audio/l16") == proto.AudioFlag.PCM16LE_16KHZ_MONO
    assert proto.flag_to_mime(proto.AudioFlag.MP3) == "audio/mpeg"
