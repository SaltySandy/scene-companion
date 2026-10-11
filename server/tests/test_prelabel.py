"""prelabel.py against a fake Claude client: request shape, output, resume, refusals."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import prelabel
from server.ai import vlm


class FakeMessages:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if answer == "refusal":
            return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="bio"), content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))])


def fake_client(answers):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(answers)))


def answer(center: float) -> dict:
    return {
        "left": {"nearest_m": 9.0, "obstacle": "none"},
        "center": {"nearest_m": center, "obstacle": "door"},
        "right": {"nearest_m": 2.0, "obstacle": "wall"},
    }


def make_session(tmp_path: Path, keys: list[int]) -> Path:
    (tmp_path / "frames").mkdir()
    lines = []
    for k in keys:
        (tmp_path / "frames" / f"{k}.jpg").write_bytes(b"\xff\xd8jpeg")
        lines.append(json.dumps({"frame_key": k, "capture_ts": k}))
    (tmp_path / "frames.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


def test_request_shape() -> None:
    req = vlm.build_request(b"\xff\xd8", "prompt", prelabel.SCHEMA, effort="high")
    assert req["model"] == "claude-opus-5-5"
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["schema"]["required"] == ["left", "center", "right"]
    image, text = req["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/jpeg" and text["type"] == "text"


def test_labels_written_clamped_and_resumable(tmp_path: Path) -> None:
    session = make_session(tmp_path, [100, 200, 300])
    client = fake_client([answer(1.2), answer(0.5)])
    assert prelabel.label_session(session, client, every=1, limit=2, workers=1) == 2
    labels = [json.loads(line) for line in (session / "labels.jsonl").read_text().splitlines()]
    assert [lab["frame_key"] for lab in labels] == [100, 200]
    assert labels[0]["left"]["nearest_m"] == 5.0  # clamped to MAX_M
    assert labels[1]["center"] == {"nearest_m": 0.5, "obstacle": "door"}
    # Resume: only frame 300 is left; a refusal writes nothing.
    assert prelabel.pick_frames(session, every=1, limit=None) == [300]
    assert prelabel.label_session(session, fake_client(["refusal"]), every=1, workers=1) == 0
    assert prelabel.pick_frames(session, every=1, limit=None) == [300]


def test_every_nth_frame(tmp_path: Path) -> None:
    session = make_session(tmp_path, list(range(10)))
    assert prelabel.pick_frames(session, every=5, limit=None) == [0, 5]
