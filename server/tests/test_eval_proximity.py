"""eval_proximity scoring and sweep on synthetic depth maps."""

from __future__ import annotations

import numpy as np

from scripts.eval_proximity import score, sweep
from server.ai.proximity import ProximityConfig

TRUTH = ProximityConfig()  # near < 1.6, danger < 0.8


def depth_map(center: float, far: float = 5.0) -> np.ndarray:
    d = np.full((100, 90), far, dtype=np.float32)
    d[20:70, 30:60] = center
    return d


def test_false_clear_is_counted_and_weighted() -> None:
    truth = [{"left": 5.0, "center": 1.0, "right": 5.0}]
    pred = [{"left": 5.0, "center": 2.0, "right": 5.0}]  # model says clear: a miss
    s = score(pred, truth, TRUTH, TRUTH)
    assert s["false_clear_rate"] == 1.0 and s["false_alarm_rate"] == 0.0
    assert s["cost"] == 5.0


def test_sweep_finds_a_threshold_that_undoes_model_bias() -> None:
    # The "model" reads every obstacle 0.5 m too far; truth distances vary.
    true_centers = [0.6, 1.0, 1.4, 2.5, 3.5, 4.5]
    depths = [depth_map(c + 0.5) for c in true_centers]
    truth = [{"left": 5.0, "center": c, "right": 5.0} for c in true_centers]
    biased = score([{"left": 5.0, "center": c + 0.5, "right": 5.0} for c in true_centers], truth, TRUTH, TRUTH)
    assert biased["false_clear_rate"] > 0
    best = sweep(depths, truth, TRUTH)[0]
    assert best["false_clear_rate"] == 0.0 and best["false_alarm_rate"] == 0.0
    assert best["config"]["near_m"] > TRUTH.near_m  # compensates by warning earlier


def test_ties_prefer_the_cautious_config() -> None:
    depths = [depth_map(4.0)]
    truth = [{"left": 5.0, "center": 4.0, "right": 5.0}]  # nothing near: every config ties
    best = sweep(depths, truth, TRUTH)[0]["config"]
    assert (best["near_m"], best["danger_m"], best["percentile"]) == (2.6, 1.2, 5.0)
