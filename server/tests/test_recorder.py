"""FrameRecord schema, JSONL recorder, and per-frame lineage from the hub."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import server.hub as hub_module
from server.ai_hook import Say
from server.frame_record import FrameRecord
from server.hub import Hub, PendingFrame
from server.recorder import Recorder

SPEC_KEYS = [
    "session_id",
    "frame_key",
    "capture_ts",
    "recv_ts",
    "infer_start",
    "infer_end",
    "model",
    "sectors_m",
    "level",
    "decision",
    "message",
    "dropped_since_last",
]


def make_record(capture_ts: int = 1000, **overrides) -> FrameRecord:
    values = dict(
        session_id="s1",
        frame_key=capture_ts,
        capture_ts=capture_ts,
        recv_ts=capture_ts + 40,
        infer_start=capture_ts + 45,
        infer_end=capture_ts + 90,
        model=None,
        sectors_m=None,
        level=None,
        decision="none",
        message=None,
        dropped_since_last=0,
    )
    values.update(overrides)
    return FrameRecord(**values)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- FrameRecord -----------------------------------------------------------


def test_json_keys_match_the_spec_in_order() -> None:
    line = make_record().to_json()
    assert "\n" not in line
    assert list(json.loads(line)) == SPEC_KEYS


def test_round_trip_ignores_unknown_keys() -> None:
    rec = make_record(sectors_m={"left": 2.5, "center": 0.8, "right": 3.0}, level="near")
    data = json.loads(rec.to_json())
    data["added_in_a_later_commit"] = 1
    assert FrameRecord.from_dict(data) == rec


# --- Recorder --------------------------------------------------------------


def test_disabled_recorder_writes_nothing(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, "s1", enabled=False)
    rec.start()
    rec.record(make_record(), b"jpeg")
    rec.stop()
    assert list(tmp_path.iterdir()) == []


def test_from_env_is_off_unless_saw_record(tmp_path: Path) -> None:
    assert not Recorder.from_env({"SAW_SESSIONS_DIR": str(tmp_path)}).enabled
    on = Recorder.from_env({"SAW_RECORD": "1", "SAW_SESSIONS_DIR": str(tmp_path), "SAW_SESSION_ID": "walk1"})
    assert on.enabled and on.save_jpeg
    assert on.path == tmp_path / "walk1" / "frames.jsonl"
    off_jpeg = Recorder.from_env({"SAW_RECORD": "1", "SAW_RECORD_JPEG": "0"})
    assert not off_jpeg.save_jpeg


def test_stop_flushes_every_queued_record_and_jpeg(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, "s1")
    rec.start()
    for i in range(50):
        rec.record(make_record(1000 + i), f"jpeg{i}".encode())
    rec.stop()
    lines = read_lines(rec.path)
    assert [line["capture_ts"] for line in lines] == list(range(1000, 1050))
    assert (tmp_path / "s1" / "frames" / "1049.jpg").read_bytes() == b"jpeg49"
    assert rec.records_written == 50 and rec.records_dropped == 0


def test_jpegs_can_be_turned_off(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, "s1", save_jpeg=False)
    rec.start()
    rec.record(make_record(), b"jpeg")
    rec.stop()
    assert len(read_lines(rec.path)) == 1
    assert not (tmp_path / "s1" / "frames").exists()


def test_records_after_stop_are_ignored(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, "s1")
    rec.start()
    rec.stop()
    rec.record(make_record(), b"jpeg")
    assert read_lines(rec.path) == []


# --- Hub.process_frame -----------------------------------------------------


@pytest.fixture
def recording_hub(tmp_path: Path):
    hub = Hub(recorder=Recorder(tmp_path, "s1"))
    hub.recorder.start()
    yield hub
    hub.recorder.stop()


async def test_lineage_and_sent_decision(recording_hub: Hub, monkeypatch) -> None:
    async def speaks(jpeg: bytes, ts: int):
        return Say("wall ahead")

    monkeypatch.setattr(hub_module, "on_frame", speaks)
    frame = PendingFrame(b"jpeg", capture_ts=1000, recv_ts=1040, dropped_before=3)
    rec = await recording_hub.process_frame(frame)
    assert (rec.session_id, rec.frame_key, rec.capture_ts, rec.recv_ts) == ("s1", 1000, 1000, 1040)
    assert rec.infer_start <= rec.infer_end
    assert rec.dropped_since_last == 3
    assert (rec.decision, rec.message) == ("sent", "wall ahead")
    recording_hub.recorder.stop()
    assert FrameRecord.from_dict(read_lines(recording_hub.recorder.path)[0]) == rec


@pytest.mark.parametrize("response", [None, [], Say("   ")])
async def test_nothing_sendable_is_decision_none(recording_hub: Hub, monkeypatch, response) -> None:
    async def quiet(jpeg: bytes, ts: int):
        return response

    monkeypatch.setattr(hub_module, "on_frame", quiet)
    rec = await recording_hub.process_frame(PendingFrame(b"jpeg", 1000, 1040, 0))
    assert (rec.decision, rec.message) == ("none", None)


async def test_failing_hook_is_still_recorded(recording_hub: Hub, monkeypatch) -> None:
    async def broken(jpeg: bytes, ts: int):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(hub_module, "on_frame", broken)
    rec = await recording_hub.process_frame(PendingFrame(b"jpeg", 1000, 1040, 0))
    assert rec.decision == "none"
    recording_hub.recorder.stop()
    assert len(read_lines(recording_hub.recorder.path)) == 1
