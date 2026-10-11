"""Live sensor path: hysteresis, the hook's readings, and the hub's heartbeat gate."""

from __future__ import annotations

import json

import numpy as np
import pytest

import server.ai_hook as ai_hook
import server.hub as hub_module
from server import protocol as proto
from server.ai.proximity import LevelTracker, ProximityConfig
from server.ai_hook import ProximityReading
from server.hub import Hub, PendingFrame


def test_tracker_escalates_at_once_and_calms_slowly() -> None:
    t = LevelTracker(hold=2)
    assert t.update({"center": "clear"}) == {"center": "clear"}
    assert t.update({"center": "danger"}) == {"center": "danger"}  # immediate
    assert t.update({"center": "clear"}) == {"center": "danger"}  # 1st calmer frame: held
    assert t.update({"center": "near"}) == {"center": "near"}  # 2nd calmer frame: step down
    assert t.update({"center": "clear"}) == {"center": "near"}
    assert t.update({"center": "danger"}) == {"center": "danger"}  # resets the count
    assert t.update({"center": "clear"}) == {"center": "danger"}


class FakeModel:
    name = "fake-depth"

    def __init__(self, center: float = 5.0, fail: bool = False) -> None:
        self.center, self.fail = center, fail

    def predict(self, jpeg: bytes) -> np.ndarray:
        if self.fail:
            raise RuntimeError("boom")
        d = np.full((100, 90), 5.0, dtype=np.float32)
        d[20:70, 30:60] = self.center
        return d


@pytest.fixture
def sensor(monkeypatch):
    def install(model):
        monkeypatch.setattr(ai_hook, "_model_tried", True)
        monkeypatch.setattr(ai_hook, "_model", model)
        monkeypatch.setattr(ai_hook, "_cfg", ProximityConfig())
        monkeypatch.setattr(ai_hook, "_tracker", LevelTracker())

    return install


async def test_reading_from_model(sensor) -> None:
    sensor(FakeModel(center=1.0))
    r = await ai_hook.read_proximity(b"jpeg")
    assert (r.state, r.level, r.model) == ("ok", "near", "fake-depth")
    assert r.sectors_m["center"] == 1.0


async def test_missing_or_failing_model_is_unknown(sensor) -> None:
    sensor(None)
    assert (await ai_hook.read_proximity(b"jpeg")).state == "unknown"
    sensor(FakeModel(fail=True))
    assert (await ai_hook.read_proximity(b"jpeg")).state == "unknown"


class FakePhone:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def hub_with(monkeypatch, reading) -> tuple[Hub, FakePhone]:
    async def hook(jpeg: bytes, ts: int):
        return [reading]

    monkeypatch.setattr(hub_module, "on_frame", hook)
    hub = Hub()
    hub.phone = FakePhone()
    return hub, hub.phone


OK = ProximityReading(state="ok", level="near", raw_level="near",
                      levels={"left": "clear", "center": "near", "right": "clear"},
                      sectors_m={"left": 3.0, "center": 1.2, "right": 3.0}, model="m")


async def test_fresh_frame_sends_level_first(monkeypatch) -> None:
    hub, phone = hub_with(monkeypatch, OK)
    now = proto.now_ms()
    rec = await hub.process_frame(PendingFrame(b"j", capture_ts=now, recv_ts=now, dropped_before=0))
    assert rec.decision == "sent" and rec.level == "near" and rec.model == "m"
    msg = phone.sent[0]
    assert (msg["type"], msg["state"], msg["level"], msg["frame_key"]) == ("proximity", "ok", "near", now)


async def test_stale_frame_sends_unknown(monkeypatch) -> None:
    hub, phone = hub_with(monkeypatch, OK)
    old = proto.now_ms() - 5_000
    rec = await hub.process_frame(PendingFrame(b"j", capture_ts=old, recv_ts=old, dropped_before=0))
    assert rec.decision == "stale"
    assert phone.sent[0]["state"] == "unknown" and phone.sent[0]["level"] is None


async def test_held_level_is_suppressed_and_unknown_is_none(monkeypatch) -> None:
    held = ProximityReading(state="ok", level="danger", raw_level="clear", levels={}, sectors_m={})
    hub, _ = hub_with(monkeypatch, held)
    now = proto.now_ms()
    assert (await hub.process_frame(PendingFrame(b"j", now, now, 0))).decision == "suppressed"
    hub, phone = hub_with(monkeypatch, ai_hook.UNKNOWN)
    assert (await hub.process_frame(PendingFrame(b"j", now, now, 0))).decision == "none"
    assert phone.sent[0]["state"] == "unknown"


async def test_synced_clock_sets_age(monkeypatch) -> None:
    hub, phone = hub_with(monkeypatch, OK)
    now = proto.now_ms()
    hub.clock.add(t0=now - 10, tp=now - 5 + 3000, t1=now)  # phone 3 s ahead
    await hub.process_frame(PendingFrame(b"j", capture_ts=now + 3000 - 200, recv_ts=now, dropped_before=0))
    assert 190 <= phone.sent[0]["age_ms"] <= 260
