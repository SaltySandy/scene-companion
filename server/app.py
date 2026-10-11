"""FastAPI laptop server: WebSocket stream, live viewer, say/play endpoints."""

from __future__ import annotations

import logging
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from server import protocol as proto
from server.hub import hub, play, say

log = logging.getLogger("saw")
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    hub.ensure_worker()
    ip = lan_ip()
    port = DEFAULT_PORT
    print()
    print("=" * 60)
    print("  SAW laptop server  (protocol v1)")
    print(f"  LAN IP : {ip}")
    print(f"  Phone  : ws://{ip}:{port}/v1/stream")
    print(f"  Viewer : http://{ip}:{port}/")
    print(f"  USB    : adb reverse tcp:{port} tcp:{port}")
    print(f"           then phone host 127.0.0.1:{port}")
    print(f"  Say    : curl -X POST http://{ip}:{port}/say -H 'content-type: application/json' -d '{{\"text\":\"hello\"}}'")
    print("=" * 60)
    print()
    yield
    if hub.worker_task:
        hub.worker_task.cancel()


app = FastAPI(title="SAW Server", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "proto": proto.PROTO_VERSION,
        "phone_connected": hub.phone is not None,
        "viewers": len(hub.viewers),
        "lan_ip": lan_ip(),
    }


@app.post("/say")
async def http_say(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    urgent = False
    text = ""
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("message") or "")
            urgent = bool(body.get("urgent", False))
        else:
            text = str(body)
    else:
        text = (await request.body()).decode("utf-8", errors="replace")
        urgent = request.query_params.get("urgent", "").lower() in {"1", "true", "yes"}
    if not text.strip():
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    await say(text, urgent=urgent)
    return JSONResponse({"ok": True, "text": text.strip(), "urgent": urgent})


@app.post("/play")
async def http_play(request: Request) -> JSONResponse:
    audio = await request.body()
    if not audio:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
    mime = request.headers.get("content-type") or "audio/wav"
    urgent = request.query_params.get("urgent", "").lower() in {"1", "true", "yes"}
    await play(audio, mime=mime, urgent=urgent)
    return JSONResponse({"ok": True, "bytes": len(audio), "mime": mime, "urgent": urgent})


@app.websocket("/v1/stream")
async def phone_stream(ws: WebSocket) -> None:
    await ws.accept()
    await hub.attach_phone(ws)
    await ws.send_text(proto.encode_json(proto.MsgType.HELLO_ACK, server="saw-server", proto=proto.PROTO_VERSION))
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text") is not None:
                await _handle_phone_text(ws, message["text"])
            elif message.get("bytes") is not None:
                await _handle_phone_bytes(message["bytes"])
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("phone stream error")
    finally:
        hub.detach_phone(ws)


@app.websocket("/v1/viewer")
async def viewer_stream(ws: WebSocket) -> None:
    await ws.accept()
    hub.attach_viewer(ws)
    await ws.send_text(
        proto.encode_json(
            proto.MsgType.HELLO_ACK,
            server="saw-server",
            proto=proto.PROTO_VERSION,
            role="viewer",
            phone_connected=hub.phone is not None,
        )
    )
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            # Viewers may send say text from the page over this socket.
            if message.get("text"):
                try:
                    obj = proto.decode_json(message["text"])
                except ValueError:
                    continue
                if obj["type"] == proto.MsgType.SAY:
                    await say(str(obj.get("text") or ""), urgent=bool(obj.get("urgent", False)))
    except WebSocketDisconnect:
        pass
    finally:
        hub.detach_viewer(ws)


async def _handle_phone_text(_ws: WebSocket, text: str) -> None:
    try:
        obj = proto.decode_json(text)
    except ValueError as exc:
        log.warning("bad JSON from phone: %s", exc)
        return
    kind = obj["type"]
    if kind == proto.MsgType.PING:
        await _ws_safe_send_text(_ws, proto.encode_json(proto.MsgType.PONG, t=obj.get("t")))
    elif kind == proto.MsgType.HELLO:
        log.info("phone hello: %s", obj)
    elif kind == proto.MsgType.CONFIG:
        log.info("phone config: %s", obj)


async def _ws_safe_send_text(ws: WebSocket, text: str) -> None:
    try:
        await ws.send_text(text)
    except Exception:
        pass


async def _handle_phone_bytes(data: bytes) -> None:
    try:
        msg = proto.decode_binary(data)
    except ValueError as exc:
        log.warning("bad binary from phone: %s", exc)
        return
    recv_ms = proto.now_ms()
    if msg.kind == proto.Kind.JPEG_FRAME:
        hub.submit_frame(msg.payload, msg.timestamp_ms)
        await hub.broadcast_frame_to_viewers(msg.payload, msg.timestamp_ms, recv_ms)
    elif msg.kind == proto.Kind.MIC_PCM:
        # Mic is accepted and ignored until the AI hook wants it.
        # Viewers get a tiny stats nudge so the page can show "mic live".
        await hub._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.MIC, enabled=True, bytes=len(msg.payload), t=msg.timestamp_ms)
        )


@app.get("/lan")
async def lan() -> PlainTextResponse:
    return PlainTextResponse(lan_ip() + "\n")
