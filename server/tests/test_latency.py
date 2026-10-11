"""Clock sync, rolling percentiles, flash detector, stage breakdown."""

from __future__ import annotations

from server.latency import LatencyBook, ntp_offset_rtt, percentile
from server.proximity import mean_luminance
from server.proximity_frames import render_obstacle, render_white_flash
from server.tests.fake_client import MINI_JPEG


def test_percentile_empty_and_single() -> None:
    assert percentile([], 95) is None
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 95) == 7.0


def test_percentile_linear_interpolation() -> None:
    xs = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert percentile(xs, 50) == 20.0
    assert percentile(xs, 0) == 0.0
    assert percentile(xs, 100) == 40.0


def test_ntp_recovers_skew_on_symmetric_path() -> None:
    # Phone is 400 ms ahead. 10 ms each way.
    t0, t1, t2, t3 = 1_400, 1_010, 1_012, 1_422
    offset, rtt = ntp_offset_rtt(t0, t1, t2, t3)
    assert abs(offset - (-400)) < 1e-6
    assert abs(rtt - 20) < 1e-6


def test_ntp_asymmetric_path_biases_offset() -> None:
    # 10 ms up, 90 ms down. True offset 0. Bias = (90-10)/2 = 40.
    offset, rtt = ntp_offset_rtt(0, 10, 12, 102)
    assert rtt == 100
    assert abs(offset - (-40)) < 1e-6


def test_min_rtt_sample_wins(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 80, 90, 200)  # rtt 110, messy
    book.consider_sync(1000, 1006, 1007, 1013)  # rtt 12, offset 0
    assert book.offset_rtt_ms == 12
    assert book.offset_ms is not None
    assert abs(book.offset_ms) < 1


def test_later_worse_rtt_does_not_replace(clean_state) -> None:
    book = LatencyBook()
    book.consider_sync(0, 5, 6, 12)
    first = book.offset_ms
    book.consider_sync(100, 200, 250, 500)
    assert book.offset_ms == first
    assert book.sync_samples == 2


def test_e2e_is_phone_clock_only() -> None:
    book = LatencyBook()
    book.consider_sync(0, 10_000, 10_001, 20)  # huge bogus offset if someone mixed clocks
    book.note_tts_start(capture_ts=1_000, tts_start_ts=1_240, say_recv_ts=1_180)
    assert book.e2e.summary()["n"] == 1
    assert book.e2e.summary()["p50"] == 240


def test_stage_breakdown_with_offset() -> None:
    book = LatencyBook()
    # laptop = phone + 50
    book.consider_sync(t0=1000, t1=1055, t2=1056, t3=1012)  # rtt 11, offset ~49.5
    capture = 2000
    recv = 2110  # uplink ~ 2110 - (2000+50) = 60
    book.note_frame(capture, recv)
    book.note_process(recv, start_ms=2115, end_ms=2130)
    book.note_say_sent(capture, say_sent_ms=2132, recv_ms=recv, process_start_ms=2115, process_end_ms=2130)
    book.note_tts_start(capture, tts_start_ts=2220, say_recv_ts=2180)
    snap = book.snapshot()
    assert snap["e2e_ms"]["p50"] == 220
    assert snap["stages_ms"]["uplink"]["n"] == 1
    assert snap["stages_ms"]["queue"]["p50"] == 5
    assert snap["stages_ms"]["process"]["p50"] == 15
    assert snap["stages_ms"]["tts"]["p50"] == 40


def test_remaining_p95_falls_back_to_default() -> None:
    book = LatencyBook()
    assert book.remaining_p95_ms() == 250.0
    book.e2e.add(400)
    book.e2e.add(420)
    book.e2e.add(380)
    assert abs(book.remaining_p95_ms() - 420) < 5 or book.remaining_p95_ms() >= 380


def test_lead_time_is_age_plus_remaining() -> None:
    book = LatencyBook()
    book.consider_sync(0, 0, 0, 0)  # offset 0
    book.remaining.add(200)
    book.remaining.add(200)
    lead = book.lead_time_ms(capture_ts=1000, now_ms=1150, recv_ms=1100)
    assert abs(lead - 350) < 1  # 150 age + 200 remaining


def test_frame_age_without_offset_uses_recv() -> None:
    book = LatencyBook()
    assert book.frame_age_ms(1, now_ms=500, recv_ms=400) == 100


def test_flash_detects_white_and_ignores_dark() -> None:
    book = LatencyBook()
    dark = mean_luminance(render_obstacle(2.0))
    white = mean_luminance(render_white_flash())
    assert dark is not None and dark < 180
    assert white is not None and white > 240
    book.arm_flash(1_000)
    assert book.note_brightness(dark, 1_020) is None
    assert book.flash_armed is True
    hit = book.note_brightness(white, 1_080)
    assert hit is not None
    assert hit.scene_to_processed_ms == 80
    assert book.flash_armed is False


def test_flash_is_on_demand_not_always_on() -> None:
    book = LatencyBook()
    white = mean_luminance(render_white_flash())
    assert book.note_brightness(white or 255, 50) is None
    book.arm_flash(10)
    assert book.note_brightness(30, 20) is None  # not bright enough


def test_flash_expires() -> None:
    book = LatencyBook()
    book.arm_flash(0)
    assert book.note_brightness(255, 20_000) is None
    assert book.flash_armed is False


def test_tiny_jpeg_brightness_does_not_crash() -> None:
    # 1x1 protocol probe may not decode on every Pillow; must not raise.
    mean_luminance(MINI_JPEG)


def test_snapshot_shape() -> None:
    book = LatencyBook()
    snap = book.snapshot()
    assert snap["ok"] is True
    assert "clock" in snap and "e2e_ms" in snap and "stages_ms" in snap and "flash" in snap
