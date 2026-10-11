#!/usr/bin/env python3
"""Download the ONNX weights the exit navigator needs.

YOLOv8n (~6 MB) detects chairs and other COCO obstacles. The EXIT sign is a
color heuristic, so it needs no extra file. Depth (Brian) is still
``scripts/export_depth_onnx.py`` → ``models/da2-metric-indoor-s-350x266.onnx``.

Windows PowerShell (one command, from the repo root)::

    pip install -q ultralytics; python -c "from pathlib import Path; from ultralytics import YOLO; Path('models').mkdir(exist_ok=True); Path(YOLO('yolov8n.pt').export(format='onnx', imgsz=320)).replace(Path('models/yolov8n.onnx'))"

Ultralytics downloads ``yolov8n.pt`` (~6.5 MB) from GitHub releases and writes
``models/yolov8n.onnx``. Same thing via this script::

    python scripts/fetch_nav_models.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models" / "yolov8n.onnx"

URLS = (
    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx",
    "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx",
)

PT_URLS = (
    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt",
    "https://github.com/ultralytics/assets/releases/download/v8.2.1/yolov8n.pt",
)


def _download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".onnx.part")
    print(f"GET {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "scene-companion"})
        with urllib.request.urlopen(req, timeout=60) as src, tmp.open("wb") as out:
            while True:
                chunk = src.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
        size = tmp.stat().st_size
        if size < 1_000_000:
            tmp.unlink(missing_ok=True)
            print(f"  too small ({size} B), skip")
            return False
        tmp.replace(dest)
        print(f"  wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return True
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"  failed: {exc}")
        return False


def _export_ultralytics(dest: Path) -> bool:
    try:
        from ultralytics import YOLO
    except ImportError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    weights = ROOT / "models" / "yolov8n.pt"
    src = str(weights if weights.is_file() else "yolov8n.pt")
    print(f"exporting {src} → ONNX via ultralytics")
    model = YOLO(src)
    produced = Path(model.export(format="onnx", imgsz=320, simplify=True))
    if produced.is_file():
        produced.replace(dest)
        print(f"  wrote {dest}")
        return True
    return False


def main() -> int:
    if OUT.is_file() and OUT.stat().st_size > 1_000_000:
        print(f"already have {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
        return 0
    for url in URLS:
        if _download(url, OUT):
            return 0
    pt = ROOT / "models" / "yolov8n.pt"
    if not pt.is_file():
        for url in PT_URLS:
            if _download(url, pt):
                break
    if _export_ultralytics(OUT):
        return 0
    print("could not fetch yolov8n.onnx; navigator will use the color fallback", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
