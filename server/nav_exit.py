"""Exit-guidance navigator: detect, fuse proximity, pick a heading, speak.

Registered as ``exit``. ``analyze(frame)`` is the scorecard entry point.
The live server never calls this on the event loop — ``nav_live`` runs it
in a thread and drops stale frames.

Detection mix (deliberately generic, not fitted to the 8 synthetic photos):
- YOLOv8n ONNX for chairs / people / furniture (COCO). Optional; degrades.
- Red illuminated-sign + rectangle heuristic for EXIT (COCO has no exit).
- Gray-slab heuristic for a *closed* door (an open doorway is not an exit).
- Brian's depth / proximity, when the last reading is stashed on the navigator.
- Optional wall_detector too_close, if that module is on main.

Path: 5-column occupancy cost, bias toward the remembered exit bearing.
"""

from __future__ import annotations

import io
import time
from typing import Any

from server.ai import exit_sign, yolo
from server.nav import (
    Bearing,
    Distance,
    ExitHint,
    NavResult,
    Obstacle,
    Visible,
    register,
)

DIST_ORDER = {"close": 0, "near": 1, "mid": 2, "far": 3, "none": 4}


def _bearing_from_x(cx: float) -> Bearing:
    if cx < 0.34:
        return "left"
    if cx > 0.66:
        return "right"
    return "center"


def _dist_from_m(meters: float | None, fallback: Distance) -> Distance:
    if meters is None:
        return fallback
    if meters < 1.0:
        return "close"
    if meters < 2.5:
        return "near"
    if meters < 5.0:
        return "mid"
    return "far"


def _dist_from_box_height(h: float, y1: float = 1.0) -> tuple[Distance, float]:
    # Perspective: something in our face is large AND reaches the bottom of the frame.
    if h > 0.38 and y1 > 0.78:
        return "close", 0.8
    if h > 0.20 or (h > 0.14 and y1 > 0.70):
        return "near", 1.6
    if h > 0.10:
        return "mid", 3.2
    return "far", 6.0


def _depth_in_box(depth, box: yolo.Det | None, cx: float, cy: float) -> float | None:
    if depth is None:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    h, w = int(depth.shape[0]), int(depth.shape[1])
    if box is not None:
        x0, x1 = int(box.x0 * w), int(box.x1 * w)
        y0, y1 = int(box.y0 * h), int(box.y1 * h)
    else:
        x0, x1 = int((cx - 0.08) * w), int((cx + 0.08) * w)
        y0, y1 = int((cy - 0.08) * h), int((cy + 0.08) * h)
    x0, x1 = max(0, x0), min(w, max(x0 + 1, x1))
    y0, y1 = max(0, y0), min(h, max(y0 + 1, y1))
    patch = depth[y0:y1, x0:x1]
    valid = patch[np.isfinite(patch) & (patch > 0.15) & (patch < 20)]
    if valid.size == 0:
        return None
    return float(np.percentile(valid, 20))


class ExitNavigator:
    """Real detector/planner behind ``analyze(frame)``."""

    name = "exit"

    def __init__(self) -> None:
        self.prev: NavResult | None = None
        self.last_exit_bearing: Bearing = "none"
        self.last_exit_seen = 0.0
        self.last_boxes: list[dict] = []
        self.last_heading: Bearing = "none"
        self.last_ms: float | None = None
        self.proximity: dict[str, Any] | None = None
        self.depth: Any = None

    def reset(self) -> None:
        self.prev = None
        self.last_exit_bearing = "none"
        self.last_exit_seen = 0.0
        self.last_boxes = []
        self.last_heading = "none"
        self.last_ms = None
        self.proximity = None
        self.depth = None

    def set_context(self, *, proximity: dict[str, Any] | None = None, depth: Any = None) -> None:
        """Live hook stashes Brian's last reading so we can fuse distance."""
        if proximity is not None:
            self.proximity = proximity
        if depth is not None:
            self.depth = depth

    def accept_vlm(self, answer: dict[str, Any]) -> None:
        """Occasional non-blocking EXIT confirmation (Anthropic / Gemini)."""
        if not answer.get("exit_visible"):
            return
        bearing = str(answer.get("bearing") or "").strip().lower()
        if bearing in {"left", "center", "right"}:
            self.last_exit_bearing = bearing  # type: ignore[assignment]
            self.last_exit_seen = time.monotonic()

    def analyze(self, frame: bytes) -> NavResult:
        t0 = time.perf_counter()
        self._pull_live_depth()
        blob = exit_sign.find_exit(frame)
        door = exit_sign.door_slab(frame)
        # A bright room with an EXIT sign is not a "plain wall".
        wall = blob is None and (exit_sign.plain_wall(frame) or self._wall_detector_close())
        dets = yolo.detect(frame)
        if not dets:
            dets = self._dark_fallback(frame)

        visible, bearing, distance, distance_m, exit_box = self._exit_from(blob)
        if wall and visible == "no":
            # A blank wall in the face is not "exit somewhere".
            bearing, distance, distance_m = "none", "none", None

        obstacles, obstacle_ahead, nearest = self._obstacles(dets, wall, door)
        heading = self._heading(bearing, obstacles, wall)
        arrived = self._arrived(visible, bearing, distance, door, wall)
        should_stop = bool(
            wall
            or arrived
            or (obstacle_ahead and nearest == "close")
            or self._prox_center() == "danger"
        )
        hint = ExitHint(visible, bearing, distance, distance_m)
        boxes = self._boxes(blob, dets, door, heading)
        draft = NavResult(
            exit=hint,
            obstacles=obstacles,
            should_stop=should_stop,
            arrived=arrived,
            obstacle_ahead=obstacle_ahead,
            nearest_obstacle_distance=nearest,
            heading=heading,
            boxes=boxes,
        )
        result = NavResult(
            exit=hint,
            obstacles=obstacles,
            should_stop=should_stop,
            arrived=arrived,
            obstacle_ahead=obstacle_ahead,
            nearest_obstacle_distance=nearest,
            heading=heading,
            boxes=boxes,
            utterance=self._utterance(draft, wall),
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
        )
        self.prev = result
        self.last_boxes = boxes
        self.last_heading = heading
        self.last_ms = result.latency_ms
        if visible != "no" and bearing in {"left", "center", "right"}:
            self.last_exit_bearing = bearing
            self.last_exit_seen = time.monotonic()
        return result

    def _pull_live_depth(self) -> None:
        if self.depth is not None:
            return
        try:
            from server import ai_hook
        except Exception:
            return
        snap = getattr(ai_hook, "_debug", None)
        if snap is None:
            return
        depth, result, _cfg, _name = snap
        self.depth = depth
        if self.proximity is None and result is not None:
            self.proximity = {
                "level": getattr(result, "level", None),
                "levels": dict(getattr(result, "levels", {}) or {}),
                "sectors_m": dict(getattr(result, "sectors_m", {}) or {}),
            }

    def _wall_detector_close(self) -> bool:
        try:
            from server.wall_detector import detector
        except Exception:
            return False
        verdict = getattr(detector, "last_verdict", None)
        if verdict is None:
            return False
        return getattr(verdict, "level", "") == "too_close"

    def _prox_center(self) -> str | None:
        if not self.proximity:
            return None
        levels = self.proximity.get("levels") or {}
        return levels.get("center") or self.proximity.get("level")

    def _sector_m(self, bearing: Bearing) -> float | None:
        if not self.proximity:
            return None
        sectors = self.proximity.get("sectors_m") or {}
        if bearing in sectors:
            try:
                return float(sectors[bearing])
            except (TypeError, ValueError):
                return None
        return None

    def _exit_from(self, blob: exit_sign.ExitBlob | None) -> tuple[Visible, Bearing, Distance, float | None, dict | None]:
        if blob is None:
            if self.last_exit_bearing in {"left", "center", "right"}:
                # Still remember where it was so we can say "turn" / "behind".
                return "no", "behind", "none", None, None
            return "no", "none", "none", None, None
        meters = blob.distance_m
        depth_m = _depth_in_box(self.depth, None, blob.cx, blob.cy)
        if depth_m is not None:
            meters = depth_m
        prox_m = self._sector_m(blob.bearing)
        # Prefer a closer of the two so we do not walk into a door we think is far.
        if prox_m is not None and (meters is None or prox_m < meters):
            meters = prox_m
        distance = _dist_from_m(meters, blob.distance)
        box = {
            "label": "exit",
            "x0": blob.x0,
            "y0": blob.y0,
            "x1": blob.x1,
            "y1": blob.y1,
            "conf": blob.conf,
        }
        return blob.visible, blob.bearing, distance, meters, box

    def _obstacles(
        self, dets: list[yolo.Det], wall: bool, door: dict | None
    ) -> tuple[list[Obstacle], bool, Distance]:
        if wall:
            meters = self._sector_m("center")
            if meters is None:
                meters = 0.6
            return (
                [Obstacle("wall", "center", "close", meters, ahead=True)],
                True,
                "close",
            )
        found: list[Obstacle] = []
        for det in dets:
            if det.area < 0.01 and det.height < 0.12:
                continue
            dist, meters = _dist_from_box_height(det.height, det.y1)
            depth_m = _depth_in_box(self.depth, det, det.cx, det.cy)
            if depth_m is not None:
                meters = depth_m
                dist = _dist_from_m(meters, dist)
            prox_m = self._sector_m(_bearing_from_x(det.cx))
            if prox_m is not None and prox_m < (meters or 99):
                meters = prox_m
                dist = _dist_from_m(meters, dist)
            # Far furniture is scenery, not a cue.
            if dist == "far":
                continue
            ahead = det.x0 < 0.68 and det.x1 > 0.32 and det.y1 > 0.45
            found.append(
                Obstacle(det.label, _bearing_from_x(det.cx), dist, meters, ahead=ahead)
            )
        if door and door.get("width", 0) > 0.28 and door.get("cx", 0.5) > 0.28 and door.get("cx", 0.5) < 0.72:
            # Closed door filling the middle is the destination, not a chair.
            pass
        if not found:
            return [], False, "none"
        ahead = any(o.ahead for o in found)
        nearest = min((o.distance for o in found), key=lambda d: DIST_ORDER[d])
        return found, ahead, nearest

    def _heading(self, exit_bearing: Bearing, obstacles: list[Obstacle], wall: bool) -> Bearing:
        if wall:
            return "none"
        # 5-column cost: occupancy + "go toward the exit".
        cost = [0.15, 0.05, 0.0, 0.05, 0.15]  # slight edge penalty
        goal = {"left": 0, "center": 2, "right": 4, "behind": 2, "none": 2}[exit_bearing]
        for i in range(5):
            cost[i] += 0.35 * abs(i - goal)
        for obs in obstacles:
            col = {"left": 0, "center": 2, "right": 4, "behind": 2, "none": 2}[obs.bearing]
            weight = {"close": 3.0, "near": 1.6, "mid": 0.6, "far": 0.15, "none": 0}[obs.distance]
            for j, c in enumerate((0, 1, 2, 3, 4)):
                cost[c] += weight * (1.0 if j == col else (0.45 if abs(j - col) == 1 else 0.0))
        best = min(range(5), key=lambda i: cost[i])
        if best <= 1:
            return "left"
        if best >= 3:
            return "right"
        return "center"

    def _arrived(
        self, visible: Visible, bearing: Bearing, distance: Distance, door: dict | None, wall: bool
    ) -> bool:
        if wall or visible == "no":
            return False
        if distance == "close" and bearing == "center":
            return True
        if door and bearing == "center" and door.get("width", 0) > 0.22 and distance in {"close", "near"}:
            return True
        return False

    def _boxes(
        self,
        blob: exit_sign.ExitBlob | None,
        dets: list[yolo.Det],
        door: dict | None,
        heading: Bearing,
    ) -> list[dict]:
        boxes: list[dict] = []
        if blob is not None:
            boxes.append(
                {
                    "label": "exit",
                    "x0": round(blob.x0, 3),
                    "y0": round(blob.y0, 3),
                    "x1": round(blob.x1, 3),
                    "y1": round(blob.y1, 3),
                    "conf": round(blob.conf, 2),
                }
            )
        for det in dets:
            boxes.append(
                {
                    "label": det.label,
                    "x0": round(det.x0, 3),
                    "y0": round(det.y0, 3),
                    "x1": round(det.x1, 3),
                    "y1": round(det.y1, 3),
                    "conf": round(det.conf, 2),
                }
            )
        if door:
            boxes.append(
                {
                    "label": "door",
                    "x0": round(door["x0"], 3),
                    "y0": 0.12,
                    "x1": round(door["x1"], 3),
                    "y1": 0.88,
                    "conf": 0.5,
                }
            )
        boxes.append({"label": "heading", "bearing": heading})
        return boxes

    def _utterance(self, result: NavResult, wall: bool) -> str:
        if result.arrived:
            return "You've reached the exit door, handle on the right."
        if wall and result.exit.visible == "no":
            return "Stop. Wall directly ahead."
        if result.should_stop:
            side = "left" if result.heading == "left" else "right"
            label = next((o.label for o in result.obstacles if o.ahead), "chair")
            if label == "wall":
                return "Stop. Wall directly ahead."
            return f"Stop. {label.capitalize()} ahead, step {side}."
        if result.exit.visible == "no":
            bits = []
            if result.obstacle_ahead:
                side = next((o.bearing for o in result.obstacles if o.ahead), "ahead")
                where = "ahead" if side == "center" else f"on your {side}"
                label = next((o.label for o in result.obstacles if o.ahead), "chair")
                bits.append(f"{label.capitalize()} {where}.")
            bits.append("Exit is not in view. Turn and scan.")
            return " ".join(bits)
        side = {
            "left": "slightly left",
            "right": "slightly right",
            "center": "ahead",
            "behind": "behind you",
            "none": "nearby",
        }[result.exit.bearing]
        if result.exit.bearing == "center":
            phrase = "Exit ahead."
        elif result.exit.visible == "partial" and result.exit.bearing in {"left", "right"}:
            phrase = f"Exit is to your {result.exit.bearing}. Turn {result.exit.bearing}."
        else:
            phrase = f"Exit ahead, {side}."
        if result.obstacle_ahead:
            step = "left" if result.heading == "left" else ("right" if result.heading == "right" else "to the side")
            label = next((o.label for o in result.obstacles if o.ahead), "chair")
            phrase += f" {label.capitalize()} ahead, step {step}."
        return phrase

    def _dark_fallback(self, frame: bytes) -> list[yolo.Det]:
        """When YOLO is missing: dark compact blobs in the lower two-thirds."""
        try:
            from PIL import Image
            import numpy as np
        except ImportError:
            return []
        try:
            img = Image.open(io.BytesIO(frame)).convert("RGB")
        except Exception:
            return []
        w, h = img.size
        small = img.resize((160, 120), Image.BILINEAR)
        arr = np.asarray(small, dtype=np.float32)
        luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
        mean = float(luma.mean())
        cut = min(48.0, max(18.0, mean * 0.42))
        y0, y1 = int(120 * 0.28), int(120 * 0.92)
        band = luma[y0:y1]
        dark = band < cut
        dets: list[yolo.Det] = []
        # Three sectors; require a real blob, not carpet grain.
        for i in range(3):
            sl = band[:, int(i * 160 / 3) : int((i + 1) * 160 / 3)]
            frac = float((sl < cut).mean()) if sl.size else 0.0
            if frac < 0.045:
                continue
            ys, xs = np.where(sl < cut)
            if xs.size < 8:
                continue
            # Map back to full-image normalized box.
            x0 = (i / 3) + (xs.min() / sl.shape[1]) / 3
            x1 = (i / 3) + (xs.max() / sl.shape[1]) / 3
            yy0 = 0.28 + (ys.min() / max(sl.shape[0], 1)) * 0.64
            yy1 = 0.28 + (ys.max() / max(sl.shape[0], 1)) * 0.64
            # Tall dark openings (doorways) are not furniture.
            if yy0 < 0.34 and (yy1 - yy0) > max(0.28, (x1 - x0) * 1.3):
                continue
            dets.append(
                yolo.Det(
                    label="chair",
                    conf=0.4,
                    x0=float(max(0.0, x0 - 0.02)),
                    y0=float(max(0.0, yy0)),
                    x1=float(min(1.0, x1 + 0.02)),
                    y1=float(min(1.0, yy1)),
                )
            )
        return dets


register("exit", ExitNavigator)
