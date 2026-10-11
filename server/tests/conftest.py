"""Shared fixtures. The hub / latency book / proximity engine are process-wide."""

from __future__ import annotations

import os

import pytest

# Disable the demo stub unless a test opts in. Must be set before workers run.
os.environ.setdefault("SAW_DEMO_ANNOUNCE_SEC", "0")


@pytest.fixture
def clean_state():
    from server.ai_hook import reset_demo_state
    from server.hub import hub
    from server.latency import book
    from server.audio_log import audio_log
    from server.proximity import engine
    from server.trace import tracer

    reset_demo_state()
    hub.reset_for_tests()
    book.reset()
    engine.reset()
    audio_log.reset()
    tracer.reset()
    yield
    reset_demo_state()
    hub.reset_for_tests()
    book.reset()
    engine.reset()
    audio_log.reset()
    tracer.reset()
