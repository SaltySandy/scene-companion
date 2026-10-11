"""Exit navigator: contract, generic heuristics, toggle, no fixture overfitting."""

from __future__ import annotations

import io
import os

import pytest

os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("SAW_NAV", "0")

from PIL import Image

from server.nav import (
    BEARING,
    DISTANCE,
    VISIBLE,
    analyze,
    default_navigator,
    get_navigator,
    nav_enabled,
    reset_navigator,
    set_nav_enabled,
)
from server.nav_exit import ExitNavigator


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _red_sign() -> bytes:
    img = Image.new("RGB", (640, 480), (230, 230, 228))
    # Illuminated EXIT rectangle, upper-right, typical pictogram aspect.
    for x in range(470, 560):
        for y in range(40, 72):
            img.putpixel((x, y), (210, 30, 30))
    return _jpeg(img)


def _blank_wall() -> bytes:
    return _jpeg(Image.new("RGB", (640, 480), (210, 210, 208)))


def _open_doorway() -> bytes:
    img = Image.new("RGB", (640, 480), (230, 230, 228))
    # Dark open doorway — must not be called an exit (no red sign).
    for x in range(260, 380):
        for y in range(80, 400):
            img.putpixel((x, y), (28, 26, 24))
    return _jpeg(img)


def test_analyze_keeps_the_navigator_evaluate_selected() -> None:
    reset_navigator()
    nav = default_navigator("baseline")
    assert nav.name == "baseline"
    assert analyze(_red_sign()).utterance  # baseline, not exit
    # Switching back is explicit.
    assert default_navigator("exit").name == "exit"


def test_exit_is_the_default_analyze() -> None:
    reset_navigator()
    nav = get_navigator()
    assert nav.name == "exit"
    result = analyze(_red_sign())
    assert result.exit.visible in VISIBLE
    assert result.exit.bearing in BEARING
    assert result.exit.distance in DISTANCE
    assert isinstance(result.should_stop, bool)
    assert isinstance(result.utterance, str)


def test_red_sign_is_an_exit_to_the_right() -> None:
    nav = ExitNavigator()
    result = nav.analyze(_red_sign())
    assert result.exit.visible in {"yes", "partial"}
    assert result.exit.bearing == "right"
    assert "exit" in result.utterance.lower()
    assert any(b.get("label") == "exit" for b in result.boxes)


def test_open_doorway_is_not_an_exit() -> None:
    nav = ExitNavigator()
    result = nav.analyze(_open_doorway())
    assert result.exit.visible == "no"
    assert "ahead" not in result.utterance.lower() or "not" in result.utterance.lower()
    assert "turn" in result.utterance.lower() or "scan" in result.utterance.lower()


def test_blank_wall_says_stop() -> None:
    nav = ExitNavigator()
    result = nav.analyze(_blank_wall())
    assert result.should_stop is True
    assert result.arrived is False
    assert "stop" in result.utterance.lower()
    assert result.exit.visible == "no"


def test_heading_avoids_a_center_blob() -> None:
    nav = ExitNavigator()
    img = Image.new("RGB", (640, 480), (230, 230, 228))
    for x in range(470, 560):
        for y in range(40, 72):
            img.putpixel((x, y), (210, 30, 30))
    # Big dark chair in the middle.
    for x in range(240, 400):
        for y in range(180, 430):
            img.putpixel((x, y), (18, 18, 18))
    result = nav.analyze(_jpeg(img))
    assert result.obstacle_ahead is True
    assert result.heading in {"left", "right"}
    assert result.heading != "center"


def test_remembers_exit_when_it_leaves_view() -> None:
    nav = ExitNavigator()
    nav.analyze(_red_sign())
    hidden = nav.analyze(_open_doorway())
    assert hidden.exit.visible == "no"
    assert hidden.exit.bearing == "behind"


def test_runtime_toggle() -> None:
    set_nav_enabled(True)
    assert nav_enabled() is True
    set_nav_enabled(False)
    assert nav_enabled() is False
    set_nav_enabled(None)
    os.environ["SAW_NAV"] = "0"
    assert nav_enabled() is False


@pytest.mark.asyncio
async def test_nav_http_toggle() -> None:
    import asyncio

    import httpx
    import uvicorn

    from server.app import app
    from server.tests.phone_helpers import free_port

    set_nav_enabled(None)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with httpx.AsyncClient() as client:
            off = await client.get(f"http://127.0.0.1:{port}/nav")
            assert off.status_code == 200
            assert off.json()["enabled"] is False
            on = await client.post(f"http://127.0.0.1:{port}/nav", json={"enabled": True})
            assert on.status_code == 200
            assert on.json()["enabled"] is True
            assert (await client.get(f"http://127.0.0.1:{port}/nav")).json()["enabled"] is True
            bad = await client.post(
                f"http://127.0.0.1:{port}/nav",
                content="{",
                headers={"content-type": "application/json"},
            )
            assert bad.status_code == 400
    finally:
        set_nav_enabled(None)
        server.should_exit = True
        await task


def test_analyze_is_fast_on_a_vga_frame() -> None:
    nav = ExitNavigator()
    jpeg = _red_sign()
    nav.analyze(jpeg)  # warmup
    result = nav.analyze(jpeg)
    assert result.latency_ms is not None
    assert result.latency_ms < 200
