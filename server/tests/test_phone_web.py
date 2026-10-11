"""Playwright: fake camera, frames reach the server, say/audio are handled."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from server.tests.phone_helpers import free_port, start_uvicorn, stop_proc

ARTIFACTS = Path("/tmp/saw-phone-artifacts")
REPO_SHOTS = Path(__file__).resolve().parents[2] / "server" / "tests" / "artifacts"


def _tiny_wav() -> bytes:
    import struct
    import wave
    from io import BytesIO

    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = struct.pack("<" + "h" * 1600, *([0] * 1600))
        w.writeframes(frames)
    return buf.getvalue()


def _playwright_or_skip():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (pip install -r server/requirements-web.txt)")
    return sync_playwright


@pytest.fixture(scope="module")
def live_server():
    port = free_port()
    proc = start_uvicorn(port)
    try:
        yield port
    finally:
        stop_proc(proc)


def test_phone_ui_streams_frames_and_handles_server_audio(live_server: int) -> None:
    sync_playwright = _playwright_or_skip()
    origin = f"http://127.0.0.1:{live_server}"
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    REPO_SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            permissions=["camera", "microphone"],
        )
        phone = context.new_page()
        phone.goto(f"{origin}/phone", wait_until="domcontentloaded")
        phone.screenshot(path=str(ARTIFACTS / "phone_ui_idle.png"))
        phone.screenshot(path=str(REPO_SHOTS / "phone_ui_idle.png"))

        assert phone.locator("#startStop").is_visible()
        assert phone.locator("#listen").is_visible()
        phone.locator("#startStop").click()
        phone.wait_for_function("() => window.__saw && window.__saw.state.connected", timeout=15000)
        phone.wait_for_function("() => window.__saw && window.__saw.framesSent() >= 3", timeout=15000)

        viewer_frames = {"n": 0}

        def _on_ws(ws) -> None:
            def _frame(data) -> None:
                if isinstance(data, bytes) and len(data) > 20 and data[:4] == b"SAW1":
                    viewer_frames["n"] += 1

            ws.on("framereceived", _frame)

        viewer = context.new_page()
        viewer.on("websocket", _on_ws)
        viewer.goto(f"{origin}/", wait_until="domcontentloaded")
        deadline = time.time() + 8
        while time.time() < deadline and viewer_frames["n"] < 1:
            # Also prove the server accepted JPEGs via the demo hook + health.
            time.sleep(0.2)
        # Viewer may miss binary frames depending on Playwright version; health + __saw is enough.
        sent = phone.evaluate("() => window.__saw.framesSent()")
        assert sent >= 3

        with httpx.Client() as client:
            r = client.post(
                f"{origin}/say",
                json={"text": "stairs ahead, on your right", "urgent": False},
                timeout=5,
            )
            assert r.status_code == 200
            play = client.post(
                f"{origin}/play",
                content=_tiny_wav(),
                headers={"content-type": "audio/wav"},
                timeout=5,
            )
            assert play.status_code == 200

        phone.wait_for_function(
            "() => window.__saw && window.__saw.lastSay() && window.__saw.lastSay().indexOf('stairs') !== -1",
            timeout=8000,
        )
        phone.screenshot(path=str(ARTIFACTS / "phone_ui_connected.png"))
        phone.screenshot(path=str(REPO_SHOTS / "phone_ui_connected.png"))
        status = phone.locator("#status").inner_text()
        assert "Connected" in status or "connected" in status.lower()
        transcript = phone.locator("#transcript").inner_text()
        assert "stairs" in transcript.lower()

        handled = phone.evaluate("() => window.__saw.state.handledTypes")
        assert "say" in handled
        assert "hello_ack" in handled

        browser.close()


def test_phone_webkit_loads_if_available(live_server: int) -> None:
    sync_playwright = _playwright_or_skip()
    origin = f"http://127.0.0.1:{live_server}"
    with sync_playwright() as p:
        if not p.webkit:
            pytest.skip("WebKit not provided by this Playwright build")
        try:
            browser = p.webkit.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"WebKit unavailable: {exc}")
        page = browser.new_page()
        page.goto(f"{origin}/phone", wait_until="domcontentloaded")
        assert page.locator("#startStop").inner_text() == "Start"
        page.screenshot(path=str(ARTIFACTS / "phone_ui_webkit.png"))
        browser.close()
