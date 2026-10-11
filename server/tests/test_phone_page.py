"""The /phone route is the only server change testers need."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import uvicorn

from server.app import app


async def _wait_started(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("uvicorn did not start")


@pytest.mark.asyncio
async def test_phone_page_served() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=18780, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        await _wait_started(server)
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:18780/phone")
            css = await client.get("http://127.0.0.1:18780/static/phone/phone.css")
            js = await client.get("http://127.0.0.1:18780/static/phone/protocol.js")
        assert r.status_code == 200
        body = r.text
        assert "Scene Companion" in body
        assert 'id="startStop"' in body
        assert 'id="listen"' in body
        assert "On by default" in body
        assert "what's in front of me" in body
        assert 'aria-live="assertive"' in body
        assert "/static/phone/phone.js" in body
        assert css.status_code == 200
        assert js.status_code == 200
        assert "KIND_JPEG" in js.text
    finally:
        server.should_exit = True
        await task
