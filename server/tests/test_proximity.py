"""Sector distances and levels from synthetic depth maps, plus the real model if exported."""

from __future__ import annotations

import io

import numpy as np
import pytest

from server.ai.proximity import ProximityConfig, analyze, overall, sector_distances

H, W = 100, 90  # rows, cols; sectors are 30 columns each
CFG = ProximityConfig()


def scene(far: float = 5.0) -> np.ndarray:
    return np.full((H, W), far, dtype=np.float32)


def test_open_space_is_clear() -> None:
    p = analyze(scene(), CFG)
    assert p.sectors_m == {"left": 5.0, "center": 5.0, "right": 5.0}
    assert p.level == "clear"


def test_floor_below_the_band_is_ignored() -> None:
    depth = scene()
    depth[80:, :] = 0.9  # near floor at the feet
    assert analyze(depth, CFG).level == "clear"


def test_obstacle_in_center_band() -> None:
    depth = scene()
    depth[20:70, 35:55] = 1.2  # door frame / person ahead
    p = analyze(depth, CFG)
    assert p.levels == {"left": "clear", "center": "near", "right": "clear"}
    assert p.level == "near"


def test_small_obstacle_still_counts_but_noise_does_not() -> None:
    depth = scene()
    depth[15:75, 40:45] = 0.6  # a pole: ~17% of the center sector
    assert analyze(depth, CFG).levels["center"] == "danger"
    noisy = scene()
    noisy[30, 40] = 0.1  # single bad pixel
    assert analyze(noisy, CFG).levels["center"] == "clear"


def test_side_near_is_a_corridor_but_side_danger_counts() -> None:
    assert overall({"left": "near", "center": "clear", "right": "near"}) == "clear"
    assert overall({"left": "danger", "center": "clear", "right": "clear"}) == "danger"


def test_all_invalid_sector_raises() -> None:
    depth = scene()
    depth[:, :30] = np.nan
    with pytest.raises(ValueError):
        sector_distances(depth, CFG)


def test_config_from_env() -> None:
    cfg = ProximityConfig.from_env({"SAW_NEAR_M": "2.0", "SAW_DANGER_M": ""})
    assert (cfg.near_m, cfg.danger_m) == (2.0, 0.8)


def test_real_model_if_exported() -> None:
    from server.ai import depth

    model = depth.load()
    if model is None:
        pytest.skip("depth model not exported")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (480, 640), (120, 110, 100)).save(buf, format="JPEG")
    out = model.predict(buf.getvalue())
    assert out.shape == (model.height, model.width)
    assert np.isfinite(out).all() and (out > 0).all()
    analyze(out)  # does not raise
