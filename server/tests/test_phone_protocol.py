"""Node unit tests for the browser protocol + cue policy."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PHONE = Path(__file__).resolve().parents[1] / "static" / "phone"


def test_js_protocol_and_cues() -> None:
    proc = subprocess.run(
        ["node", str(PHONE / "protocol_test.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok: protocol.js" in proc.stdout


def test_js_classifies_both_proximity_dialects() -> None:
    script = r"""
const proto = require("./protocol.js");
const extras = [
  {v:1, type:"proximity", state:"ok", level:"near", seq:1, frame_key:9},
  {v:1, type:"say", text:"what is it: a chair", urgent:true},
  {v:1, type:"flash", armed:true},
  {v:1, type:"latency", e2e_ms:40},
  {v:1, type:"audio_out", id:"a1"},
  {v:1, type:"transcript", text:"turn left", final:true},
];
const actions = extras.map((o) => proto.classifyInbound(o).action);
console.log(JSON.stringify(actions));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=PHONE,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    actions = json.loads(proc.stdout)
    assert actions == ["proximity", "say", "ignore", "ignore", "ignore", "transcript"]
