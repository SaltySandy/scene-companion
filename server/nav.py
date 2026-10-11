"""Pluggable navigation stage for exit-finding and spoken guidance.

The live hook and the replay harness both call ``analyze(frame)``. Drop a real
detector or planner in by registering a ``Navigator`` — the scorecard will
pick it up. The default ``baseline`` is a color-heuristic stub so scoring
runs today with no extra model.
"""

from __future__ import annotations

import io
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal, Protocol

VISIBLE = ("yes", "partial", "no")
BEARING = ("left", "center", "right", "behind", "none")
DISTANCE = ("close", "near", "mid", "far", "none")

Visible = Literal["yes", "partial", "no"]
Bearing = Literal["left", "center", "right", "behind", "none"]
Distance = Literal["close", "near", "mid", "far", "none"]


@dataclass(frozen=True)
class ExitHint:
    visible: Visible
    bearing: Bearing
    distance: Distance
    distance_m: float | None = None


@dataclass(frozen=True)
class Obstacle:
    label: str
    bearing: Bearing
    distance: Distance
    distance_m: float | None = None
    ahead: bool = False


@dataclass(frozen=True)
class NavResult:
    """Structured output the scorecard grades. Matches:

    ``{exit: {visible, bearing, distance}, obstacles: [...],
    should_stop, arrived, utterance}``
    """

    exit: ExitHint
    obstacles: list[Obstacle] = field(default_factory=list)
    should_stop: bool = False
    arrived: bool = False
    utterance: str = ""
    obstacle_ahead: bool = False
    nearest_obstacle_distance: Distance = "none"

    def payload(self) -> dict:
        return {
            "exit": {
                "visible": self.exit.visible,
                "bearing": self.exit.bearing,
                "distance": self.exit.distance,
                "distance_m": self.exit.distance_m,
            },
            "obstacles": [asdict(o) for o in self.obstacles],
            "should_stop": self.should_stop,
            "arrived": self.arrived,
            "utterance": self.utterance,
            "obstacle_ahead": self.obstacle_ahead,
            "nearest_obstacle_distance": self.nearest_obstacle_distance,
        }


class Navigator(Protocol):
    name: str

    def analyze(self, frame: bytes) -> NavResult: ...

    def reset(self) -> None: ...


NAVIGATORS: dict[str, Callable[[], Navigator]] = {}


def register(name: str, factory: Callable[[], Navigator]) -> None:
    NAVIGATORS[name] = factory


def get_navigator(name: str | None = None) -> Navigator:
    key = (name or os.environ.get("SAW_NAVIGATOR") or "baseline").strip()
    if key not in NAVIGATORS:
        raise KeyError(f"unknown navigator {key!r}; have {sorted(NAVIGATORS)}")
    return NAVIGATORS[key]()


_default: Navigator | None = None


def default_navigator(name: str | None = None) -> Navigator:
    """The shared navigator. An explicit ``name`` (e.g. ``evaluate(navigator=...)``) sticks:
    later ``analyze()`` calls with no name keep it instead of falling back to
    ``SAW_NAVIGATOR`` / baseline."""
    global _default
    if name is None and _default is not None:
        return _default
    want = (name or os.environ.get("SAW_NAVIGATOR") or "baseline").strip()
    if _default is None or getattr(_default, "name", None) != want:
        _default = get_navigator(want)
    return _default


def reset_navigator() -> None:
    global _default
    if _default is not None:
        _default.reset()


def analyze(frame: bytes) -> NavResult:
    """Entry point: ``analyze(frame) -> NavResult``.

    Detection and path planning replace ``BaselineNavigator`` (or register a
    new name and set ``SAW_NAVIGATOR``).
    """
    return default_navigator().analyze(frame)


def nav_enabled() -> bool:
    return (os.environ.get("SAW_NAV") or "0").strip() not in {"0", "", "false", "off"}


# ---------------------------------------------------------------------------
# Trivial baseline: red EXIT blob + dark-object / blank-wall heuristics
# ---------------------------------------------------------------------------


def _load_rgb(frame: bytes):
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        return Image.open(io.BytesIO(frame)).convert("RGB")
    except Exception:
        return None


def _sample(img, step: int = 2):
    w, h = img.size
    px = img.load()
    red_x = red_y = red_n = 0
    dark = [0, 0, 0]
    dark_n = [0, 0, 0]
    gray_center = 0
    bright_sum = 0
    bright_sq = 0
    n = 0
    y0, y1 = int(h * 0.40), int(h * 0.90)
    pixels = []
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            n += 1
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            bright_sum += luma
            bright_sq += luma * luma
            pixels.append((x, y, r, g, b, luma))
            if r > 140 and r > g + 45 and r > b + 45:
                red_n += 1
                red_x += x
                red_y += y
    mean = bright_sum / max(n, 1)
    var = bright_sq / max(n, 1) - mean * mean
    std = var ** 0.5 if var > 0 else 0.0
    # Absolute cap plus relative-to-mean so a dark room is not "all chairs".
    dark_cut = min(58.0, max(10.0, mean * 0.50))
    for x, y, r, g, b, luma in pixels:
        sector = 0 if x < w / 3 else (2 if x > 2 * w / 3 else 1)
        if y0 <= y < y1:
            dark_n[sector] += 1
            if luma < dark_cut:
                dark[sector] += 1
            if sector == 1 and 70 <= luma <= 165 and abs(r - g) < 25 and abs(g - b) < 25:
                gray_center += 1
    red_frac = red_n / max(n, 1)
    cx = (red_x / red_n / w) if red_n else None
    cy = (red_y / red_n / h) if red_n else None
    dark_frac = [dark[i] / max(dark_n[i], 1) for i in range(3)]
    gray_frac = gray_center / max(sum(dark_n), 1)
    return {
        "w": w,
        "h": h,
        "n": n,
        "red_n": red_n,
        "red_frac": red_frac,
        "red_cx": cx,
        "red_cy": cy,
        "mean": mean,
        "std": std,
        "dark_frac": dark_frac,
        "gray_frac": gray_frac,
    }


def _bearing_from_x(cx: float | None) -> Bearing:
    if cx is None:
        return "none"
    if cx < 0.38:
        return "left"
    if cx > 0.62:
        return "right"
    return "center"


class BaselineNavigator:
    """Color stub: red EXIT sign, dark chairs, blank-wall stop.

    Not a detector. It exists so the scorecard and the live hook have a
    drop-in ``analyze(frame)`` today. Replace this class or register another
    navigator when YOLO / VLM / planning lands.
    """

    name = "baseline"

    def __init__(self) -> None:
        self.prev: NavResult | None = None

    def reset(self) -> None:
        self.prev = None

    def analyze(self, frame: bytes) -> NavResult:
        img = _load_rgb(frame)
        if img is None:
            result = NavResult(
                exit=ExitHint("no", "none", "none"),
                utterance="I cannot read this frame.",
            )
            self.prev = result
            return result

        s = _sample(img)
        visible, bearing, distance, distance_m = self._exit(s)
        wall = self._plain_wall(s, visible)
        obstacles, obstacle_ahead, nearest = self._obstacles(s, wall)
        arrived = self._arrived(s, visible, bearing, wall)
        should_stop = bool(wall or arrived or (obstacle_ahead and nearest == "close"))
        if wall:
            visible, bearing, distance, distance_m = "no", "none", "none", None
            arrived = False

        if (
            self.prev is not None
            and visible != "no"
            and self.prev.exit.bearing in {"left", "right"}
            and bearing in {"left", "right"}
            and bearing != self.prev.exit.bearing
        ):
            bearing = self.prev.exit.bearing
        hint = ExitHint(visible, bearing, distance, distance_m)
        draft = NavResult(
            exit=hint,
            obstacles=obstacles,
            should_stop=should_stop,
            arrived=arrived,
            obstacle_ahead=obstacle_ahead,
            nearest_obstacle_distance=nearest,
        )
        result = NavResult(
            exit=hint,
            obstacles=obstacles,
            should_stop=should_stop,
            arrived=arrived,
            obstacle_ahead=obstacle_ahead,
            nearest_obstacle_distance=nearest,
            utterance=self._utterance(draft, wall),
        )
        self.prev = result
        return result

    def _exit(self, s: dict) -> tuple[Visible, Bearing, Distance, float | None]:
        if s["red_n"] >= 25:
            visible: Visible = "yes"
        elif s["red_n"] >= 6:
            visible = "partial"
        else:
            return "no", "none", "none", None
        bearing = _bearing_from_x(s["red_cx"])
        # Small high-up blob → far; large / lower blob → closer.
        if s["red_frac"] > 0.0025:
            distance: Distance = "close"
            distance_m = 1.5
        elif s["red_frac"] > 0.0012:
            distance = "mid"
            distance_m = 4.0
        else:
            distance = "far"
            distance_m = 7.0
        return visible, bearing, distance, distance_m

    def _plain_wall(self, s: dict, visible: Visible) -> bool:
        if visible != "no":
            return False
        return s["mean"] > 155 and s["std"] < 38 and max(s["dark_frac"]) < 0.03

    def _obstacles(self, s: dict, wall: bool) -> tuple[list[Obstacle], bool, Distance]:
        if wall:
            return (
                [Obstacle("wall", "center", "close", 0.6, ahead=True)],
                True,
                "close",
            )
        labels = ("left", "center", "right")
        found: list[Obstacle] = []
        for i, name in enumerate(labels):
            frac = s["dark_frac"][i]
            if frac < 0.035:
                continue
            if frac > 0.16:
                dist: Distance = "close"
                meters = 0.8
            elif frac > 0.07:
                dist = "near"
                meters = 1.5
            else:
                dist = "mid"
                meters = 3.0
            found.append(Obstacle("chair", name, dist, meters, ahead=(name == "center" or frac > 0.07)))
        if not found:
            return [], False, "none"
        ahead = any(o.ahead for o in found)
        order = {"close": 0, "near": 1, "mid": 2, "far": 3, "none": 4}
        nearest = min((o.distance for o in found), key=lambda d: order[d])
        return found, ahead, nearest

    def _arrived(self, s: dict, visible: Visible, bearing: Bearing, wall: bool) -> bool:
        if wall or visible == "no":
            return False
        # Gray door slab filling the center, EXIT sign near the top.
        # Only when the EXIT sign is centered and a gray slab fills the
        # middle of the frame — a distant door on the right is not arrival.
        return (
            bearing == "center"
            and s["red_frac"] > 0.0018
            and s["gray_frac"] > 0.06
            and s["red_cy"] is not None
            and 0.05 < s["red_cy"] < 0.25
            and s["red_cx"] is not None
            and 0.35 < s["red_cx"] < 0.65
        )

    def _utterance(self, result: NavResult, wall: bool) -> str:
        if result.arrived:
            return "You have reached the exit door. Handle on the right."
        if wall or (result.should_stop and result.nearest_obstacle_distance == "close" and result.exit.visible == "no"):
            return "Stop. Wall directly ahead."
        if result.should_stop:
            return "Stop. Chair directly ahead. Step to the side, then continue to the exit."
        if result.exit.visible == "no":
            if result.obstacle_ahead:
                return "Chair ahead. Exit is not in view. Turn and scan for the exit sign."
            return "Exit is not in view. Turn and scan for the exit sign."
        side = {
            "left": "to your left",
            "right": "to your right",
            "center": "ahead",
            "behind": "behind you",
            "none": "somewhere nearby",
        }[result.exit.bearing]
        bits = [f"Exit {side}."]
        if result.obstacle_ahead:
            bits.append("Chair ahead, keep to the side.")
        return " ".join(bits)


register("baseline", BaselineNavigator)


def _exitpath_factory() -> Navigator:
    # Lazy: needs numpy/scipy/onnxruntime + the depth ONNX (see server/exit_nav.py).
    from server.exit_nav import ExitPathNavigator

    return ExitPathNavigator()


register("exitpath", _exitpath_factory)


def on_frame_nav(jpeg_bytes: bytes) -> NavResult | None:
    """Called from ``ai_hook.on_frame`` when ``SAW_NAV=1``. None if disabled."""
    if not nav_enabled():
        return None
    return analyze(jpeg_bytes)
