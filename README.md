# Scene Companion (SAW)

Hackathon skeleton for an AI-assisted situational-awareness tool. The **phone is the eyes and ears**. The **AI runs on a laptop**. This repo is the plumbing: Android camera/mic uplink, laptop live viewer, and a single hook where the model goes.

- **Android app** (`android/`): CameraX rear-camera JPEG stream over WebSocket, TTS + clip playback, optional mic, TalkBack-first UI.
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

Open the viewer: [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

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

On the phone, set **Laptop address** to `127.0.0.1:8765` (the default). Double-tap the huge **Start** button.

TalkBack should hear **connected**. The laptop viewer should show frames. About every five seconds the demo stub speaks *“frame received, N per second”* on the phone.

**Wi-Fi instead:** put `LAPTOP_LAN_IP:8765` in the address field (the IP printed at server startup). Phone and laptop must be on the same network; some guest Wi-Fi blocks device-to-device.

### 4. Prove audio without the phone

```bash
curl -X POST http://127.0.0.1:8765/say \
  -H 'content-type: application/json' \
  -d '{"text":"stairs ahead, on your right","urgent":false}'
```

Or type in the viewer text box. A scripted fake phone is in `server/tests/fake_client.py`.

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

Phone → laptop: `hello`, `config` (`width`, `height`, `fps`, `quality`, `mic`), `ping`.

Laptop → phone:

- `hello_ack`
- `say` — `{"v":1,"type":"say","text":"...","urgent":false}` (Android TTS; lowest latency, works offline)
- `pong`
- `status` with `msg="audio"` may precede an `AUDIO_CLIP` and can carry `urgent`

HTTP helpers (same laptop):

- `POST /say` JSON `{"text":"...","urgent":false}` or raw text
- `POST /play` raw audio body, `Content-Type` = mime
- `GET /health`, `GET /`

---

## Android notes

- minSdk 26, targetSdk 36, CameraX, one huge Start/Stop control, TalkBack labels, spoken status, haptic on connect/disconnect.
- Defaults: ~640×480, 8 fps, JPEG quality 70. Frames are dropped, never queued.
- Foreground service + wake lock so streaming survives the screen locking.
- Mic uplink is **off by default**.
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
- Cleartext `ws://` only (hackathon LAN / USB). No TLS.
- Mic chunks are accepted by the server and not processed.
- Emulator camera is a fake scene if you use one; a real phone is the intended client.
- JPEG encode + rotate on the phone costs a few milliseconds per frame at 640×480.

## Top 3 next steps for the AI

1. Uncomment / implement the vision call in `on_frame` and return short, hazard-first sentences (`Say`).
2. Add a simple cooldown / change detector so you do not talk over the user every frame.
3. Wire the optional mic into a speech-to-text question loop (`say` the answer, `urgent=True` if it is a hazard).
