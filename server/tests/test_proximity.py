"""Deterministic proximity estimator + warning policy."""

from __future__ import annotations

from server.latency import LatencyBook
from server.proximity import (
    AF_CAL_APPROXIMATE,
    AF_CAL_CALIBRATED,
    AF_CAL_UNCALIBRATED,
    AF_STATE_FOCUSED_LOCKED,
    AF_STATE_PASSIVE_FOCUSED,
    AF_STATE_PASSIVE_SCAN,
    AutofocusEstimator,
    CorridorLoomingEstimator,
    FixedEstimator,
    ProximityEngine,
    fuse_estimates,
    mean_luminance,
)
from server.proximity_frames import (
    approach_sequence,
    focus_meta_for_distance,
    render_bright_plain,
    render_dark,
    render_empty,
    render_obstacle,
    render_wall,
    render_white_flash,
)
from server.tests.fake_client import MINI_JPEG


def test_covered_lens_is_low_light_not_an_obstacle() -> None:
    est = CorridorLoomingEstimator()
    jpeg = render_dark(value=6)
    got = est.estimate(jpeg)
    assert got is not None
    assert got.signal == "low_light"
    assert got.usable is False


def test_dark_frame_does_not_warn_or_alarm(clean_state) -> None:
    engine = ProximityEngine()
    dec = engine.process_jpeg(render_dark(value=6), capture_ts=100, recv_ms=100, now_ms=100, remaining_p95_ms=0)
    assert dec is not None
    assert dec.should_warn is False
    assert dec.reason == "low_light"
    assert engine.last_signal == "low_light"
    assert engine.in_alarm is False
    snap = engine.snapshot()
    assert snap["last_signal"] == "low_light"
    assert snap["last_distance_m"] is None


def test_featureless_dim_frame_is_no_signal() -> None:
    est = CorridorLoomingEstimator()
    got = est.estimate(render_dark(value=22))
    assert got is not None
    assert got.signal in {"low_light", "no_signal"}
    assert got.usable is False


def test_tiny_jpeg_is_not_an_obstacle() -> None:
    est = CorridorLoomingEstimator()
    assert est.estimate(MINI_JPEG) is None


def test_empty_scene_is_not_an_obstacle() -> None:
    est = CorridorLoomingEstimator()
    assert est.estimate(render_empty()) is None


def test_estimator_is_deterministic() -> None:
    jpeg = render_obstacle(1.0)
    est = CorridorLoomingEstimator()
    a = est.estimate(jpeg)
    b = est.estimate(jpeg)
    assert a is not None and b is not None
    assert a.distance_m == b.distance_m
    assert a.fill == b.fill


def test_closer_object_reads_nearer() -> None:
    est = CorridorLoomingEstimator()
    far = est.estimate(render_obstacle(2.2))
    mid = est.estimate(render_obstacle(1.8))
    near = est.estimate(render_obstacle(1.45))
    assert far and mid and near
    assert far.distance_m > mid.distance_m > near.distance_m


def test_recovered_distance_is_in_band() -> None:
    est = CorridorLoomingEstimator()
    for truth in (0.6, 1.0, 1.6):
        got = est.estimate(render_obstacle(truth, width=320, height=240))
        assert got is not None
        # Pinhole + JPEG + downscale: allow a generous band, but must not flip near/far.
        assert 0.25 * truth < got.distance_m < 2.4 * truth


def test_fill_frame_is_treated_as_close() -> None:
    est = CorridorLoomingEstimator()
    got = est.estimate(render_obstacle(0.12, width=160, height=120))
    assert got is not None
    assert got.distance_m <= 0.45


def test_nearest_of_two_objects() -> None:
    est = CorridorLoomingEstimator()
    # Near object in the corridor, far one off to the side.
    jpeg = render_obstacle(0.5, extras=[(2.2, 0.18)])
    got = est.estimate(jpeg)
    assert got is not None
    far_only = est.estimate(render_obstacle(2.2))
    assert far_only is not None
    assert got.distance_m < far_only.distance_m


def test_wall_gets_closer_as_it_fills() -> None:
    est = CorridorLoomingEstimator()
    far = est.estimate(render_wall(2.0))
    near = est.estimate(render_wall(0.35))
    assert far and near
    assert near.distance_m < far.distance_m


def test_predicted_crosses_before_measured(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(
        estimator=FixedEstimator(fn=lambda: distances[0]),
        latency=book,
        threshold_m=0.3,
        cooldown_ms=10_000,
    )
    # 1.4 m/s walk. remaining 400 ms + ~0 age => lead 0.4 s => 0.56 m of travel.
    # At 0.80 m measured, predicted = 0.80 - 1.4*0.4 = 0.24 <= 0.3 → warn.
    # At 1.20 m measured, predicted = 1.20 - 0.56 = 0.64 > 0.3 → no warn.
    distances = [1.20]
    t0 = 1_000_000
    d1 = engine.decide(engine.estimator.estimate(b""), capture_ts=t0, recv_ms=t0, now_ms=t0, remaining_p95_ms=400)
    assert d1.should_warn is False
    distances[0] = 0.80
    d2 = engine.decide(
        engine.estimator.estimate(b""),
        capture_ts=t0 + 286,
        recv_ms=t0 + 286,
        now_ms=t0 + 286,
        remaining_p95_ms=400,
    )
    assert d2.closing_mps > 1.0
    assert d2.predicted_m <= 0.3
    assert d2.should_warn is True
    assert d2.distance_m > 0.3  # warned before the frame itself was at one foot


def test_does_not_warn_late_when_lead_is_honored(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(estimator=FixedEstimator(1.0), latency=book, threshold_m=0.3)
    # closing 0, distance 1.0, remaining 200 → predicted 1.0
    dec = engine.decide(engine.estimator.estimate(b""), 1000, 1000, now_ms=1000, remaining_p95_ms=200)
    assert dec.should_warn is False
    assert dec.predicted_m == 1.0


def test_stale_frame_is_dropped(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(estimator=FixedEstimator(0.2), latency=book, stale_ms=800)
    dec = engine.decide(engine.estimator.estimate(b""), capture_ts=0, recv_ms=0, now_ms=2_000, remaining_p95_ms=200)
    assert dec.stale is True
    assert dec.should_warn is False
    assert engine.skipped_stale == 1


def test_cooldown_and_hysteresis_stop_flapping(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(
        estimator=FixedEstimator(0.2),
        latency=book,
        threshold_m=0.3,
        hysteresis_m=0.35,
        cooldown_ms=2_000,
    )
    t = 10_000
    first = engine.decide(engine.estimator.estimate(b""), t, t, now_ms=t, remaining_p95_ms=0)
    assert first.should_warn is True
    second = engine.decide(engine.estimator.estimate(b""), t + 80, t + 80, now_ms=t + 80, remaining_p95_ms=0)
    assert second.should_warn is False
    assert second.reason == "cooldown"


def test_rearm_after_object_recedes(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    dist = [0.2]
    engine = ProximityEngine(estimator=FixedEstimator(fn=lambda: dist[0]), latency=book, cooldown_ms=1)
    t = 20_000
    assert engine.decide(engine.estimator.estimate(b""), t, t, now_ms=t, remaining_p95_ms=0).should_warn
    dist[0] = 1.2
    far = engine.decide(engine.estimator.estimate(b""), t + 200, t + 200, now_ms=t + 200, remaining_p95_ms=0)
    assert far.should_warn is False
    assert engine.in_alarm is False
    dist[0] = 0.2
    again = engine.decide(engine.estimator.estimate(b""), t + 400, t + 400, now_ms=t + 400, remaining_p95_ms=0)
    assert again.should_warn is True


def test_quality_switch_gap_resets_closing_speed(clean_state) -> None:
    book = LatencyBook()
    engine = ProximityEngine(estimator=FixedEstimator(1.0), latency=book)
    engine._update_speed(1.0, 2.0)
    engine._update_speed(1.1, 1.8)  # ~2 m/s
    assert engine.closing_mps > 0.5
    engine._update_speed(1.1 + 0.8, 1.7)  # 800 ms gap, like a camera rebind
    assert engine.closing_mps == 0.0


def test_noisy_speed_is_smoothed(clean_state) -> None:
    book = LatencyBook()
    engine = ProximityEngine(estimator=FixedEstimator(1.0), latency=book)
    # True close at 1.4 m/s with one outlier jump.
    t = 10.0
    d = 3.0
    for i, step in enumerate((0.14, 0.14, 0.90, 0.14, 0.14, 0.14)):
        t += 0.1
        d -= step
        engine._update_speed(t, d)
    assert 0.6 < engine.closing_mps < 3.5


def test_fast_arrival_still_warns(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    dist = [2.5]
    engine = ProximityEngine(estimator=FixedEstimator(fn=lambda: dist[0]), latency=book)
    engine.decide(engine.estimator.estimate(b""), 0, 0, now_ms=0, remaining_p95_ms=200)
    dist[0] = 0.15  # object suddenly fills the frame
    dec = engine.decide(engine.estimator.estimate(b""), 120, 120, now_ms=120, remaining_p95_ms=200)
    assert dec.should_warn is True
    assert dec.predicted_m <= 0.3


def test_standing_still_inside_threshold_warns(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(estimator=FixedEstimator(0.22), latency=book)
    dec = engine.decide(engine.estimator.estimate(b""), 50, 50, now_ms=50, remaining_p95_ms=200)
    assert dec.closing_mps == 0.0
    assert dec.should_warn is True


def test_stationary_does_not_rewarn_after_cooldown(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    engine = ProximityEngine(
        estimator=FixedEstimator(0.22),
        latency=book,
        cooldown_ms=100,
    )
    first = engine.decide(engine.estimator.estimate(b""), 1_000, 1_000, now_ms=1_000, remaining_p95_ms=0)
    assert first.should_warn is True
    later = engine.decide(engine.estimator.estimate(b""), 1_200, 1_200, now_ms=1_200, remaining_p95_ms=0)
    assert later.should_warn is False
    assert later.reason == "stationary"
    assert engine.warnings == 1


def test_still_closing_rewarns_after_cooldown(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    dist = [0.8]
    engine = ProximityEngine(
        estimator=FixedEstimator(fn=lambda: dist[0]),
        latency=book,
        cooldown_ms=50,
        threshold_m=0.3,
    )
    t = 5_000
    engine.decide(engine.estimator.estimate(b""), t, t, now_ms=t, remaining_p95_ms=0)
    dist[0] = 0.2
    first = engine.decide(engine.estimator.estimate(b""), t + 200, t + 200, now_ms=t + 200, remaining_p95_ms=0)
    assert first.should_warn is True
    dist[0] = 0.12
    again = engine.decide(engine.estimator.estimate(b""), t + 400, t + 400, now_ms=t + 400, remaining_p95_ms=0)
    assert again.closing_mps > 0.15
    assert again.should_warn is True
    assert engine.warnings == 2


def test_disabled_engine_returns_none(clean_state) -> None:
    engine = ProximityEngine(estimator=FixedEstimator(0.1), enabled=False)
    assert engine.process_jpeg(render_obstacle(0.2), 1, 1) is None


def test_process_jpeg_records_cpu_time(clean_state) -> None:
    engine = ProximityEngine()
    engine.process_jpeg(render_obstacle(0.8, width=320, height=240), 1, 1, now_ms=1, remaining_p95_ms=200)
    assert engine.last_process_ms >= 0.0
    assert engine.processed == 1


def test_approach_sequence_warns_once_under_injected_latency(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)
    for _ in range(8):
        book.remaining.add(400)
    engine = ProximityEngine(latency=book, cooldown_ms=5_000)
    warned_at: list[float] = []
    t_ms = 1_000_000
    for frame in approach_sequence(start_m=2.0, end_m=0.15, speed_mps=1.4, fps=8):
        dec = engine.process_jpeg(
            frame.jpeg,
            capture_ts=t_ms,
            recv_ms=t_ms + 20,
            now_ms=t_ms + 30,
            remaining_p95_ms=400,
        )
        if dec and dec.should_warn:
            warned_at.append(frame.distance_m)
        t_ms += 125
    assert warned_at, "expected a warning before walking into the object"
    # Must not wait until the object is already at one foot in the frame.
    assert warned_at[0] > 0.3
    # And must not spam.
    assert len(warned_at) == 1


def test_flash_luma_split() -> None:
    assert mean_luminance(render_white_flash()) > 240
    assert mean_luminance(render_obstacle(1.5)) < 180


def test_pluggable_estimator(clean_state) -> None:
    engine = ProximityEngine(estimator=FixedEstimator(0.9))
    engine.set_estimator(FixedEstimator(0.15))
    dec = engine.process_jpeg(b"ignored", 10, 10, now_ms=10, remaining_p95_ms=0)
    assert dec is not None and dec.should_warn is True


def test_af_estimator_converts_diopters() -> None:
    est = AutofocusEstimator()
    got = est.estimate(3.33, af_state=AF_STATE_PASSIVE_FOCUSED, calibration=AF_CAL_APPROXIMATE)
    assert got is not None and got.usable
    assert abs(got.distance_m - 1.0 / 3.33) < 0.01


def test_af_calibrated_is_trusted() -> None:
    est = AutofocusEstimator()
    got = est.estimate(5.0, af_state=AF_STATE_FOCUSED_LOCKED, calibration=AF_CAL_CALIBRATED)
    assert got is not None and got.usable
    assert abs(got.distance_m - 0.2) < 0.001
    assert got.confidence >= 0.8


def test_af_hunting_is_not_usable() -> None:
    est = AutofocusEstimator()
    got = est.estimate(4.0, af_state=AF_STATE_PASSIVE_SCAN, calibration=AF_CAL_APPROXIMATE)
    assert got is not None
    assert got.usable is False
    assert got.signal == "af_hunting"


def test_af_uncalibrated_is_not_usable() -> None:
    est = AutofocusEstimator()
    got = est.estimate(4.0, af_state=AF_STATE_PASSIVE_FOCUSED, calibration=AF_CAL_UNCALIBRATED)
    assert got is not None
    assert got.usable is False
    assert got.signal == "af_uncalibrated"


def test_af_zero_diopters_is_infinity() -> None:
    est = AutofocusEstimator()
    got = est.estimate(0.0, af_state=AF_STATE_PASSIVE_FOCUSED, calibration=AF_CAL_CALIBRATED)
    assert got is not None and got.usable
    assert got.distance_m >= 8.0


def test_fuse_takes_nearer_usable() -> None:
    image = CorridorLoomingEstimator().estimate(render_obstacle(1.6))
    af = AutofocusEstimator().estimate(1.0 / 0.4, AF_STATE_PASSIVE_FOCUSED, AF_CAL_APPROXIMATE)
    assert image and image.usable and af and af.usable
    fused = fuse_estimates(image, af)
    assert fused is not None and fused.usable
    assert fused.distance_m == min(image.distance_m, af.distance_m)
    assert fused.details.get("fused_from") in {"image", "af"}


def test_fuse_falls_back_when_af_hunting() -> None:
    image = CorridorLoomingEstimator().estimate(render_obstacle(0.9))
    af = AutofocusEstimator().estimate(5.0, AF_STATE_PASSIVE_SCAN, AF_CAL_APPROXIMATE)
    fused = fuse_estimates(image, af)
    assert image and fused
    assert fused.usable
    assert fused.distance_m == image.distance_m
    assert fused.details.get("fused_from") == "image"


def test_fuse_falls_back_when_uncalibrated() -> None:
    image = CorridorLoomingEstimator().estimate(render_obstacle(1.1))
    af = AutofocusEstimator().estimate(5.0, AF_STATE_PASSIVE_FOCUSED, AF_CAL_UNCALIBRATED)
    fused = fuse_estimates(image, af)
    assert image and fused and fused.distance_m == image.distance_m


def test_low_light_beats_stale_af_lock() -> None:
    image = CorridorLoomingEstimator().estimate(render_dark(value=6))
    af = AutofocusEstimator().estimate(1.0 / 0.25, AF_STATE_FOCUSED_LOCKED, AF_CAL_CALIBRATED)
    fused = fuse_estimates(image, af)
    assert image and fused
    assert fused.signal == "low_light"
    assert fused.usable is False


def test_bright_plain_wall_is_likely_near_surface() -> None:
    est = CorridorLoomingEstimator()
    got = est.estimate(render_bright_plain(value=200))
    assert got is not None
    assert got.usable is True
    assert got.signal == "ok"
    assert got.details.get("reason") == "plain_wall"
    assert 0.3 < got.distance_m < 0.8
    assert got.confidence < 0.5


def test_bright_plain_wall_is_not_no_signal() -> None:
    est = CorridorLoomingEstimator()
    got = est.estimate(render_bright_plain(value=230))
    assert got is not None
    assert got.signal != "no_signal"
    assert got.signal != "low_light"


def test_empty_corridor_is_still_not_a_plain_wall() -> None:
    est = CorridorLoomingEstimator()
    assert est.estimate(render_empty()) is None


def test_speed_outlier_3_7_is_rejected(clean_state) -> None:
    engine = ProximityEngine(estimator=FixedEstimator(1.0))
    t = 10.0
    d = 2.0
    for _ in range(5):
        t += 0.1
        d -= 0.14  # 1.4 m/s walk
        engine._update_speed(t, d)
    assert 0.8 < engine.closing_mps < 1.9
    t += 0.1
    engine._update_speed(t, d - 0.37)  # 3.7 m/s spike
    assert engine.closing_mps < 2.0
    assert engine.closing_mps < 2.5


def test_engine_fuses_af_on_bright_wall(clean_state) -> None:
    engine = ProximityEngine()
    jpeg = render_bright_plain()
    focus = focus_meta_for_distance(0.45)
    dec = engine.process_jpeg(jpeg, 1_000, 1_000, now_ms=1_000, remaining_p95_ms=0, focus=focus)
    assert engine.last_af_estimate is not None and engine.last_af_estimate.usable
    assert abs(engine.last_af_estimate.distance_m - 0.45) < 0.02
    assert engine.last_estimate is not None and engine.last_estimate.usable
    # nearer of image (~0.55) and AF (0.45)
    assert engine.last_estimate.distance_m <= 0.55
    assert dec is not None
    snap = engine.snapshot()
    assert snap["last_af_m"] is not None
    assert snap["last_fused_m"] is not None


def test_bright_wall_approach_warns_before_one_foot(clean_state) -> None:
    engine = ProximityEngine(cooldown_ms=5_000)
    warned_at: list[float] = []
    t_ms = 2_000_000
    seq = approach_sequence(start_m=0.6, end_m=0.2, speed_mps=0.45, fps=8, kind="bright_wall")
    for frame in seq:
        dec = engine.process_jpeg(
            frame.jpeg,
            capture_ts=t_ms,
            recv_ms=t_ms + 15,
            now_ms=t_ms + 20,
            remaining_p95_ms=200,
            focus=focus_meta_for_distance(frame.distance_m),
        )
        if dec and dec.should_warn:
            warned_at.append(frame.distance_m)
        t_ms += 125
    assert warned_at, "expected a warning on a slow bright-wall approach"
    assert warned_at[0] > 0.3


def test_trace_row_has_expected_keys(clean_state) -> None:
    engine = ProximityEngine()
    dec = engine.process_jpeg(
        render_bright_plain(),
        50,
        50,
        now_ms=50,
        remaining_p95_ms=0,
        focus=focus_meta_for_distance(0.5),
    )
    row = engine.trace_row(capture_ts=50, recv_ms=50, brightness=200.0, decision=dec)
    for key in (
        "ts",
        "brightness",
        "signal",
        "image_m",
        "af_diopters",
        "af_m",
        "af_state",
        "af_calibration",
        "fused_m",
        "closing_mps",
        "predicted_m",
        "lead_ms",
        "decision",
        "reason",
    ):
        assert key in row
