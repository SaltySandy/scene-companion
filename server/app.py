"""FastAPI laptop server: WebSocket stream, live viewer, say/play endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Load GEMINI_API_KEY etc. from a .env file in the repo root, if present.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import sentry_sdk
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration

from server import protocol as proto
from server.ai_hook import on_transcript
from server.event_sinks import sinks
from server import ai_hook
from server.hub import hub, play, say
from server.transcribe import transcriber
from server.scene_assistant import assistant
from server.wall_detector import detector as wall_detector

log = logging.getLogger("saw")
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
DEFAULT_SENTRY_DSN = (
    "https://0954950fd2aaf958b6b3eaf7d322f856@o4512238207893504.ingest.us.sentry.io/4512238400503809"
)


def _drop_images(event, hint):
    """Never forward image bytes. Request bodies are already disabled."""
    del hint
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    if isinstance(headers, dict):
        ctype = ""
        for key, value in headers.items():
            if str(key).lower() == "content-type":
                ctype = str(value).lower()
                break
        if ctype.startswith("image/"):
            request.pop("data", None)
            event["request"] = request
    return event


def init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", DEFAULT_SENTRY_DSN).strip()
    if not dsn:
        log.info("Sentry disabled (empty SENTRY_DSN)")
        return
    sentry_sdk.init(
        dsn=dsn,
        environment="hackathon",
        release="0.1.0",
        send_default_pii=False,
        max_request_body_size="never",
        integrations=[FastApiIntegration(), AsyncioIntegration()],
        before_send=_drop_images,
    )


init_sentry()


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
    depth_model = await asyncio.to_thread(ai_hook.warmup)
    hub.ensure_worker()
    sinks.start()
    transcriber.bind(asyncio.get_running_loop(), _emit_transcript)
    transcriber.start()
    hub.recorder.start()
    wall_detector.start(speak=say, status=_emit_wall_status)
    assistant.bind(speak=say, frame=_latest_frame_for_assistant, status=_emit_assistant_status)
    ip = lan_ip()
    port = DEFAULT_PORT
    print()
    print("=" * 60)
    print("  SAW laptop server  (protocol v1)")
    print(f"  LAN IP : {ip}")
    print(f"  Phone  : ws://{ip}:{port}/v1/stream")
    print(f"  Phone web: http://{ip}:{port}/phone")
    print(f"  Viewer : http://{ip}:{port}/")
    print(f"  USB    : adb reverse tcp:{port} tcp:{port}")
    print(f"           then phone host 127.0.0.1:{port}")
    if wall_detector.enabled:
        print(f"  Walls  : Gemini Live backup detector ON ({wall_detector.model}). Depth model is primary.")
    if assistant.enabled:
        print(f"  Ask    : Gemini ({os.environ.get('SAW_GEMINI_VISION_MODEL', 'gemini-3.5-flash-lite')})"
              f" + Nimble web search {'ON' if assistant.nimble_enabled else 'OFF (set NIMBLE_API_KEY)'}")
        print(f"  Voice  : speech-to-text via {transcriber.backend}")
    print(f"  Say    : curl -X POST http://{ip}:{port}/say -H 'content-type: application/json' -d '{{\"text\":\"hello\"}}'")
    if hub.recorder.enabled:
        print(f"  Record : {hub.recorder.path}")
    if ai_hook.PROXIMITY_ON:
        print(f"  Depth  : {depth_model or 'MISSING - phone will hear sensor lost (scripts/export_depth_onnx.py)'}")
    print("=" * 60)
    print()
    yield
    await wall_detector.stop()
    transcriber.stop()
    sinks.stop()
    tasks = [t for t in (hub.worker_task, hub.clock_task, hub.fov_task) if t]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    hub.recorder.stop()  # after the worker, so its last record is flushed


app = FastAPI(title="SAW Server", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/phone", response_class=HTMLResponse)
async def phone() -> HTMLResponse:
    """Mobile web client (iPhone Safari / Android Chrome). Additive; not the laptop viewer."""
    return HTMLResponse((STATIC_DIR / "phone" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "proto": proto.PROTO_VERSION,
        "phone_connected": hub.phone is not None,
        "viewers": len(hub.viewers),
        "frames_dropped": hub.frames_dropped,
        "depth_model": ai_hook.warmup() if ai_hook.PROXIMITY_ON else None,
        "recording": hub.recorder.session_id if hub.recorder.enabled else None,
        "records_dropped": hub.recorder.records_dropped,
        "clock_offset_ms": hub.clock.offset_ms,
        "clock_rtt_ms": hub.clock.rtt_ms,
        "last_cue_latency_ms": hub.last_cue_latency_ms,
        "lan_ip": lan_ip(),
        "latest_jpeg_bytes": len(hub.last_jpeg) if hub.last_jpeg else 0,
        "fov": ai_hook.fov_params(),
        "assistant": {"enabled": assistant.enabled, "nimble": assistant.nimble_enabled},
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


@app.post("/ask")
async def http_ask(request: Request) -> JSONResponse:
    """Ask about the current camera frame, same as speaking a question."""
    question = ""
    if "application/json" in request.headers.get("content-type", ""):
        body = await request.json()
        if isinstance(body, dict):
            question = str(body.get("question") or body.get("text") or "")
    else:
        question = (await request.body()).decode("utf-8", errors="replace")
    question = question.strip() or "What is this?"
    if not assistant.enabled:
        return JSONResponse({"ok": False, "error": "assistant off (set GEMINI_API_KEY)"}, status_code=400)
    assistant.ask(question)
    return JSONResponse({"ok": True, "question": question, "nimble": assistant.nimble_enabled})


@app.post("/recall")
async def http_recall(request: Request) -> JSONResponse:
    body: dict = {}
    if "application/json" in request.headers.get("content-type", ""):
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    question = str(body.get("question") or body.get("text") or "").strip()
    backend = body.get("backend")
    if isinstance(backend, str):
        backend = backend.strip().lower() or None
    else:
        backend = None
    if not question:
        return JSONResponse({"ok": False, "error": "empty question"}, status_code=400)
    if backend not in (None, "snowflake", "tiger"):
        return JSONResponse({"ok": False, "error": "backend must be snowflake or tiger"}, status_code=400)
    try:
        text, used = await asyncio.to_thread(sinks.recall, question, backend)
    except Exception as exc:
        log.warning("recall failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc), "backend": backend})
    if text:
        await say(text, urgent=False)
    return JSONResponse({"ok": True, "text": text, "backend": used})


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
            try:
                if message.get("text") is not None:
                    await _handle_phone_text(ws, message["text"])
                elif message.get("bytes") is not None:
                    await _handle_phone_bytes(message["bytes"])
            except Exception:
                log.exception("phone message failed")
                sentry_sdk.capture_exception()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("phone stream error")
        sentry_sdk.capture_exception()
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
            fov=ai_hook.fov_params(),
        )
    )
    await hub.send_snapshot_to_viewer(ws)
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
                try:
                    if obj["type"] == proto.MsgType.SAY:
                        await say(str(obj.get("text") or ""), urgent=bool(obj.get("urgent", False)))
                except Exception:
                    log.exception("viewer message failed")
                    sentry_sdk.capture_exception()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("viewer stream error")
        sentry_sdk.capture_exception()
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
    elif kind == proto.MsgType.PONG:
        # Answer to our clock ping: t0 = our send time, tp = phone clock.
        t0, tp = obj.get("t0"), obj.get("tp")
        if isinstance(t0, int) and isinstance(tp, int):
            hub.clock.add(t0, tp, proto.now_ms())
    elif kind == proto.MsgType.ACK:
        key, cue_ts = obj.get("frame_key"), obj.get("cue_start_ts")
        if isinstance(key, int) and isinstance(cue_ts, int):
            hub.on_ack(key, cue_ts, str(obj.get("state") or ""))
    elif kind == proto.MsgType.HELLO:
        log.info("phone hello: %s", obj)
    elif kind == proto.MsgType.CONFIG:
        hub.phone_config = {k: obj[k] for k in obj if k not in {"v", "type"}}
        log.info("phone config: %s", obj)
    elif kind == proto.MsgType.SPEAKING:
        transcriber.set_phone_speaking(bool(obj.get("active", False)))


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
        hub.submit_frame(msg.payload, msg.timestamp_ms, recv_ms)
        wall_detector.submit(msg.payload, msg.timestamp_ms)
        await hub.broadcast_frame_to_viewers(msg.payload, msg.timestamp_ms, recv_ms)
    elif msg.kind == proto.Kind.MIC_PCM:
        # Enqueue only. Whisper runs on its own thread.
        transcriber.push_pcm(msg.payload, msg.timestamp_ms)
        await hub._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.MIC, enabled=True, bytes=len(msg.payload), t=msg.timestamp_ms)
        )


def _latest_frame_for_assistant() -> tuple[bytes, int] | None:
    """Newest phone JPEG for "What is this?". Reads hub state on the event loop only."""
    if hub.last_jpeg is None:
        return None
    return hub.last_jpeg, hub.last_jpeg_ts or 0


async def _emit_assistant_status(status: dict) -> None:
    await hub._broadcast_viewers_text(proto.encode_json(proto.MsgType.ASSISTANT, **status))


async def _emit_wall_status(status: dict) -> None:
    await hub._broadcast_viewers_text(proto.encode_json(proto.MsgType.WALL, **status))


async def _emit_transcript(text: str, is_final: bool, ts: int) -> None:
    payload = proto.encode_json(proto.MsgType.TRANSCRIPT, text=text, final=is_final, ts=ts)
    await hub._broadcast_viewers_text(payload)
    await hub._send_phone_text(payload)
    await on_transcript(text, is_final, ts)


@app.post("/debug/proximity")
async def debug_proximity(request: Request) -> JSONResponse:
    """Send one proximity message so phone cues can be tested without the model.

    Body: {"level": "near"} or {"state": "unknown"}, optionally "levels"
    {left, center, right} and "sectors_m". Real heartbeats keep flowing if the
    model is running, so test with the depth model off (SAW_PROXIMITY=0).
    """
    body = await request.json() if "application/json" in request.headers.get("content-type", "") else {}
    if not isinstance(body, dict):
        body = {}
    state = str(body.get("state") or request.query_params.get("state") or proto.PROXIMITY_OK)
    level = body.get("level") or request.query_params.get("level")
    levels = body.get("levels")
    if state == proto.PROXIMITY_OK and levels is None and level in proto.LEVELS:
        levels = {s: level for s in proto.SECTORS}
    try:
        await hub.send_proximity(state, level, levels, body.get("sectors_m"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "seq": hub.proximity_seq, "state": state, "level": level})


@app.get("/debug/sentry")
async def debug_sentry() -> JSONResponse:
    """Send one test error. Does not affect the camera stream."""
    try:
        raise RuntimeError("sightline server sentry debug")
    except RuntimeError:
        event_id = sentry_sdk.capture_exception()
        sentry_sdk.flush(timeout=2.0)
    return JSONResponse({"ok": True, "event_id": str(event_id) if event_id else None})


@app.get("/latest.jpg")
async def latest_jpeg() -> Response:
    """Last phone JPEG. Lets the viewer page show a frame even if the WS never opens."""
    if not hub.last_jpeg:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        content=hub.last_jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Capture-Ts": str(hub.last_jpeg_ts or "")},
    )


@app.get("/fov.jpg")
async def fov_jpeg() -> Response:
    if not hub.last_fov_jpeg:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        content=hub.last_fov_jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Fov-Seq": str(hub.fov_seq)},
    )


@app.get("/fov.json")
async def fov_json() -> JSONResponse:
    from server.ai_hook import fov_params

    meta = dict(hub.last_fov_meta or fov_params())
    meta["seq"] = hub.fov_seq
    meta["has_jpeg"] = hub.last_fov_jpeg is not None
    if hub.phone_config:
        meta["phone"] = hub.phone_config
    return JSONResponse(meta, headers={"Cache-Control": "no-store"})


@app.get("/lan")
async def lan() -> PlainTextResponse:
    return PlainTextResponse(lan_ip() + "\n")
