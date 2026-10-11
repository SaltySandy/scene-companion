"""Export Depth Anything V2 Metric Indoor Small to ONNX at a fixed input size.

    pip install -r scripts/requirements-export.txt
    python scripts/export_depth_onnx.py            # models/da2-metric-indoor-s-350x266.onnx

The input is portrait (height > width) because the phone is chest-mounted
upright and commit 4 sends upright frames. Both sides must be multiples of 14
(the ViT patch size). Smaller is faster: on an Intel i9 CPU, 350x266 runs in
~80 ms with ONNX Runtime; 252x196 in ~45 ms.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--height", type=int, default=350)
    p.add_argument("--width", type=int, default=266)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.height % 14 or args.width % 14:
        raise SystemExit("height and width must be multiples of 14")
    out = args.out or ROOT / "models" / f"da2-metric-indoor-s-{args.height}x{args.width}.onnx"
    out.parent.mkdir(parents=True, exist_ok=True)

    import torch
    from transformers import AutoModelForDepthEstimation

    model = AutoModelForDepthEstimation.from_pretrained(MODEL_ID).eval()
    dummy = torch.zeros(1, 3, args.height, args.width)
    torch.onnx.export(
        model,
        (dummy,),
        str(out),
        input_names=["pixel_values"],
        output_names=["predicted_depth"],
        opset_version=17,
    )
    print(f"wrote {out} ({out.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
