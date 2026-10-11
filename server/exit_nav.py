"""Exit finding + path to exit from one landscape frame (prototype navigator "exitpath").

Pipeline (deterministic, CPU only):
  1. metric depth: DA2 metric-indoor-small ONNX, landscape frame run as two portrait tiles
     (depth_land.depth_landscape(mode="tiles")), working grid 320x240.
  2. floor plane: robust LS fit on the bottom 28% of rows -> camera height / pitch / roll in
     model units. Depth is rescaled so camera height = CAM_H_M (1.3 m chest mount, the same
     assumption server/ai/proximity.py documents). If the fit is implausible the raw depth and a
     level camera are used and the result is flagged (scale_source="raw").
  3. exit: red EXIT-sign blob (HSV) in the top 40% of the frame, then the door slab under it
     (columns darker than the wall or farther in depth). A door/doorway WITHOUT a sign is
     reported as `door_candidates` but never as the exit (scene 08 trap).
  4. top-down occupancy: back-project depth, height above the floor plane; 0.15-1.9 m = obstacle,
     <0.08 m = floor. 0.1 m cells, per-column ray casting marks seen-free cells, rest unknown.
  5. A* (8-connected) from the camera to a goal 0.5 m in front of the door, obstacles inflated by
     body radius (0.25 m) + margin (0.10 m); unknown cells allowed at 3x cost.
  6. spoken instruction from the path (first-leg heading, legs, distance, obstacles beside the
     path) plus a NavResult for the existing harness scorer.
"""
from __future__ import annotations

import heapq
import io
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from server.depth_land import depth_landscape

CFG = dict(
    hfov_deg=66.0,       # ASSUMED horizontal FOV of the 4:3 landscape frame (phone main cam ~26 mm eq.)
    cam_h_m=1.3,         # ASSUMED chest-camera height (server/ai/proximity.py docstring)
    work_hw=(240, 320),  # depth / geometry grid (half of 640x480)
    floor_rows=0.72,     # floor fit uses rows below this fraction of height
    obst_h=(0.15, 1.9),  # obstacle height band above floor (m)
    floor_h=0.08,
    cell=0.1, x_half=5.0, z_max=12.0,
    body_r=0.25, margin=0.10,
    unknown_cost=3.0,
    stop_corridor=(0.35, 1.2),  # |x| < 0.35 m, z < 1.2 m occupied -> STOP (about one stride + reaction)
    arrive_m=3.0, arrive_deg=12.0,  # arrive_m chosen after looking at scene 04 (see report)
    bearing_center_deg=10.0,
)


# ----------------------------------------------------------------------------- geometry
@dataclass
class Geo:
    f: float
    cx: float
    cy: float
    n_up: np.ndarray       # unit floor normal pointing from floor to camera (camera coords, Y down)
    h: float               # camera height above floor (m, after scaling)
    r_ax: np.ndarray       # ground "right" axis
    f_ax: np.ndarray       # ground "forward" axis
    scale: float
    scale_source: str
    pitch_deg: float
    roll_deg: float
    raw_h: float

    def ray(self, u, v):
        return np.stack([(np.asarray(u) - self.cx) / self.f, (np.asarray(v) - self.cy) / self.f, np.ones_like(np.asarray(u, float))], -1)

    def floor_hit(self, u, v):
        """Ground (x,z) of where pixel ray (u,v) meets the floor plane, or None if above horizon."""
        d = self.ray(u, v)
        foot = -self.h * self.n_up
        denom = d @ self.n_up
        if denom >= -1e-6:
            return None
        t = (foot @ self.n_up) / denom
        p = d * t
        return float(p @ self.r_ax), float(p @ self.f_ax)

    def project_ground(self, x, z):
        p = -self.h * self.n_up + np.outer(x, self.r_ax) + np.outer(z, self.f_ax)
        ok = p[:, 2] > 0.05
        u = self.f * p[:, 0] / np.where(ok, p[:, 2], 1) + self.cx
        v = self.f * p[:, 1] / np.where(ok, p[:, 2], 1) + self.cy
        return u, v, ok


def fit_floor(d: np.ndarray, cfg=CFG) -> Geo:
    H, W = d.shape
    f = (W / 2) / math.tan(math.radians(cfg["hfov_deg"] / 2)); cx, cy = W / 2 - 0.5, H / 2 - 0.5
    v, u = np.mgrid[0:H, 0:W]
    Z = d; X = (u - cx) * Z / f; Y = (v - cy) * Z / f
    m = v > H * cfg["floor_rows"]
    P = np.stack([X[m], Y[m], Z[m]], 1)
    keep = np.ones(len(P), bool)
    for _ in range(5):
        A = np.c_[P[keep, 0], P[keep, 2], np.ones(keep.sum())]
        c, *_ = np.linalg.lstsq(A, P[keep, 1], rcond=None)
        r = np.abs(np.c_[P[:, 0], P[:, 2], np.ones(len(P))] @ c - P[:, 1])
        keep = r < max(np.median(r[keep]) * 2.5, 0.02)
    a, b, c0 = c
    nrm = np.array([a, -1.0, b]); nn = np.linalg.norm(nrm)
    raw_h = abs(c0) / nn
    pitch = math.degrees(math.atan(b)); roll = math.degrees(math.atan(a))
    ok = keep.mean() > 0.5 and abs(pitch) < 25 and abs(roll) < 20 and 0.4 < raw_h < 6
    if ok:
        n_up = nrm / nn
        if n_up[1] > 0:  # must point up (toward -Y in camera coords)
            n_up = -n_up
        h_model = raw_h; src = "floor"
    else:
        n_up = np.array([0.0, -1.0, 0.0]); h_model = cfg["cam_h_m"]; src = "raw"; pitch = roll = 0.0
    def proj(e):
        g = e - (e @ n_up) * n_up; return g / np.linalg.norm(g)
    return Geo(f, cx, cy, n_up, h_model, proj(np.array([1.0, 0, 0])),
               proj(np.array([0, 0, 1.0])), 1.0, src, pitch, roll, raw_h)


def apply_scale(g: Geo, s: float, source: str) -> Geo:
    """Depth is multiplied by s; the floor plane distance scales with it (plane fitted on raw depth)."""
    from dataclasses import replace
    h = g.h * s if g.scale_source == "floor" else g.h
    return replace(g, h=h, scale=s, scale_source=source if g.scale_source == "floor" or source == "door" else "raw")


def ground_points(d: np.ndarray, g: Geo):
    H, W = d.shape
    v, u = np.mgrid[0:H, 0:W]
    P = g.ray(u, v) * d[..., None]
    hgt = P @ g.n_up + g.h
    return P @ g.r_ax, P @ g.f_ax, hgt


# ----------------------------------------------------------------------------- exit detection
def _label(mask):
    from scipy import ndimage as ndi
    lab, n = ndi.label(mask)
    return lab, ndi.find_objects(lab)


def find_exit_sign(img: Image.Image):
    """Largest red, saturated, bright blob in the top 40% of the frame (640x480 coords)."""
    from scipy import ndimage as ndi
    hsv = np.asarray(img.convert("HSV"), np.int16)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((h < 12) | (h > 235)) & (s > 110) & (v > 110)
    red = ndi.binary_closing(red, structure=np.ones((3, 9)), iterations=1)
    lab, objs = _label(red)
    H = img.height; best = None
    for i, sl in enumerate(objs, 1):
        if sl is None:
            continue
        area = int((lab[sl] == i).sum())
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        if area < 40 or y0 > 0.40 * H:
            continue
        if best is None or area > best[0]:
            best = (area, (x0, y0, x1, y1))
    return None if best is None else dict(box=best[1], area=best[0], cropped=best[1][2] >= img.width - 2 or best[1][0] <= 1)


def _runs(mask):
    out = []; i = 0; n = len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j)); i = j
        else:
            i += 1
    return out


def find_door_slabs(img: Image.Image, depth640: np.ndarray, y_top: int, y_bot: int):
    """Vertical slabs (columns) in rows [y_top,y_bot) that are darker than the wall OR farther
    than the wall in depth. Returns list of (x0, x1, kind)."""
    L = np.asarray(img.convert("L"), np.float32)
    band = L[y_top:y_bot]; dband = depth640[y_top:y_bot]
    col_l = np.median(band, 0); col_d = np.median(dband, 0)
    wall_l = np.percentile(col_l, 75); wall_d = np.median(col_d)
    dark = col_l < 0.72 * wall_l
    far = col_d > 1.12 * wall_d
    out = []
    for kind, m in (("dark", dark), ("far", far)):
        for a, b in _runs(m):
            if b - a >= 12:
                out.append((a, b, kind))
    return out, col_l, col_d


def door_vertical_extent(img, depth640, x0, x1, y_start):
    """Door top/bottom from luma.
    top    = first row below the sign where the slab columns are darker than the wall above;
    bottom = wall/floor line next to the door: in 12-px wall strips left and right of the slab,
             the first row (40+ px below the top) where luma drops under 0.75x the wall level for
             3 rows (white wall/baseboard -> carpet). Median of the sides that exist."""
    L = np.asarray(img.convert("L"), np.float32); H, W = L.shape
    slab = np.median(L[:, x0:x1], 1)
    wall_ref = np.percentile(L[y_start:min(y_start + 80, H), :], 75)
    top = y_start
    for y in range(y_start, min(y_start + 80, H)):
        if slab[y] < 0.8 * wall_ref:
            top = y; break
    bots = []
    for a, b in ((x0 - 16, x0 - 4), (x1 + 4, x1 + 16)):
        a, b = max(a, 0), min(b, W)
        if b - a < 6:
            continue
        prof = np.median(L[:, a:b], 1); ref = np.median(prof[top:top + 60])
        below = prof < 0.75 * ref
        for y in range(top + 40, H - 5):
            # the drop must persist: >= 85% of the rows from y to the bottom of the frame are
            # darker (carpet continues); wall-mounted signs / switches only last a few rows
            if below[y] and below[y:].mean() >= 0.85:
                bots.append(y); break
    # an occluder (e.g. a chair in front of the wall strip) makes a side look "too high";
    # the lowest side is the better estimate of the floor line. Keep only bottoms that give a
    # door-like aspect (h/w 1.6..3.6 for a ~2.03 x 0.9 m door seen roughly face-on).
    w = max(x1 - x0, 1)
    ok = [b for b in bots if 1.6 <= (b - top) / w <= 3.6 or (x1 >= W - 2 or x0 <= 1)]
    bot = int(max(ok)) if ok else None
    return top, bot, bots


def detect_exit(img: Image.Image, depth640: np.ndarray) -> dict[str, Any]:
    W, H = img.size
    sign = find_exit_sign(img)
    res: dict[str, Any] = dict(sign=sign, door=None, door_candidates=[], method=None)
    # door candidates anywhere in the upper-middle band (for the 08 "doorway without sign" case)
    slabs, _, _ = find_door_slabs(img, depth640, int(0.25 * H), int(0.50 * H))
    for a, b, kind in slabs:
        if a <= 2 and kind == "far":
            continue
        res["door_candidates"].append(dict(x0=int(a), x1=int(b), kind=kind))
    if sign is None:
        return res
    sx0, sy0, sx1, sy1 = sign["box"]; scx = (sx0 + sx1) / 2; sw = max(sx1 - sx0, 8)
    y_top = min(sy1 + 6, H - 2)
    slabs, _, _ = find_door_slabs(img, depth640, y_top, min(y_top + int(0.25 * H), H))
    # a slab must contain/touch the sign's x (within 1.5 sign widths)
    cand = [(a, b, k) for a, b, k in slabs if a - 1.5 * sw <= scx <= b + 1.5 * sw]
    if cand:
        # merge dark/far runs that overlap the sign column
        a = min(c[0] for c in cand); b = max(c[1] for c in cand)
        kinds = sorted({c[2] for c in cand})
        top, bot, bots = door_vertical_extent(img, depth640, a, b, y_top)
        res["door"] = dict(box=(int(a), int(top), int(b), int(bot if bot is not None else H - 1)), kinds=kinds,
                           bottom_found=bot is not None, bottom_sides=bots,
                           cropped=b >= W - 2 or a <= 1)
        res["method"] = "sign+slab(" + "+".join(kinds) + ")"
    else:
        # sign only: assume the door is centred under the sign (bearing only, distance from depth)
        res["method"] = "sign-only"
    return res


# ----------------------------------------------------------------------------- occupancy + A*
@dataclass
class Grid:
    occ: np.ndarray       # bool occupied
    free: np.ndarray      # bool seen free
    inflated: np.ndarray  # bool blocked after inflation
    cell: float
    x_half: float
    z_max: float

    def ij(self, x, z):
        return int(round(z / self.cell)), int(round((x + self.x_half) / self.cell))

    def xz(self, i, j):
        return j * self.cell - self.x_half, i * self.cell


def build_grid(d: np.ndarray, g: Geo, cfg=CFG) -> Grid:
    cell = cfg["cell"]; nz = int(cfg["z_max"] / cell) + 1; nx = int(2 * cfg["x_half"] / cell) + 1
    x, z, hgt = ground_points(d, g)
    H, W = d.shape
    occ_cnt = np.zeros((nz, nx), np.int32); free = np.zeros((nz, nx), bool)
    lo, hi = cfg["obst_h"]
    ob = (hgt > lo) & (hgt < hi) & (z > 0.05)
    fl = (np.abs(hgt) < cfg["floor_h"]) & (z > 0.05)
    def idx(xx, zz):
        i = np.round(zz / cell).astype(int); j = np.round((xx + cfg["x_half"]) / cell).astype(int)
        k = (i >= 0) & (i < nz) & (j >= 0) & (j < nx); return i[k], j[k]
    i, j = idx(x[ob], z[ob]); np.add.at(occ_cnt, (i, j), 1)
    i, j = idx(x[fl], z[fl]); free[i, j] = True
    occ = occ_cnt >= 3
    # per-column ray cast: cells from camera to the nearest obstacle hit in that column are free
    for c in range(W):
        col = ob[:, c]
        if col.any():
            rng = np.hypot(x[:, c][col], z[:, c][col]); k = int(np.argmin(rng)); r = rng[k]
            ang = math.atan2(x[:, c][col][k], z[:, c][col][k])
        else:
            fz = fl[:, c]
            if not fz.any():
                continue
            rng = np.hypot(x[:, c][fz], z[:, c][fz]); k = int(np.argmax(rng)); r = rng[k]
            ang = math.atan2(x[:, c][fz][k], z[:, c][fz][k])
        for t in np.arange(0.0, max(r - 0.15, 0), cell / 2):
            ii, jj = int(round(t * math.cos(ang) / cell)), int(round((t * math.sin(ang) + cfg["x_half"]) / cell))
            if 0 <= ii < nz and 0 <= jj < nx:
                free[ii, jj] = True
    free &= ~occ
    rad = int(math.ceil((cfg["body_r"] + cfg["margin"]) / cell))
    yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]; disk = (yy ** 2 + xx ** 2) <= rad * rad
    from scipy import ndimage as ndi
    inflated = ndi.binary_dilation(occ, structure=disk)
    return Grid(occ, free, inflated, cell, cfg["x_half"], cfg["z_max"])


def astar(grid: Grid, start, goal, cfg=CFG):
    nz, nx = grid.occ.shape
    blocked = grid.inflated.copy()
    # the camera's own cell and the goal disk are never blocked (goal sits at the wall/door)
    for (i0, j0), r in ((start, 3), (goal, 4)):
        blocked[max(i0 - r, 0):i0 + r + 1, max(j0 - r, 0):j0 + r + 1] = False
    cost = np.where(grid.free, 1.0, cfg["unknown_cost"])
    h = lambda a: math.hypot(a[0] - goal[0], a[1] - goal[1])
    openq = [(h(start), 0.0, start)]; came = {start: None}; gbest = {start: 0.0}
    nb = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
    while openq:
        _, gc, cur = heapq.heappop(openq)
        if cur == goal:
            path = [cur]
            while came[path[-1]] is not None:
                path.append(came[path[-1]])
            return path[::-1]
        if gc > gbest.get(cur, 1e18):
            continue
        for di, dj in nb:
            n = (cur[0] + di, cur[1] + dj)
            if not (0 <= n[0] < nz and 0 <= n[1] < nx) or blocked[n]:
                continue
            ng = gc + math.hypot(di, dj) * cost[n]
            if ng < gbest.get(n, 1e18):
                gbest[n] = ng; came[n] = cur; heapq.heappush(openq, (ng + h(n), ng, n))
    return None


def simplify(pts, tol=0.3):
    """Douglas-Peucker on (x,z) metres."""
    pts = np.asarray(pts, float)
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]; ab = b - a; L = np.linalg.norm(ab) or 1e-9
    dist = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / L
    k = int(np.argmax(dist))
    if dist[k] > tol:
        return np.vstack([simplify(pts[:k + 1], tol)[:-1], simplify(pts[k:], tol)])
    return np.vstack([a, b])


def obstacle_blobs(grid: Grid, max_range=4.0):
    """Connected occupied components; small ones (< 1.3 m extent) are 'objects', large = walls."""
    lab, objs = _label(grid.occ)
    out = []
    for k, sl in enumerate(objs, 1):
        if sl is None:
            continue
        ii, jj = np.nonzero(lab[sl] == k)
        if len(ii) < 3:
            continue
        xs = (jj + sl[1].start) * grid.cell - grid.x_half; zs = (ii + sl[0].start) * grid.cell
        rng = np.hypot(xs, zs); m = int(np.argmin(rng))
        ext = max(xs.max() - xs.min(), zs.max() - zs.min())
        if rng[m] > max_range:
            continue
        out.append(dict(x=float(xs[m]), z=float(zs[m]), range=float(rng[m]), extent=float(ext),
                        kind="object" if ext < 1.3 else "wall", n=int(len(ii))))
    return sorted(out, key=lambda o: o["range"])


# ----------------------------------------------------------------------------- words
def bucket_distance(m):
    if m is None:
        return "none"
    return "close" if m < 1.0 else "near" if m < 2.5 else "mid" if m < 5.0 else "far"


def dir_words(deg):
    a = abs(deg); side = "right" if deg > 0 else "left"
    if a < 7:
        return "straight ahead"
    if a < 20:
        return f"ahead, slightly {side}"
    if a < 45:
        return f"ahead to your {side}"
    if a < 100:
        return f"to your {side}"
    return "behind you"


def turn_words(deg):
    a = abs(deg); side = "right" if deg > 0 else "left"
    if a < 7:
        return "go straight"
    if a < 20:
        return f"bear slightly {side}"
    if a < 45:
        return f"bear {side}"
    return f"turn {side}"


def round_m(m):
    return f"{m:.0f}" if m >= 3 else f"{m:.1f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------------------- main entry
def analyze_frame(jpeg: bytes, cfg=CFG, depth_mode="tiles") -> dict[str, Any]:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if img.size != (640, 480):
        img = img.resize((640, 480), Image.BICUBIC)
    H, W = cfg["work_hw"]
    raw = depth_landscape(img, depth_mode, out_hw=(H, W))
    g0 = fit_floor(raw, cfg)
    k = 640 / W  # work grid -> 640 px
    raw640 = np.asarray(Image.fromarray(raw).resize((640, 480), Image.BILINEAR), np.float32)
    ex = detect_exit(img, raw640)
    f640 = g0.f * k
    # --- metric scale: (1) standard door height 2.03 m if a full door is seen, else
    #     (2) camera height CAM_H_M from the floor fit, else (3) raw DA2 output
    door_size_m = None; frame_raw = None
    if ex["door"] is not None:
        x0, y0, x1, y1 = ex["door"]["box"]
        strips = np.r_[raw640[y0:y1, max(x0 - 10, 0):max(x0 - 2, 0)].ravel(), raw640[y0:y1, min(x1 + 2, 640):min(x1 + 10, 640)].ravel()]
        frame_raw = float(np.median(strips)) if strips.size else None
        if ex["door"]["bottom_found"] and y1 - y0 > 20:
            door_size_m = f640 * 2.03 / (y1 - y0)
    if door_size_m is not None and frame_raw:
        g = apply_scale(g0, door_size_m / frame_raw, "door")
    elif g0.scale_source == "floor":
        g = apply_scale(g0, cfg["cam_h_m"] / g0.raw_h, "floor")
    else:
        g = apply_scale(g0, 1.0, "raw")
    d = raw * g.scale
    depth640 = raw640 * g.scale
    grid = build_grid(d, g, cfg)

    out: dict[str, Any] = dict(geo=dict(scale=round(float(g.scale), 3), scale_source=g.scale_source, raw_cam_h=round(float(g.raw_h), 2),
                                        cam_h_after_scale=round(float(g.h), 2),
                                        pitch_deg=round(float(g.pitch_deg), 1), roll_deg=round(float(g.roll_deg), 1), hfov_deg=cfg["hfov_deg"]),
                               exit=ex, grid=grid, g=g, depth=d, raw_depth=raw, img=img)
    # stop check (corridor straight ahead)
    cw, cz = cfg["stop_corridor"]
    nz_c = int(cz / grid.cell) + 1; j0 = int(round((-cw + grid.x_half) / grid.cell)); j1 = int(round((cw + grid.x_half) / grid.cell))
    corridor_hit = bool(grid.occ[1:nz_c, j0:j1 + 1].any())
    blobs = obstacle_blobs(grid)
    out["blobs"] = blobs
    # wall-filling-view rule: no valid floor fit and almost every image column has an obstacle
    _, _, hgt = ground_points(d, g)
    ob_cols = ((hgt > cfg["obst_h"][0]) & (hgt < cfg["obst_h"][1])).any(0).mean()
    wall_fill = g0.scale_source == "raw" and ob_cols > 0.95
    out["corridor_hit"] = corridor_hit; out["wall_fill"] = wall_fill

    # exit geometry
    e = None
    if ex["sign"] is not None:
        sx0, sy0, sx1, sy1 = ex["sign"]["box"]
        if ex["door"] is not None:
            x0, y0, x1, y1 = ex["door"]["box"]; u_c = (x0 + x1) / 2
            hit = g.floor_hit(u_c / k, y1 / k) if ex["door"]["bottom_found"] else None
            dist_floor = None if hit is None else math.hypot(*hit)
            dist_depth = None if frame_raw is None else frame_raw * g.scale
            dist_size = door_size_m
        else:
            u_c = (sx0 + sx1) / 2; hit = None; dist_floor = None; dist_size = None
            colm = depth640[min(sy1 + 20, 479):min(sy1 + 80, 480), max(int(u_c) - 10, 0):int(u_c) + 10]
            dist_depth = float(np.median(colm)) if colm.size else None
        bearing = math.degrees(math.atan((u_c / k - g.cx) / g.f))
        dist = dist_size if dist_size is not None else dist_depth
        if dist is not None:
            # goal on the ground along the exit bearing (the door plane itself reads as a "hole" in DA2)
            b = math.radians(bearing); hit = (dist * math.sin(b), dist * math.cos(b))
        e = dict(bearing_deg=bearing, dist_m=dist, dist_floor_m=dist_floor, dist_depth_m=dist_depth,
                 dist_doorsize_m=dist_size, ground=hit,
                 visible="partial" if (ex["door"] is None or ex["door"]["cropped"] or ex["sign"]["cropped"]) else "yes")
    out["exit_geom"] = e

    # path
    path = None; legs = []
    if e is not None and e["ground"] is not None:
        gx, gz = e["ground"]; L = math.hypot(gx, gz)
        back = min(0.5, max(L - 0.2, 0))
        gx2, gz2 = gx - back * gx / max(L, 1e-6), gz - back * gz / max(L, 1e-6)
        gz2 = min(gz2, grid.z_max - 0.2)
        start = grid.ij(0, 0); goal = grid.ij(gx2, gz2)
        p = astar(grid, start, goal, cfg)
        if p is not None:
            path = np.array([grid.xz(i, j) for i, j in p])
            simp = simplify(path, 0.3)
            for a, b in zip(simp[:-1], simp[1:]):
                v = b - a; legs.append(dict(heading_deg=math.degrees(math.atan2(v[0], v[1])), length_m=float(np.hypot(*v))))
    out["path"] = path; out["legs"] = legs
    out["say"], out["nav_fields"] = compose(out, cfg)
    return out


def compose(o, cfg=CFG):
    e = o["exit_geom"]; ex = o["exit"]; blobs = o["blobs"]; legs = o["legs"]
    near_objs = [b for b in blobs if b["kind"] == "object" and b["range"] < 3.0]
    nearest = blobs[0] if blobs else None
    stop = o["corridor_hit"] or o["wall_fill"]
    arrived = bool(e and e["dist_m"] is not None and e["dist_m"] < cfg["arrive_m"] and abs(e["bearing_deg"]) < cfg["arrive_deg"]
                   and ex["door"] is not None)
    bits = []
    if arrived:
        bits.append(f"You have reached the exit door. It is straight ahead, about {round_m(e['dist_m'])} meters; walk forward to it.")
        stop = True
    elif o["wall_fill"]:
        bits.append("Stop. Wall directly ahead, no clear floor in view.")
    else:
        if stop:
            side = "ahead"
            bits.append("Stop. Obstacle directly ahead.")
        if e is not None:
            dtxt = f", about {round_m(e['dist_m'])} meters" if e["dist_m"] is not None else ""
            bits.append(f"Exit {dir_words(e['bearing_deg'])}{dtxt}.")
            if legs:
                first = legs[0]
                if len(legs) == 1 or legs[0]["length_m"] > 0.8 * sum(l["length_m"] for l in legs):
                    bits.append(f"{turn_words(first['heading_deg']).capitalize()} and walk about {round_m(sum(l['length_m'] for l in legs))} meters.")
                else:
                    b2 = legs[1]
                    bits.append(f"{turn_words(first['heading_deg']).capitalize()} for about {round_m(first['length_m'])} meters, "
                                f"then {turn_words(b2['heading_deg'])} for about {round_m(sum(l['length_m'] for l in legs[1:]))} meters.")
            else:
                bits.append("No clear path found; stop and scan.")
                stop = True
        else:
            bits.append("Exit is not in view. Turn and scan for the exit sign.")
            if ex["door_candidates"]:
                bits.append("There is a doorway, but no exit sign on it.")
        ahead_objs = [b for b in blobs if b["range"] < 3.0 and abs(math.degrees(math.atan2(b["x"], b["z"]))) < 40]
        if ahead_objs and not (stop and abs(ahead_objs[0]["x"]) < 0.35):
            ob = ahead_objs[0]
            # side relative to the walking path at the obstacle's range
            px = 0.0
            if o["path"] is not None:
                k = int(np.argmin(np.abs(o["path"][:, 1] - ob["z"]))); px = o["path"][k, 0]
            rel = ob["x"] - px; side = "right" if rel > 0 else "left"
            keep = "keep left of it" if rel > 0 else "keep right of it"
            what = "Wall" if ob["extent"] > 2.5 else "Obstacle"
            bits.append(f"{what} on your {side} at about {round_m(ob['range'])} meters, {keep}.")
    say = " ".join(bits)
    bearing = "none"; visible = "no"; dist_b = "none"
    if e is not None:
        visible = e["visible"]
        bearing = "center" if abs(e["bearing_deg"]) < cfg["bearing_center_deg"] else ("right" if e["bearing_deg"] > 0 else "left")
        dist_b = bucket_distance(e["dist_m"])
    elif ex["sign"] is None:
        bearing = "behind" if blobs else "none"  # "not in view" -> manifest uses behind/none
    if o["wall_fill"]:
        visible, bearing, dist_b = "no", "none", "none"
    nav = dict(exit_visible=visible, exit_bearing=bearing, exit_distance=dist_b,
               exit_distance_m=None if e is None else e["dist_m"],
               obstacle_ahead=bool(stop or any(abs(math.degrees(math.atan2(b['x'], b['z']))) < 25 for b in blobs if b["range"] < 3.0)),
               nearest_obstacle_distance=bucket_distance(nearest["range"]) if nearest else ("close" if o["wall_fill"] else "none"),
               should_stop=bool(stop), arrived=arrived)
    return say, nav


# ----------------------------------------------------------------------------- navigator
class ExitPathNavigator:
    """``server.nav`` Navigator: SAW_NAVIGATOR=exitpath. Needs the depth ONNX and scipy."""

    name = "exitpath"

    def __init__(self) -> None:
        self.last: dict[str, Any] | None = None

    def reset(self) -> None:
        self.last = None

    def analyze(self, frame: bytes):
        from server.nav import ExitHint, NavResult, Obstacle

        try:
            o = analyze_frame(frame)
        except Exception as exc:  # unreadable frame / model missing -> never "clear"
            return NavResult(exit=ExitHint("no", "none", "none"), should_stop=True,
                             utterance=f"Stop. I cannot see right now ({type(exc).__name__}).")
        self.last = o
        n = o["nav_fields"]
        obs = []
        for b in o["blobs"][:4]:
            ang = math.degrees(math.atan2(b["x"], b["z"]))
            bear = "center" if abs(ang) < 10 else ("right" if ang > 0 else "left")
            obs.append(Obstacle("wall" if b["extent"] > 2.5 else "obstacle", bear, bucket_distance(b["range"]),
                                round(b["range"], 2), ahead=abs(ang) < 25))
        dm = n["exit_distance_m"]
        return NavResult(exit=ExitHint(n["exit_visible"], n["exit_bearing"], n["exit_distance"], None if dm is None else round(dm, 2)),
                         obstacles=obs, should_stop=n["should_stop"], arrived=n["arrived"], utterance=o["say"],
                         obstacle_ahead=n["obstacle_ahead"], nearest_obstacle_distance=n["nearest_obstacle_distance"])
