"""FastAPI laptop server: WebSocket stream, live viewer, say/play endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration

from server import protocol as proto
from server.event_sinks import sinks
from server.audio_log import audio_log
from server.hub import hub, play, say
from server.latency import book as latency_book
from server.proximity import engine as prox_engine
from server.trace import tracer

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
    hub.ensure_worker()
    sinks.start()
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
    sinks.stop()
    for task in (hub.worker_task, hub.prox_task):
        if task:
            task.cancel()


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
        "proximity": prox_engine.estimator.name if prox_engine.enabled else "off",
    }


@app.get("/latency")
async def latency() -> dict[str, object]:
    snap = latency_book.snapshot()
    snap["proximity"] = prox_engine.snapshot()
    return snap


@app.get("/audio_log")
async def http_audio_log() -> dict[str, object]:
    return audio_log.snapshot()


@app.get("/proximity_trace")
async def http_proximity_trace(request: Request) -> dict[str, object]:
    """Last N per-frame proximity rows for the current (or last) session."""
    raw = request.query_params.get("n", "80")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 80
    return tracer.snapshot(n)


@app.post("/flash")
async def http_flash(request: Request) -> JSONResponse:
    """Arm the on-demand flash detector. Optional JSON ``{"t": laptop_ms}``."""
    flash_ts = None
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("t") is not None:
            try:
                flash_ts = int(body["t"])
            except (TypeError, ValueError):
                flash_ts = None
    armed_at = latency_book.arm_flash(flash_ts)
    await hub._broadcast_viewers_text(
        proto.encode_json(proto.MsgType.FLASH, armed=True, flash_ts=armed_at)
    )
    return JSONResponse({"ok": True, "armed": True, "flash_ts": armed_at})


@app.post("/say")
async def http_say(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    urgent = False
    text = ""
    capture_ts = None
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("message") or "")
            urgent = bool(body.get("urgent", False))
            raw_ts = body.get("capture_ts")
            if raw_ts is not None:
                try:
                    capture_ts = int(raw_ts)
                except (TypeError, ValueError):
                    capture_ts = None
        else:
            text = str(body)
    else:
        text = (await request.body()).decode("utf-8", errors="replace")
        urgent = request.query_params.get("urgent", "").lower() in {"1", "true", "yes"}
    if not text.strip():
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    await say(text, urgent=urgent, capture_ts=capture_ts)
    return JSONResponse({"ok": True, "text": text.strip(), "urgent": urgent, "capture_ts": capture_ts})


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
                try:
                    if obj["type"] == proto.MsgType.SAY:
                        cap = obj.get("capture_ts")
                        try:
                            cap_i = int(cap) if cap is not None else None
                        except (TypeError, ValueError):
                            cap_i = None
                        await say(
                            str(obj.get("text") or ""),
                            urgent=bool(obj.get("urgent", False)),
                            capture_ts=cap_i,
                        )
                    elif obj["type"] == proto.MsgType.FLASH:
                        try:
                            flash_ts = int(obj["t"]) if obj.get("t") is not None else None
                        except (TypeError, ValueError):
                            flash_ts = None
                        armed_at = latency_book.arm_flash(flash_ts)
                        await ws.send_text(
                            proto.encode_json(proto.MsgType.FLASH, armed=True, flash_ts=armed_at)
                        )
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
        t0 = obj.get("t0", obj.get("t"))
        t1 = proto.now_ms()
        t2 = proto.now_ms()
        await _ws_safe_send_text(
            _ws,
            proto.encode_json(proto.MsgType.PONG, t=t0, t0=t0, t1=t1, t2=t2),
        )
    elif kind == proto.MsgType.SYNC:
        try:
            t0 = float(obj["t0"])
            t1 = float(obj["t1"])
            t2 = float(obj["t2"])
            t3 = float(obj["t3"])
        except (KeyError, TypeError, ValueError):
            log.warning("bad sync from phone: %s", obj)
            return
        result = latency_book.consider_sync(t0, t1, t2, t3)
        log.info(
            "clock sync offset=%.1fms rtt=%.1fms (sample rtt=%.1f)",
            result["offset_ms"],
            result["rtt_ms"],
            result["sample_rtt_ms"],
        )
    elif kind == proto.MsgType.TTS_START:
        try:
            capture_ts = int(obj["capture_ts"])
            tts_start_ts = int(obj["tts_start_ts"])
        except (KeyError, TypeError, ValueError):
            log.warning("bad tts_start from phone: %s", obj)
            return
        say_recv = obj.get("say_recv_ts")
        try:
            say_recv_i = int(say_recv) if say_recv is not None else None
        except (TypeError, ValueError):
            say_recv_i = None
        measured = latency_book.note_tts_start(capture_ts, tts_start_ts, say_recv_i)
        log.info("tts_start e2e=%.0fms capture_ts=%s", measured["e2e_ms"], capture_ts)
        await hub._broadcast_viewers_text(
            proto.encode_json(proto.MsgType.LATENCY, **latency_book.snapshot())
        )
    elif kind == proto.MsgType.AUDIO_EVENT:
        audio_id = str(obj.get("id") or "")
        event = str(obj.get("event") or "")
        if not audio_id or not event:
            log.warning("bad audio_event from phone: %s", obj)
            return
        vol = obj.get("volume")
        muted = obj.get("muted")
        err = obj.get("error_code")
        try:
            vol_i = int(vol) if vol is not None else None
        except (TypeError, ValueError):
            vol_i = None
        try:
            err_i = int(err) if err is not None else None
        except (TypeError, ValueError):
            err_i = None
        muted_b = None if muted is None else bool(muted)
        entry = audio_log.note_event(audio_id, event, volume=vol_i, muted=muted_b, error_code=err_i)
        if entry is not None:
            log.info("audio_event id=%s event=%s summary=%s", audio_id, event, entry.summary())
            await hub.broadcast_audio_entry(entry)
    elif kind == proto.MsgType.HELLO:
        log.info("phone hello: %s", obj)
    elif kind == proto.MsgType.CONFIG:
        log.info("phone config: %s", obj)
    elif kind == proto.MsgType.FRAME_META:
        hub.note_frame_meta(obj)


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


@app.get("/debug/sentry")
async def debug_sentry() -> JSONResponse:
    """Send one test error. Does not affect the camera stream."""
    try:
        raise RuntimeError("sightline server sentry debug")
    except RuntimeError:
        event_id = sentry_sdk.capture_exception()
        sentry_sdk.flush(timeout=2.0)
    return JSONResponse({"ok": True, "event_id": str(event_id) if event_id else None})


@app.get("/lan")
async def lan() -> PlainTextResponse:
    return PlainTextResponse(lan_ip() + "\n")
