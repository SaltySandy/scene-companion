# SAW skeleton — report

See the README for the 5-minute quickstart and protocol. This page is the tight verification report.

## Architecture

```
Phone (Kotlin / CameraX)  --SAW v1 WebSocket-->  Laptop (FastAPI)
        ^                                              |
        |   say / play audio                           |
        +------------------ same socket ---------------+
                                                       |
                                              ai_hook.on_frame
                                              (latest JPEG only)
                                                       |
                                              viewer page / curl /say
```

- **Phone:** foreground `StreamService` owns CameraX (`STRATEGY_KEEP_ONLY_LATEST`), OkHttp WebSocket (drop if `queueSize() > 0`), TTS + MediaPlayer/AudioTrack, optional 16 kHz mic. UI is one huge Start/Stop plus persisted `host:port`.
- **Laptop:** `server/app.py` accepts `/v1/stream` and `/v1/viewer`. `server/hub.py` keeps one phone socket, broadcasts JPEGs to the viewer, and exposes `say()` / `play()`. `server/ai_hook.py` is the only place the model should live.

## Protocol

SAW **v1**: binary envelope `SAW1 | kind | flags | reserved | ts_ms | len | payload` for JPEG / mic PCM / audio clips; JSON `{"v":1,"type":...}` for hello, say, config, ping/pong. Documented in the README.

## What was verified live vs not

Filled in after the VM build and fake-client run (see the end of this file after CI-style checks).

## Latency (expected)

- Phone capture → JPEG (640×480 q70): a few ms, plus a rotate if the sensor is not upright.
- USB `adb reverse`: typically **30–80 ms** stamp-to-viewer when clocks agree; inter-frame ~125 ms at 8 fps.
- Wi-Fi LAN: often **80–250 ms**, jittery on conference networks.
- TTS on-device after a `say` arrives: tens of ms to start speaking (offline engine).
- A vision LLM in `on_frame` should be **1–4 s**; that must never sit on the socket — the latest-frame worker already isolates it.

## APK artifact

`/opt/cursor/artifacts/app-debug.apk` (also `android/app/build/outputs/apk/debug/app-debug.apk` after Gradle).

## Top 3 next steps for the AI

1. Implement the vision call in `server/ai_hook.py` and return one short sentence per processed frame (`Say`).
2. Add a cooldown / scene-change gate so speech is sparse and interruptible (`urgent=True` for hazards only).
3. Turn the optional mic uplink into a question loop (STT on the laptop → answer via `say`).
