# Scene Companion (SAW)

Hackathon skeleton for an AI-assisted situational-awareness tool. The **phone is the eyes and ears**. The **AI runs on a laptop**. This repo is the plumbing: Android camera/mic uplink, laptop live viewer, and a single hook where the model goes.

- **Android app** (`android/`): CameraX rear-camera JPEG stream over WebSocket, TTS + clip playback, optional mic, TalkBack-first UI.
- **Phone web app** (`server/static/phone/`): same live path in Safari / Chrome, no APK. Open `/phone` on the laptop server.
- **Laptop server** (`server/`): FastAPI WebSocket + a local web page showing the live feed. Put vision logic in `server/ai_hook.py`.

Protocol version: **v1**.

---

## 5-minute quickstart

You need: Python 3.11+, a phone with USB debugging (or Wi-Fi), and `adb` from the Android SDK / platform-tools.

### 1. Run the laptop server

macOS or Linux:

```bash
./run_server.sh
```

Windows (PowerShell):

```powershell
.\run_server.ps1
```

Both create a `.venv`, install `server/requirements.txt`, and start uvicorn. On startup the server prints the laptop **LAN IP** and URLs. Default port is **8765**. If Snowflake or TigerData env vars are unset, each logs one line that it is disabled and the stream behaves as before.

Speech-to-text is local faster-whisper on CPU (`int8`, no GPU). The model name is `WHISPER_MODEL` (default `base.en`). The first run downloads it from Hugging Face: **`base.en` is about 150 MB**. `tiny.en` is smaller if you want a quicker first start. `run_server.ps1` prints this before the server comes up.

Open the viewer: [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

Desktop browser test of the phone client (no HTTPS needed): [http://127.0.0.1:8765/phone](http://127.0.0.1:8765/phone)

### 2. Install the APK

A debug APK is the usual artifact (`app-debug.apk`). Install it:

```bash
adb install -r app-debug.apk
# or, after a local Gradle build:
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

### 3. Connect — USB (`adb reverse`) is the reliable hackathon path

Hackathon Wi-Fi is often a mess. USB forwarding just works:

```bash
adb reverse tcp:8765 tcp:8765
```

On the phone, set **Laptop address** to `127.0.0.1:8765` (the default). Double-tap the huge **Start** button. **Listen** (under Start) turns the mic on. It stays off until you tap it.

TalkBack should hear **connected**. The laptop viewer should show frames. About every five seconds the demo stub speaks *“frame received, N per second”* on the phone. With Listen on, a pause after speech shows a grey partial then a black final on the viewer, and the phone says *“You said: …”*.

**Wi-Fi instead:** put `LAPTOP_LAN_IP:8765` in the address field (the IP printed at server startup). Phone and laptop must be on the same network; some guest Wi-Fi blocks device-to-device.

### 4. Prove audio without the phone

```bash
curl -X POST http://127.0.0.1:8765/say \
  -H 'content-type: application/json' \
  -d '{"text":"stairs ahead, on your right","urgent":false}'
```

Or type in the viewer text box. A scripted fake phone is in `server/tests/fake_client.py`.

---

## Use on iPhone

No APK. The laptop serves a VoiceOver-first page at **`/phone`**. iPhone Safari will only give camera and microphone in a **secure context** (HTTPS, or `http://localhost` on the same device). Hackathon Wi-Fi `http://LAN_IP:8765` will fail getUserMedia.

The Windows laptop is already on Tailscale. That is the primary path.

### 1. Start the laptop server

```powershell
.\run_server.ps1
```

Leave it running on port **8765**. Confirm [http://127.0.0.1:8765/health](http://127.0.0.1:8765/health) returns `"ok": true`.

### 2. Put HTTPS in front (Tailscale)

In a second PowerShell window:

```powershell
.\serve_phone.ps1
```

Default mode is **`tailscale serve`** (only your tailnet). For a public demo URL:

```powershell
.\serve_phone.ps1 -Mode funnel
```

The script prints the HTTPS origin. Open this on the iPhone (Safari):

```
https://<machine>.<tailnet>.ts.net/phone
```

The WebSocket URL is derived from the page (`wss://…/v1/stream`). Leave the override field blank unless you are pointing at another host.

**iPhone must be on the same Tailscale tailnet** for `serve` (install Tailscale, sign in to the same account / invite). `funnel` is reachable from the public internet — use only for the stage demo.

Manual equivalent if you skip the helper:

```powershell
tailscale serve --bg 8765
tailscale serve status
# public:
# tailscale funnel --bg 8765
```

### 3. On the iPhone

1. Open `https://<your-tailscale-hostname>/phone`.
2. Double-tap the huge **Start** button (this one tap unlocks Web Audio + speech, then asks for the camera).
3. Allow the rear camera. **Listen** (under Start) is the mic/STT toggle; it stays off until you tap it.
4. VoiceOver should hear **connected**. The laptop viewer at `/` should show frames. The demo stub speaks *“frame received, N per second”* about every five seconds.

### Fallback: Cloudflare quick tunnel

If Tailscale serve/funnel is blocked:

```powershell
.\serve_phone.ps1 -Mode cloudflare
```

That runs `cloudflared tunnel --url http://127.0.0.1:8765` and prints a `https://*.trycloudflare.com` URL. Open `/phone` on that host. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) first (`winget install Cloudflare.cloudflared`).

### Desktop testing without HTTPS

Chrome or Edge on the laptop: [http://127.0.0.1:8765/phone](http://127.0.0.1:8765/phone). `localhost` is a secure context, so the camera works.

### iOS limits you cannot fully work around

- **No camera/mic on plain `http://` LAN IP.** Must be HTTPS (or localhost).
- **Audio needs that first Start tap.** Safari will not autoplay TTS or clips.
- **`navigator.vibrate` does not exist.** Proximity haptics fall back to the same tones as Android.
- **Backgrounding pauses the camera.** The page reconnects and restarts the camera when you come back; it is not an Android foreground service.
- **Wake Lock** works on iOS 16.4+; older Safari may still dim.
- **Screen lock / other apps** will stop the stream until Safari is foreground again.

---

## Where the AI goes

Edit **`server/ai_hook.py`**. The function is:

```python
async def on_frame(jpeg_bytes: bytes, ts: int) -> Say | Play | None:
    ...
```

A background worker calls this with **only the latest frame**. If your model is slower than the camera, older JPEGs are dropped — the WebSocket never waits.

Talk back to the phone from the hook (return value) or from anywhere:

```python
from server.hub import say, play

await say("door on your left", urgent=False)
await play(wav_bytes, mime="audio/wav", urgent=True)
```

`urgent=True` interrupts current speech on the phone. Normal messages queue.

A commented OpenAI-compatible / Gemini vision example lives in the same file. Keys are env vars (`OPENAI_API_KEY` or `GEMINI_API_KEY`); the demo stub runs with no key.

Speech lands in the same module:

```python
async def on_transcript(text: str, is_final: bool, ts: int) -> None:
    ...
```

The stub logs every partial and final. Finals are written to the event sinks and spoken back with `say(f"You said: {text}")`. Whisper runs on a background thread; the camera worker never waits on it.

---

## Protocol v1

WebSocket paths:

| Who | URL |
| --- | --- |
| Phone | `ws://HOST:8765/v1/stream` |
| Viewer page | `ws://HOST:8765/v1/viewer` |

### Binary frames (both directions)

All binary WebSocket messages use this envelope:

```
Offset  Size  Field
0       4     magic = b"SAW1"
4       1     kind
5       1     flags
6       2     reserved (0)
8       8     timestamp_ms (uint64 big-endian, Unix epoch ms)
16      4     payload_len (uint32 big-endian)
20      N     payload
```

| kind | direction | payload | flags |
| --- | --- | --- | --- |
| `0x01` JPEG_FRAME | phone → laptop | JPEG bytes | 0 |
| `0x02` MIC_PCM | phone → laptop | PCM16LE mono 16 kHz | 0 |
| `0x03` AUDIO_CLIP | laptop → phone | WAV / MP3 / PCM bytes | `0=wav`, `1=mp3`, `2=pcm16le 16 kHz mono` |

If a frame is still on the phone’s outbound WebSocket queue, the next JPEG is **dropped** (no backlog).

### JSON text frames

Every text message is `{"v":1,"type":"..."}`.

Phone → laptop: `hello`, `config` (`width`, `height`, `fps`, `quality`, `mic`), `ping`, `speaking`.

`speaking` is half-duplex: `{"v":1,"type":"speaking","active":true}` while the phone is playing TTS or a clip. The server drops mic audio until that flag clears, plus a short tail. `say()` also mutes for about `0.45 + 0.05 * len(text)` seconds (capped at 8) so the speaker does not get transcribed before the phone flag arrives.

Laptop → phone:

- `hello_ack`
- `say` — `{"v":1,"type":"say","text":"...","urgent":false}` (Android TTS; lowest latency, works offline)
- `pong`
- `status` with `msg="audio"` may precede an `AUDIO_CLIP` and can carry `urgent`

Laptop → viewer (same socket as stats):

- `transcript` — `{"v":1,"type":"transcript","text":"...","final":false,"ts":1710000000000}`  
  Partials are grey on the viewer page; finals are black, with a timestamp.

HTTP helpers (same laptop):

- `POST /say` JSON `{"text":"...","urgent":false}` or raw text
- `POST /play` raw audio body, `Content-Type` = mime
- `GET /health`, `GET /`

---

## Android notes

- minSdk 26, targetSdk 36, CameraX, one huge Start/Stop control, TalkBack labels, spoken status, haptic on connect/disconnect.
- Defaults: ~640×480, 8 fps, JPEG quality 70. Frames are dropped, never queued.
- Foreground service + wake lock so streaming survives the screen locking.
- Mic uplink is **off by default**. Tap **Listen** (TalkBack: “Listen. Microphone streaming, off by default…”). It can be toggled while streaming. The mic stays off until half-duplex is confirmed on a device: the speaker plays TTS, so an always-on mic would transcribe the assistant.
- Mic format is 16 kHz mono PCM16, sent in 100 ms chunks. `AudioRecord` uses `VOICE_COMMUNICATION`, and `AcousticEchoCanceler` is enabled when the device has it. While TTS or a clip is playing, the phone sends `speaking` and the server ignores the mic.
- Server address is persisted.

Build the debug APK (needs Android SDK):

```bash
export ANDROID_HOME="$HOME/android-sdk"   # or your SDK path
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android && ./gradlew :app:assembleDebug
```

The APK lands at `android/app/build/outputs/apk/debug/app-debug.apk`.

---

## Laptop tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r server/requirements.txt
PYTHONPATH=. python -m pytest server/tests -q
```

Phone web client (protocol + Playwright with Chromium’s fake camera):

```bash
pip install -r server/requirements-web.txt
python -m playwright install chromium
PYTHONPATH=. python -m pytest server/tests/test_phone_page.py server/tests/test_phone_protocol.py server/tests/test_phone_compat.py server/tests/test_phone_web.py -q
```

`test_phone_compat.py` boots the servers from `feat/proximity` and `cursor/proximity-warning-977b` in git worktrees (or a dialect stub if a branch will not start) and drives the same JS protocol the browser uses.

---

## Error monitoring

Sentry is wired on both sides and does not change the stream. The server reads `SENTRY_DSN` and falls back to the sightline-server project when the variable is unset. Set it empty to turn reporting off. Android uses the sightline-android DSN from the `sentryDsn` Gradle property (`-PsentryDsn=...`), written into `BuildConfig` and the manifest. Environment is `hackathon`. The phone release is `versionName+versionCode` (currently `0.1.0+1`). Camera frames and other images are not attached.

Test events:

```bash
curl http://127.0.0.1:8765/debug/sentry
```

On the phone, long-press the title **Scene Companion**.

## Snowflake and TigerData

Both are optional event logs. They are not on the camera path: `log_event` only enqueues, a background thread batch-inserts, and a full queue drops events. Connect, disconnect, and every `say()` are recorded. Copy `.env.example` to `.env` when you have credentials.

**Snowflake** (`server/snowflake_store.py`): `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` or `SNOWFLAKE_PRIVATE_KEY_PATH`, plus `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_ROLE`. `ensure_schema()` creates `SCENE_EVENTS` (`ts`, `session_id`, `kind`, `text`, `hazard`, `meta VARIANT`). `recall()` calls `SNOWFLAKE.CORTEX.COMPLETE` over the latest events. A Cortex Search call is left commented in that function.

**TigerData** (`server/tiger_store.py`): `TIGER_DATABASE_URL`. `ensure_schema()` creates `scene_events` and turns it into a hypertable when TimescaleDB is installed; otherwise it stays a plain table. `recent_events(window)` and `recall(question)` read that table. A pgvector/pgai search is left commented.

`server/event_sinks.py` is the only fan-out. The server calls `log_event` once.

```bash
curl -X POST http://127.0.0.1:8765/recall \
  -H 'content-type: application/json' \
  -d '{"question":"what happened recently","backend":"snowflake"}'
```

`backend` is `snowflake`, `tiger`, or omitted (whichever store is configured; Snowflake wins if both are). The viewer page has a Recall box. Smoke checks, which need real credentials:

```bash
python scripts/snowflake_smoke.py
python scripts/tiger_smoke.py
```

## Known limitations

- No on-device model. The AI is a stub plus a commented API example.
- Phone and laptop clocks are not synced; “latency” on the viewer is `recv_ts - capture_ts` and can look wrong across Wi-Fi. Inter-frame time is the honest number.
- The Android APK still uses cleartext `ws://` (hackathon LAN / USB). The `/phone` web client uses `wss://` when the page itself is HTTPS (Tailscale serve/funnel or a Cloudflare tunnel).
- Transcripts are English (`language="en"`) on CPU. The first `base.en` download is about 150 MB.
- Emulator camera is a fake scene if you use one; a real phone is the intended client. The emulator also has to be checked before this build goes on a phone (see the report).
- JPEG encode + rotate on the phone costs a few milliseconds per frame at 640×480.

## Top 3 next steps for the AI

1. Uncomment / implement the vision call in `on_frame` and return short, hazard-first sentences (`Say`).
2. Add a simple cooldown / change detector so you do not talk over the user every frame.
3. Replace the `on_transcript` stub with a real question answer (`say` the answer, `urgent=True` if it is a hazard).
