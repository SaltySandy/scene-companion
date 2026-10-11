"""FoV snapshot uses the live ProximityConfig and model size, not hard-coded copies."""

from __future__ import annotations

import io

import numpy as np

import server.ai_hook as ai_hook
from server.ai.fov import render_fov, sector_edges
from server.ai.proximity import ProximityConfig, analyze


def test_render_fov_uses_the_cfg_it_is_given() -> None:
    cfg = ProximityConfig(roi_top=0.22, roi_bottom=0.61, percentile=15, near_m=2.2, danger_m=0.9)
    depth = np.full((40, 30), 4.0, dtype=np.float32)
    depth[10:24, 10:20] = 1.1
    result = analyze(depth, cfg)
    jpeg, meta = render_fov(depth, result, cfg, model_name="unit-depth")
    assert jpeg[:2] == b"\xff\xd8"
    assert (meta["roi_top"], meta["roi_bottom"]) == (0.22, 0.61)
    assert (meta["near_m"], meta["danger_m"], meta["percentile"]) == (2.2, 0.9, 15)
    assert (meta["depth_height"], meta["depth_width"]) == (40, 30)
    assert meta["model"] == "unit-depth"
    assert meta["mount_rotation"] == "phone"
    assert meta["sector_edges"] == sector_edges(30)
    assert set(meta["sectors_m"]) == {"left", "center", "right"}
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg))
    assert img.size[0] >= 30 and img.size[1] >= 40


def test_fov_params_read_runtime_cfg_and_stashed_depth(monkeypatch) -> None:
    cfg = ProximityConfig(roi_top=0.31, roi_bottom=0.58, near_m=1.1, danger_m=0.4)
    depth = np.full((12, 9), 3.0, dtype=np.float32)
    result = analyze(depth, cfg)
    monkeypatch.setattr(ai_hook, "_cfg", cfg)
    monkeypatch.setattr(ai_hook, "_model", type("M", (), {"name": "stashed", "height": 350, "width": 266})())
    monkeypatch.setattr(ai_hook, "_debug", (depth, result, cfg, "stashed"))
    params = ai_hook.fov_params()
    assert params["roi_top"] == 0.31
    assert params["roi_bottom"] == 0.58
    assert params["near_m"] == 1.1
    assert (params["depth_height"], params["depth_width"]) == (12, 9)
    assert params["model"] == "stashed"
    assert params["sectors_m"] == result.sectors_m


def test_render_fov_jpeg_none_until_a_frame(monkeypatch) -> None:
    monkeypatch.setattr(ai_hook, "_debug", None)
    assert ai_hook.render_fov_jpeg() is None
