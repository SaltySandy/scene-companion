"""DRAFT test for the exitpath navigator (server/exit_nav.py).

Unit tests (no model): planner geometry and wording.
Fixture tests (need the DA2 ONNX): per-scene exit / path / safety checks against
manifest.json + exit_labels.draft.json (draft, unreviewed labels).
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("onnxruntime")

from server import exit_nav as en  # noqa: E402
from server.ai.depth import DEFAULT_MODEL  # noqa: E402
from server.exit_score import said_side, score_scene  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "scenes"
MODEL = Path(os.environ.get("SAW_DEPTH_MODEL") or DEFAULT_MODEL)
# Eyeballed, unreviewed labels (see the file's _status).
LABELS = json.loads((FIX / "exit_labels.draft.json").read_text(encoding="utf-8"))["scenes"]
MANIFEST = {s["id"]: s for s in json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))["scenes"]}
needs_model = pytest.mark.skipif(not MODEL.exists(), reason=f"depth ONNX not found at {MODEL} (set SAW_DEPTH_MODEL)")


# ------------------------------------------------------------------ unit (no model)
def _grid_with_block():
    nz, nx = 60, 101
    occ = np.zeros((nz, nx), bool); occ[18:26, 45:56] = True        # 1 m wide block 1.8-2.6 m ahead
    from scipy import ndimage as ndi
    r = int(math.ceil((en.CFG["body_r"] + en.CFG["margin"]) / 0.1)); yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    inf = ndi.binary_dilation(occ, structure=(yy ** 2 + xx ** 2) <= r * r)
    return en.Grid(occ, ~occ, inf, 0.1, 5.0, 5.9)


def test_astar_goes_around_block_with_clearance():
    g = _grid_with_block(); start = g.ij(0, 0); goal = g.ij(0, 4.5)
    p = en.astar(g, start, goal)
    assert p is not None and p[0] == start and p[-1] == goal
    xz = np.array([g.xz(i, j) for i, j in p])
    beside = xz[(xz[:, 1] >= 1.8) & (xz[:, 1] <= 2.6)]
    # block spans x in [-0.5, 0.5]; body radius + margin = 0.35 -> |x| >= 0.85 while passing
    assert np.all(np.abs(beside[:, 0]) >= 0.85 - 1e-6)


def test_astar_none_when_fully_walled():
    g = _grid_with_block(); g.inflated[30, :] = True
    assert en.astar(g, g.ij(0, 0), g.ij(0, 4.5)) is None


def test_wording_and_buckets():
    assert en.dir_words(3) == "straight ahead" and en.dir_words(15) == "ahead, slightly right"
    assert en.dir_words(-30) == "ahead to your left" and en.turn_words(60) == "turn right"
    assert [en.bucket_distance(v) for v in (0.5, 1.5, 3, 7, None)] == ["close", "near", "mid", "far", "none"]
    assert said_side("Obstacle on your right at about 2 meters, keep left of it.") == "right"


# ------------------------------------------------------------------ fixtures (model)
@pytest.fixture(scope="module")
def results():
    if not MODEL.exists():
        pytest.skip("depth ONNX not present")
    out = {}
    for sid, s in MANIFEST.items():
        o = en.analyze_frame((FIX / "640x480" / s["file"]).read_bytes())
        e = o["exit_geom"]
        out[sid] = dict(o=o, res=dict(
            door_box=o["exit"]["door"] and list(o["exit"]["door"]["box"]),
            sign_box=o["exit"]["sign"] and list(o["exit"]["sign"]["box"]),
            exit_bearing_deg=e and e["bearing_deg"],
            first_leg_heading_deg=o["legs"][0]["heading_deg"] if o["legs"] else None,
            nav=o["nav_fields"], say=o["say"]))
    return out


@needs_model
def test_deterministic():
    jpeg = (FIX / "640x480" / "01_entry_overview.jpg").read_bytes()
    a, b = en.analyze_frame(jpeg), en.analyze_frame(jpeg)
    assert a["say"] == b["say"] and np.array_equal(a["path"], b["path"])


@needs_model
@pytest.mark.parametrize("sid", list(MANIFEST))
def test_exit_visibility_matches_manifest(results, sid):
    want = MANIFEST[sid]["exit_visible"] != "no"
    assert (results[sid]["res"]["exit_bearing_deg"] is not None) == want


@needs_model
def test_doorway_without_sign_is_not_the_exit(results):
    r = results["08_exit_not_visible"]
    assert r["res"]["door_box"] is None and r["o"]["path"] is None
    assert "not in view" in r["o"]["say"].lower()


@needs_model
@pytest.mark.parametrize("sid", [k for k, v in LABELS.items() if v["exit_box"] is not None])
def test_exit_centre_within_tolerance(results, sid):
    m = score_scene(results[sid]["res"], LABELS[sid])
    assert m["exit_center_ok"] == 1.0, m


@needs_model
@pytest.mark.parametrize("sid", list(MANIFEST))
def test_bearing_bucket_matches_manifest(results, sid):
    gt = MANIFEST[sid]["exit_bearing"]
    if gt in ("behind", "none"):
        pytest.skip("no visible exit")
    assert results[sid]["res"]["nav"]["exit_bearing"] == gt


@needs_model
@pytest.mark.parametrize("sid", list(MANIFEST))
def test_first_turn_and_obstacle_side(results, sid):
    m = score_scene(results[sid]["res"], LABELS[sid])
    assert m["first_turn_ok"] == 1.0, (m, results[sid]["res"]["first_leg_heading_deg"])
    assert m["obstacle_side_ok"] == 1.0, (m, results[sid]["o"]["say"])


@needs_model
@pytest.mark.parametrize("sid", list(MANIFEST))
def test_stop_and_arrival_match_manifest(results, sid):
    n = results[sid]["res"]["nav"]
    assert n["should_stop"] == MANIFEST[sid]["should_stop"]
    assert n["arrived"] == MANIFEST[sid]["arrived"]


@needs_model
def test_path_keeps_body_clearance(results):
    """Safety invariant on our own map: no path cell lies inside an inflated obstacle,
    except the start/goal disks the planner deliberately clears."""
    for sid, r in results.items():
        o = r["o"]
        if o["path"] is None:
            continue
        g = o["grid"]; pts = o["path"]
        L = np.cumsum(np.r_[0, np.hypot(*np.diff(pts, axis=0).T)])
        mid = pts[(L > 0.35) & (L < L[-1] - 0.45)]
        bad = [p for p in mid if g.inflated[g.ij(*p)]]
        assert not bad, (sid, bad[:3])


def test_exitpath_registered_and_explicit_choice_sticks(monkeypatch):
    from server import nav

    monkeypatch.delenv("SAW_NAVIGATOR", raising=False)
    assert "exitpath" in nav.NAVIGATORS
    nav._default = None
    picked = nav.default_navigator("exitpath")
    assert nav.default_navigator() is picked  # evaluate(navigator=...) no longer falls back to baseline
    nav._default = None


# ----------------------------------------------------------------------------- sign-shape filter
def _canvas(bg=(90, 90, 90)):
    from PIL import Image
    return Image.new("RGB", (640, 480), bg)


def _draw(img, box, fill, text=None):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=fill)
    if text:  # light lettering inside the box, like a real EXIT sign
        x0, y0, x1, y1 = box
        for k in range(4):
            cx = x0 + 4 + k * (x1 - x0 - 8) // 4
            d.rectangle((cx, y0 + 3, cx + 2, y1 - 3), fill=text)
    return img


def test_sign_filter_rejects_red_wall():
    img = _draw(_canvas(), (60, 30, 600, 420), (230, 30, 25))  # huge red tiled wall (c04-style)
    assert en.find_exit_sign(img) is None
    c = [c for c in en.sign_candidates(img) if c["colour"] == "red"]
    assert c and "size" in c[0]["reasons"]


def test_sign_filter_rejects_dim_board_and_speck():
    img = _canvas()
    _draw(img, (70, 175, 160, 219), (140, 30, 30))   # dull printed red board (not self-lit)
    _draw(img, (400, 90, 405, 94), (255, 40, 40))    # tiny red speck
    assert en.find_exit_sign(img) is None


def test_sign_filter_accepts_red_sign_shaped_blob_and_prefers_it_over_larger_blob():
    img = _canvas()
    _draw(img, (300, 60, 350, 80), (250, 40, 40), text=(255, 230, 230))  # 2.5:1 lit sign
    _draw(img, (80, 100, 120, 220), (250, 40, 40))  # bigger but tall/narrow red post
    s = en.find_exit_sign(img)
    assert s is not None and s["colour"] == "red"
    x0, y0, x1, y1 = s["box"]
    assert 295 <= x0 <= 305 and 345 <= x1 <= 355


def test_sign_filter_accepts_green_sign():
    img = _canvas()
    _draw(img, (420, 50, 480, 74), (40, 230, 90), text=(235, 255, 235))
    s = en.find_exit_sign(img)
    assert s is not None and s["colour"] == "green"


def test_sign_filter_rejects_sign_low_in_frame():
    img = _draw(_canvas(), (300, 420, 350, 440), (250, 40, 40))
    assert en.find_exit_sign(img) is None


# ----------------------------------------------------------------------------- floor-fit fallback
def _fake_frame(monkeypatch, depth):
    """Run analyze_frame on a flat grey image with a given raw depth map and a failed floor fit."""
    import io
    from PIL import Image
    from dataclasses import replace
    monkeypatch.setattr(en, "depth_landscape", lambda *a, **k: depth.astype(np.float32))
    real_fit = en.fit_floor
    monkeypatch.setattr(en, "fit_floor", lambda *a, **k: replace(
        real_fit(*a, **k), n_up=np.array([0.0, -1.0, 0.0]), h=en.CFG["cam_h_m"], scale_source="raw"))
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (120, 120, 120)).save(buf, "JPEG")
    return en.analyze_frame(buf.getvalue())


def test_floor_fallback_open_view_does_not_stop(monkeypatch):
    rows = np.linspace(8.0, 1.5, 240)[:, None]  # far at top, near at bottom: deep corridor-like view
    o = _fake_frame(monkeypatch, np.repeat(rows, 320, axis=1))
    assert o["floor_unsure"] and not o["nav_fields"]["should_stop"]
    assert "Not sure of the floor" in o["say"] and "Wall directly ahead" not in o["say"]


def test_floor_fallback_close_centre_stops(monkeypatch):
    d = np.full((240, 320), 6.0); d[30:190, 100:220] = 0.6   # something genuinely close in the centre
    o = _fake_frame(monkeypatch, d)
    assert o["floor_unsure"] and o["nav_fields"]["should_stop"] and o["say"].startswith("Stop")


def test_floor_fallback_flat_surface_filling_view_stops(monkeypatch):
    o = _fake_frame(monkeypatch, np.full((240, 320), 2.4))   # one uniform surface (06_plain_wall_close)
    assert o["floor_unsure"] and o["nav_fields"]["should_stop"]
