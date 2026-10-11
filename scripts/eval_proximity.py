"""Offline eval of the proximity sensor against labels, and a threshold sweep.

    python scripts/eval_proximity.py sessions/<id> [sessions/<id2> ...]
    python scripts/eval_proximity.py sessions/<id> --json

Needs frames/ and labels.jsonl (scripts/prelabel.py) in each session.

Ground truth and the sensor use separate thresholds. The truth thresholds say
what the walker should be warned about (--truth-near 1.6, --truth-danger 0.8,
applied to the labeled distances). The sensor's thresholds, band, and
percentile are swept to compensate for the model's bias: if it overestimates
distance, its near threshold should sit above 1.6 m.

Score per config: 5 x false-clear rate + false-alarm rate, where a false clear
is truth near/danger but sensor clear (the dangerous mistake) and a false alarm
is truth clear but sensor near/danger. The best config is printed as env vars.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.ai.proximity import SECTORS, SEVERITY, ProximityConfig, level_for, sector_distances  # noqa: E402

FALSE_CLEAR_WEIGHT = 5.0
MIN_POSITIVES = 30  # fewer near/danger examples than this cannot tune anything
GRID = {
    "roi_top": [0.10, 0.15, 0.20],
    "roi_bottom": [0.65, 0.75, 0.85],
    "percentile": [5.0, 10.0, 20.0],
    "near_m": [round(x, 1) for x in np.arange(1.2, 2.61, 0.2)],
    "danger_m": [0.6, 0.8, 1.0, 1.2],
}


def load_labels(session_dir: Path) -> dict[int, dict[str, float]]:
    out = {}
    for line in (session_dir / "labels.jsonl").read_text().splitlines():
        if line.strip():
            lab = json.loads(line)
            out[lab["frame_key"]] = {s: float(lab[s]["nearest_m"]) for s in SECTORS}
    return out


def score(
    predicted: list[dict[str, float]],
    truth: list[dict[str, float]],
    sensor: ProximityConfig,
    truth_cfg: ProximityConfig,
) -> dict[str, Any]:
    """Compare per-sector levels. Distances are clamped at 5 m for the error."""
    n = false_clear = false_alarm = exact = positives = negatives = 0
    abs_err = []
    for pred, true in zip(predicted, truth):
        for s in SECTORS:
            p = level_for(pred[s], sensor)
            t = level_for(true[s], truth_cfg)
            n += 1
            exact += p == t
            if t == "clear":
                negatives += 1
                false_alarm += p != "clear"
            else:
                positives += 1
                false_clear += p == "clear"
            abs_err.append(abs(min(pred[s], 5.0) - min(true[s], 5.0)))
    fc_rate = false_clear / positives if positives else 0.0
    fa_rate = false_alarm / negatives if negatives else 0.0
    return {
        "sectors": n,
        "positives": positives,
        "false_clear_rate": fc_rate,
        "false_alarm_rate": fa_rate,
        "level_accuracy": exact / n if n else 0.0,
        "distance_mae_m": float(np.mean(abs_err)) if abs_err else 0.0,
        "cost": FALSE_CLEAR_WEIGHT * fc_rate + fa_rate,
    }


def sweep(
    depths: list[np.ndarray], truth: list[dict[str, float]], truth_cfg: ProximityConfig, grid: dict[str, list] = GRID
) -> list[dict[str, Any]]:
    results = []
    for top, bottom, pct in itertools.product(grid["roi_top"], grid["roi_bottom"], grid["percentile"]):
        band = ProximityConfig(roi_top=top, roi_bottom=bottom, percentile=pct)
        predicted = [sector_distances(d, band) for d in depths]  # once per band
        for near, danger in itertools.product(grid["near_m"], grid["danger_m"]):
            if danger >= near:
                continue
            cfg = ProximityConfig(roi_top=top, roi_bottom=bottom, percentile=pct, near_m=near, danger_m=danger)
            results.append({"config": asdict(cfg), **score(predicted, truth, cfg, truth_cfg)})
    # On a tie, warn earlier: higher thresholds and a lower percentile are the cautious side.
    results.sort(
        key=lambda r: (
            r["cost"],
            r["false_clear_rate"],
            -r["config"]["near_m"],
            -r["config"]["danger_m"],
            r["config"]["percentile"],
        )
    )
    return results


def collect(session_dirs: list[Path]) -> tuple[list[np.ndarray], list[dict[str, float]]]:
    from server.ai import depth

    model = depth.load()
    if model is None:
        raise SystemExit("depth model missing: run scripts/export_depth_onnx.py")
    depths, truth = [], []
    for session_dir in session_dirs:
        labels = load_labels(session_dir)
        for key, dist in sorted(labels.items()):
            jpg = session_dir / "frames" / f"{key}.jpg"
            if jpg.exists():
                depths.append(model.predict(jpg.read_bytes()))
                truth.append(dist)
        print(f"{session_dir}: {len(labels)} labels", file=sys.stderr)
    return depths, truth


def env_line(cfg: dict[str, float]) -> str:
    return (
        f"SAW_ROI_TOP={cfg['roi_top']} SAW_ROI_BOTTOM={cfg['roi_bottom']} "
        f"SAW_SECTOR_PERCENTILE={cfg['percentile']:g} SAW_NEAR_M={cfg['near_m']} SAW_DANGER_M={cfg['danger_m']}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sessions", nargs="+", type=Path)
    p.add_argument("--truth-near", type=float, default=1.6)
    p.add_argument("--truth-danger", type=float, default=0.8)
    p.add_argument("--top", type=int, default=5, help="configs to show")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    truth_cfg = ProximityConfig(near_m=args.truth_near, danger_m=args.truth_danger)
    depths, truth = collect(args.sessions)
    if not depths:
        raise SystemExit("no labeled frames with JPEGs found")
    current = ProximityConfig.from_env()
    baseline = score([sector_distances(d, current) for d in depths], truth, current, truth_cfg)
    ranked = sweep(depths, truth, truth_cfg)
    if args.json:
        print(json.dumps({"current": {"config": asdict(current), **baseline}, "ranked": ranked[: args.top]}, indent=2))
        return 0
    print(f"{len(depths)} frames, {baseline['positives']} near/danger sectors in truth\n")
    print("config                                                  false-clear  false-alarm  accuracy  mae_m")
    rows = [("current", {"config": asdict(current), **baseline})] + [(f"#{i + 1}", r) for i, r in enumerate(ranked[: args.top])]
    for name, r in rows:
        c = r["config"]
        desc = f"{name:<8} band {c['roi_top']:.2f}-{c['roi_bottom']:.2f} p{c['percentile']:<4g} near<{c['near_m']} danger<{c['danger_m']}"
        print(f"{desc:<56} {r['false_clear_rate']:>10.1%} {r['false_alarm_rate']:>12.1%} {r['level_accuracy']:>9.1%} {r['distance_mae_m']:>6.2f}")
    if baseline["positives"] < MIN_POSITIVES:
        print(
            f"\nNot enough near/danger examples ({baseline['positives']} < {MIN_POSITIVES}) to tune. "
            "Keep the defaults and record walks that approach walls, doors, and people."
        )
        return 0
    print("\nbest: " + env_line(ranked[0]["config"]))
    if ranked[0]["false_clear_rate"] > 0.05:
        print("WARNING: best config still misses more than 5% of near/danger sectors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
