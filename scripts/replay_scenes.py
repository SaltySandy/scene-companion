#!/usr/bin/env python3
"""Replay the synthetic room scenes through the SAW pipeline and print a scorecard.

Windows PowerShell (repo root, venv already created by .\\run_server.ps1)::

    $env:SENTRY_DSN = ""
    $env:PYTHONPATH = "."
    python scripts/replay_scenes.py

Against a laptop server already listening on 8765 (or 8766 / 8767 /phone)::

    python scripts/replay_scenes.py --url http://127.0.0.1:8765

Sequence continuity (entry → chair → door)::

    python scripts/replay_scenes.py --sequence 01,02,04
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.scene_eval import main

if __name__ == "__main__":
    raise SystemExit(main())
