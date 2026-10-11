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


# --- Floor obstacles: analytic scenes from a pinhole camera over a flat floor ---

import math  # noqa: E402

from server.ai.proximity import fit_floor  # noqa: E402


def floor_scene(
    pitch_deg: float = 10.0,
    roll_deg: float = 0.0,
    height_m: float = 1.3,
    h: int = 350,
    w: int = 266,
    vfov_deg: float = 69.0,
    far_m: float = 8.0,
) -> np.ndarray:
    """Z-depth of a flat floor seen by a chest camera; rays above the floor hit a wall at far_m."""
    f = (h / 2) / math.tan(math.radians(vfov_deg) / 2)
    v, u = np.mgrid[0:h, 0:w].astype(np.float64)
    x, y = (u - w / 2) / f, (v - h / 2) / f
    r, p = math.radians(roll_deg), math.radians(pitch_deg)
    y_rolled = x * math.sin(r) + y * math.cos(r)
    down = y_rolled * math.cos(p) + math.sin(p)
    with np.errstate(divide="ignore"):
        z = np.where(down > 1e-6, height_m / down, far_m)
    return np.minimum(z, far_m).astype(np.float32)


@pytest.mark.parametrize("pitch,roll", [(0, 0), (10, 0), (25, 0), (10, 8), (15, -12)])
def test_bare_floor_is_clear_at_any_pitch_and_roll(pitch, roll) -> None:
    depth = floor_scene(pitch_deg=pitch, roll_deg=roll)
    p = analyze(depth, CFG)
    assert p.floor_fit is not None
    assert p.level == "clear", (p.sectors_m, p.floor_m)


def test_counting_the_floor_without_the_floor_model_would_cry_wolf() -> None:
    whole_frame = ProximityConfig(roi_bottom=1.0, floor=False)
    assert analyze(floor_scene(pitch_deg=25), whole_frame).level != "clear"


def test_low_chair_below_the_band_is_caught() -> None:
    depth = floor_scene(pitch_deg=10)
    depth[int(350 * 0.78) : int(350 * 0.94), 110:160] = 1.2  # seat and legs, 1.2 m ahead
    band_only = analyze(depth, ProximityConfig(floor=False))
    assert band_only.level == "clear"  # the old sensor misses it
    p = analyze(depth, CFG)
    assert p.levels["center"] == "near" and p.floor_m["center"] == pytest.approx(1.2, abs=0.05)
    assert p.levels["left"] == "clear" and p.levels["right"] == "clear"


def test_bottom_edge_artifact_is_ignored() -> None:
    depth = floor_scene(pitch_deg=10)
    depth[-10:, :] = 0.7  # the model bends in the last rows
    assert analyze(depth, CFG).level == "clear"


def test_big_obstacle_is_not_taken_for_the_floor() -> None:
    # Armchairs 1.3 m ahead fill the lower center and right, down to where they meet the
    # floor. Only the left 30% and the strip in front of them show floor.
    depth = floor_scene(pitch_deg=10)
    lower = slice(int(350 * 0.55), 350)
    region = depth[lower, 89:]  # left sector is columns 0-88
    depth[lower, 89:] = np.where(region > 1.3, 1.3, region)
    p = analyze(depth, CFG)
    assert p.floor_fit is not None
    assert p.levels["center"] != "clear" and p.levels["right"] != "clear"
    assert p.levels["left"] == "clear"  # the bare floor beside them is not an obstacle


def test_facing_a_wall_has_no_floor_and_the_band_still_works() -> None:
    depth = np.full((350, 266), 1.0, dtype=np.float32)
    assert fit_floor(depth, CFG) is None
    assert analyze(depth, CFG).level == "near"


def test_floor_settings_from_env() -> None:
    cfg = ProximityConfig.from_env({"SAW_FLOOR": "0", "SAW_FLOOR_MARGIN": "0.3", "SAW_FLOOR_TOP": "0.6"})
    assert (cfg.floor, cfg.floor_margin, cfg.floor_top) == (False, 0.3, 0.6)
    assert ProximityConfig.from_env({}).floor is True
