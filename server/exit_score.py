"""Proposed exit-path metrics (draft). Pixel-space first so the unknown FOV of the synthetic
fixtures does not enter the pass/fail, degrees reported alongside under the assumed HFOV."""
from __future__ import annotations
import math, re

CENTER_PX_TOL = 40     # |exit centre x error| <= 40 px of 640 (~4 deg at an assumed 66 deg HFOV)
IOU_MIN = 0.5
TURN_DEADBAND_DEG = 5.0


def iou(a, b):
    if a is None or b is None:
        return 0.0
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy; u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def px_to_deg(u, hfov=66.0, W=640):
    f = (W / 2) / math.tan(math.radians(hfov / 2)); return math.degrees(math.atan((u - W / 2 + 0.5) / f))


def said_side(text):
    t = text.lower()
    m = re.search(r"(obstacle|wall|chair)[^.]*?\b(on your|to your)\s+(left|right)", t)
    if m:
        return m.group(3)
    if "directly ahead" in t:
        return "center"
    return "none"


def score_scene(res: dict, lab: dict) -> dict:
    """res: one entry of out/exitpath_results.json; lab: one scene of exit_labels.draft.json."""
    m: dict = {}
    pred_box = res.get("door_box"); pred_sign = res.get("sign_box")
    has_label = lab["exit_box"] is not None
    has_pred = res.get("exit_bearing_deg") is not None
    m["exit_detect"] = 1.0 if has_pred == has_label else 0.0
    if has_label and has_pred:
        lc = (lab["exit_box"][0] + lab["exit_box"][2]) / 2
        if pred_box:
            pc = (pred_box[0] + pred_box[2]) / 2
        else:  # sign-only: centre of the sign
            pc = (pred_sign[0] + pred_sign[2]) / 2
        m["center_err_px"] = round(abs(pc - lc), 1)
        m["heading_err_deg_assumed_hfov"] = round(abs(px_to_deg(pc) - px_to_deg(lc)), 1)
        m["exit_center_ok"] = 1.0 if m["center_err_px"] <= CENTER_PX_TOL else 0.0
        m["exit_iou"] = round(iou(pred_box, lab["exit_box"]), 2)
        m["exit_iou_ok"] = 1.0 if m["exit_iou"] >= IOU_MIN else 0.0
    # doorway trap: no predicted exit box overlapping a labelled non-exit door
    m["trap_ok"] = 1.0 if all(iou(pred_box, d) < 0.1 for d in lab["non_exit_doors"]) else 0.0
    # first turn of the planned path
    want = lab["path_first_turn"]; h = res.get("first_leg_heading_deg")
    if want == "none":
        ok = h is None or res["nav"]["should_stop"]
    elif want == "any":
        ok = h is not None
    elif want == "straight":
        ok = h is not None and abs(h) < 2 * TURN_DEADBAND_DEG
    else:
        ok = h is not None and (h > TURN_DEADBAND_DEG if want == "right" else h < -TURN_DEADBAND_DEG)
    m["first_turn_ok"] = 1.0 if ok else 0.0
    # obstacle side in the spoken text
    side = said_side(res["say"]); want_side = lab["obstacle_side"]
    m["said_obstacle_side"] = side
    m["obstacle_side_ok"] = 1.0 if (want_side == "none" or side == want_side) else 0.0
    keys = [k for k in m if k.endswith("_ok") or k == "exit_detect"]
    m["score"] = round(sum(m[k] for k in keys) / len(keys), 3)
    return m
