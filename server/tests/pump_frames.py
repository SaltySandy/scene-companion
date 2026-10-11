"""Send a labeled JPEG stream so the viewer page has something to show."""

from __future__ import annotations

import argparse
import asyncio
import io
import time

import websockets

from server import protocol as proto

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


def make_jpeg(n: int, w: int = 640, h: int = 480) -> bytes:
    if Image is None:
        from server.tests.fake_client import MINI_JPEG

        return MINI_JPEG
    img = Image.new("RGB", (w, h), (18, 28, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 20, w - 20, h - 20), outline=(245, 197, 66), width=6)
    draw.text((40, 40), "SAW live feed (fake phone)", fill=(245, 197, 66))
    draw.text((40, 90), f"frame {n}", fill=(232, 237, 244))
    draw.text((40, 140), time.strftime("%H:%M:%S"), fill=(147, 160, 179))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


async def pump(url: str, fps: float, count: int) -> None:
    interval = 1.0 / max(fps, 0.1)
    async with websockets.connect(url, max_size=8_000_000) as ws:
        await ws.send(proto.encode_json(proto.MsgType.HELLO, role="phone", app="pump"))
        n = 0
        while count <= 0 or n < count:
            n += 1
            jpeg = make_jpeg(n)
            await ws.send(proto.encode_binary(proto.Kind.JPEG_FRAME, jpeg, timestamp_ms=proto.now_ms()))
            await asyncio.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:8765/v1/stream")
    p.add_argument("--fps", type=float, default=6)
    p.add_argument("--count", type=int, default=0, help="0 = run forever")
    args = p.parse_args()
    asyncio.run(pump(args.url, args.fps, args.count))


if __name__ == "__main__":
    main()
