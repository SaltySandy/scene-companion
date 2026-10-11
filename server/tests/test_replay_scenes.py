"""Scene fixtures, navigation interface, and replay scorecard."""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("SAW_NAV", "0")

from server.nav import (  # noqa: E402
    BEARING,
    DISTANCE,
    VISIBLE,
    BaselineNavigator,
    ExitHint,
    NavResult,
    analyze,
    get_navigator,
    reset_navigator,
)
from server.scene_eval import (  # noqa: E402
    FIXTURES,
    evaluate,
    load_scenes,
    parse_sequence,
    render_table,
    score_nav,
    score_utterance,
    stream_url,
)

SCENES = load_scenes()


def test_manifest_is_synthetic_and_scoreable() -> None:
    data = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert "synthetic" in data["note"].lower() or "AI-generated" in data["note"]
    assert "AI-generated" in data["note"]
    assert len(data["scenes"]) == 8
    assert data["sequence_default"] == [
        "01_entry_overview",
        "02_chair_blocking",
        "04_arrived_at_exit",
    ]
    for scene in SCENES:
        assert scene.exit_visible in VISIBLE
        assert scene.exit_bearing in BEARING
        assert scene.exit_distance in DISTANCE
        assert scene.nearest_obstacle_distance in DISTANCE
        assert isinstance(scene.obstacle_ahead, bool)
        assert isinstance(scene.should_stop, bool)
        assert isinstance(scene.arrived, bool)
        assert scene.utterance
        assert scene.jpeg_path.is_file()
        jpeg = scene.jpeg_bytes()
        assert jpeg[:2] == b"\xff\xd8"
        # Phone stream size is 640x480; keep the original next to it.
        assert (FIXTURES / "original" / scene.file).is_file()


def test_analyze_contract_and_baseline_enums() -> None:
    reset_navigator()
    nav = get_navigator("baseline")
    assert nav.name == "baseline"
    scene = next(s for s in SCENES if s.id.startswith("01"))
    result = nav.analyze(scene.jpeg_bytes())
    assert isinstance(result, NavResult)
    payload = result.payload()
    assert set(payload["exit"]) >= {"visible", "bearing", "distance"}
    assert payload["exit"]["visible"] in VISIBLE
    assert payload["exit"]["bearing"] in BEARING
    assert payload["exit"]["distance"] in DISTANCE
    assert isinstance(payload["should_stop"], bool)
    assert isinstance(payload["arrived"], bool)
    assert isinstance(payload["utterance"], str)
    assert isinstance(payload["obstacles"], list)
    # Module-level entry point used by the hook and the harness.
    again = analyze(scene.jpeg_bytes())
    assert again.exit.visible in VISIBLE


def test_baseline_hits_easy_synthetic_cases() -> None:
    """Red EXIT / blank wall are what the color stub can see."""
    reset_navigator()
    nav = BaselineNavigator()
    by_id = {s.id: s for s in SCENES}
    wall = nav.analyze(by_id["06_plain_wall_close"].jpeg_bytes())
    assert wall.should_stop is True
    assert wall.exit.visible == "no"
    assert "stop" in wall.utterance.lower()
    reset_navigator()
    nav = BaselineNavigator()
    hidden = nav.analyze(by_id["08_exit_not_visible"].jpeg_bytes())
    assert hidden.exit.visible == "no"
    assert "exit" in hidden.utterance.lower()


def test_utterance_keywords_are_deterministic() -> None:
    stop = next(s for s in SCENES if s.id.startswith("02"))
    assert score_utterance("Stop. Chair directly ahead. Step right.", stop)["score"] == 1.0
    assert score_utterance("Keep walking, all clear.", stop)["score"] == 0.0
    arrived = next(s for s in SCENES if s.id.startswith("04"))
    assert score_utterance("You have reached the exit door. Handle on the right.", arrived)["score"] == 1.0
    hidden = next(s for s in SCENES if s.id.startswith("08"))
    bad = score_utterance("Exit ahead through the doorway.", hidden)
    assert bad["doorway_trap"] is True
    good = score_utterance(
        "Chair ahead on your left. Exit is not in view. Turn and scan for the exit sign.",
        hidden,
    )
    assert good["score"] == 1.0
    assert good["doorway_trap"] is False


def test_score_nav_perfect_match() -> None:
    gt = next(s for s in SCENES if s.id.startswith("06"))
    pred = NavResult(
        exit=ExitHint("no", "none", "none"),
        should_stop=True,
        arrived=False,
        obstacle_ahead=True,
        nearest_obstacle_distance="close",
        utterance="Stop. Wall directly ahead.",
    )
    graded = score_nav(pred, gt)
    assert graded["score"] == 1.0
    assert all(v == 1.0 for v in graded["fields"].values())


def test_sequence_parser() -> None:
    seq = parse_sequence("01,02,04", SCENES)
    assert [s.id for s in seq] == [
        "01_entry_overview",
        "02_chair_blocking",
        "04_arrived_at_exit",
    ]
    assert [s.id for s in parse_sequence("default", SCENES)] == [s.id for s in seq]


def test_stream_url_accepts_http_and_phone_ports() -> None:
    assert stream_url("http://127.0.0.1:8765") == "ws://127.0.0.1:8765/v1/stream"
    assert stream_url("https://laptop.tailnet.ts.net") == "wss://laptop.tailnet.ts.net/v1/stream"
    assert stream_url("ws://127.0.0.1:8767/v1/stream") == "ws://127.0.0.1:8767/v1/stream"
    assert stream_url("http://127.0.0.1:8766/phone") == "ws://127.0.0.1:8766/v1/stream"


@pytest.mark.asyncio
async def test_scorecard_inprocess() -> None:
    reset_navigator()
    card = await evaluate(mode="inprocess", navigator="baseline", sequence_spec="01,02,04")
    assert card["n_scenes"] == 8
    assert card["navigator"] == "baseline"
    assert 0.0 <= card["overall"] <= 1.0
    table = render_table(card)
    assert "01_entry_overview" in table
    assert "Overall nav" in table
    assert "02_chair_blocking" in table
    seq = card["sequence"]
    assert seq["ids"] == ["01_entry_overview", "02_chair_blocking", "04_arrived_at_exit"]
    # Baseline is weak; the harness must still produce a number for every field.
    for row in card["scenes"]:
        assert set(row["fields"]) >= set(
            (
                "exit_visible",
                "exit_bearing",
                "exit_distance",
                "obstacle_ahead",
                "nearest_obstacle_distance",
                "should_stop",
                "arrived",
                "utterance",
            )
        )


@pytest.mark.asyncio
async def test_scorecard_websocket_fake_phone() -> None:
    reset_navigator()
    os.environ["SAW_NAV"] = "1"
    os.environ["SAW_DEMO_ANNOUNCE_SEC"] = "0"
    try:
        card = await evaluate(mode="websocket", navigator="baseline", sequence_spec="01,02,04", wait_sec=0.45)
    finally:
        os.environ["SAW_NAV"] = "0"
    assert card["mode"] == "websocket"
    assert card["n_scenes"] == 8
    assert "Overall nav" in render_table(card)
    # Live path spoke something for at least the scenes the baseline utters.
    spoken = [row.get("spoken") for row in card["scenes"]]
    assert any(spoken)
