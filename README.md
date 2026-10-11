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

A background worker calls this with **only the latest frame**. If your model is slower than the camera, older JPEGs are dropped — the WebSocket never waits. A second, isolated worker runs the deterministic proximity path so a 1–4 s vision LLM cannot delay a one-foot warning.

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

Phone → laptop: `hello`, `config` (`width`, `height`, `fps`, `quality`, `mic`), `ping` (`t0`), `sync` (`t0,t1,t2,t3`), `tts_start` (`capture_ts`, `tts_start_ts`, `say_recv_ts`, `e2e_ms`), `audio_event` (`id`, `event`, `volume`, `muted`, `error_code`), `frame_meta` (optional sidecar immediately before a JPEG: `ts`, `sensor_ts_ns`, `focus_distance_diopters`, `af_state`, `focus_calibration`). Old servers ignore unknown JSON types; JPEG bytes stay pure.

Laptop → phone:

- `hello_ack`
- `say` — `{"v":1,"type":"say","text":"...","urgent":false,"capture_ts":...,"id":"a0001"}` (Android TTS; lowest latency, works offline). `capture_ts` is the frame the speech is about; the phone reports TTS start against it. `id` is the audio-out row; older phones ignore it.
- `pong` — echoes `t` / `t0` and adds laptop `t1` (recv) and `t2` (send) for NTP-style sync
- `status` with `msg="audio"` may precede an `AUDIO_CLIP` and can carry `urgent`

HTTP helpers (same laptop):

- `POST /say` JSON `{"text":"...","urgent":false}` or raw text
- `POST /play` raw audio body, `Content-Type` = mime
- `GET /health`, `GET /`
- `GET /latency` — rolling e2e p50/p95, stage breakdown, clock offset, flash result, proximity snapshot
- `GET /audio_log` — last ~200 intended `say` / clip outputs and phone lifecycle (`received` → `done`)
- `GET /proximity_trace` — last N per-frame proximity rows (`?n=80`); full session JSONL in `server/traces/`
- `POST /flash` — arm the on-demand flash detector (`{"t": laptop_ms}` optional)

---

## Latency

The figure the warning uses is **capture → TTS actually starts**, on the **phone clock**. The server echoes each frame’s capture timestamp in `say`; the phone measures until `UtteranceProgressListener.onStart` and sends `tts_start`. Subtracting phone and laptop wall clocks is not used for that number.

**True capture time.** CameraX `imageInfo.timestamp` is converted to wall-clock using `elapsedRealtime` or uptime. If the device’s timestamp source is neither (or the sample is implausibly old/future), the analyzer falls back to `System.currentTimeMillis()`.

**Clock offset.** The phone pings every 2 s. The server replies with `t1`/`t2`. The phone sends `sync`. The **minimum-RTT** sample is kept (NTP offset: `laptop ≈ phone + offset`). Asymmetric paths (cellular tunnels) bias the offset; e2e on the phone clock stays honest. Stages (uplink, queue, process, downlink, TTS) use the offset and are shown on the viewer and `GET /latency`.

**Flash test (on-demand, not always on).** Point the phone at the laptop viewer. Click **Flash test**. The page goes white at a known laptop time; the server arms a brightness check. The first near-white frame gives **scene → processed** on the laptop clock. `POST /flash` does the same for scripts.

The viewer “Latency (stamp)” line is uplink after sync (or the old `recv - capture` figure before the first ping). Prefer e2e p50/p95.

---

## Proximity warning

A fast, deterministic estimator (`server/proximity.py`, default `corridor_looming`) looks at the center walking corridor: dark / high-contrast mass + pinhole size (`distance = assumed_width × focal / apparent_width`) and fill. A well-lit, textureless frame is a low-confidence near surface (plain wall), not `no_signal`. Same JPEG → same meters. No weights are downloaded; pip-install **numpy** and **pillow** (already in `server/requirements.txt`). The estimator is a `ProximityEstimator` protocol — drop in a depth model by implementing `estimate(jpeg)` and setting `SAW_PROXIMITY_ESTIMATOR` or `engine.set_estimator(...)`. Frames are downscaled to 320 px wide before the math. Measured CPU (Pillow + numpy, no GPU): **~2.1 ms/frame at 640×480**, ~3.7 ms at 720p, ~7.5 ms at 1080p. That stays inside the ~220 ms one-foot walking budget.

The phone also sends Camera2 `LENS_FOCUS_DISTANCE` (diopters) plus AF state and calibration. The server trusts AF only when calibration is APPROXIMATE/CALIBRATED and AF is converged, converts `meters = 1/diopters`, and **fuses** with the image estimate by taking the nearer usable reading. Closing speed is a clamped median (cap 2.2 m/s, outlier reject) so a size-change spike cannot look like a sprint. It warns when

```
predicted = distance - closing_speed × (frame_age + remaining_p95) / 1000
```

is ≤ **0.3 m** (one foot). `urgent=True` so the phone interrupts whatever was speaking. Cooldown 2 s + 0.35 m hysteresis stop flapping. Frames older than 800 ms are dropped as stale.

| Env | Default | Meaning |
| --- | --- | --- |
| `SAW_PROXIMITY_ENABLED` | `1` | Set `0` to disable |
| `SAW_PROXIMITY_THRESHOLD_M` | `0.3` | Warn threshold (meters) |
| `SAW_PROXIMITY_HYSTERESIS_M` | `0.35` | Re-arm only after predicted clears this extra |
| `SAW_PROXIMITY_COOLDOWN_MS` | `2000` | Min time between spoken warnings |
| `SAW_PROXIMITY_STALE_MS` | `800` | Drop frames older than this |
| `SAW_PROXIMITY_OBJECT_WIDTH_M` | `0.6` | Assumed obstacle width for the pinhole |
| `SAW_PROXIMITY_ESTIMATOR` | `corridor_looming` | Pluggable name |
| `SAW_DEFAULT_REMAINING_MS` | `250` | Used before enough `tts_start` samples exist |
| `SAW_PROXIMITY_TEXT` | `stop, obstacle ahead` | Spoken warning |
| `SAW_PROXIMITY_MAX_CLOSING_MPS` | `2.2` | Cap on derived closing speed |
| `SAW_TRACE_JPEG_EVERY` | `10` | Save every Nth JPEG next to the session JSONL; `0` disables |

The LLM / demo stub in `ai_hook.py` is unchanged and runs beside this path.

Field checks (flash test, walk-toward-a-wall, USB vs Wi-Fi vs tunnel) are in **`TESTING.md`**.

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

The APK lands at `android/app/build/outputs/apk/debug/app-debug.apk`. Debug builds are signed with `android/keystore/debug.keystore` so `adb install -r` can replace the current Pixel install.

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

- No on-device model. The AI is a stub plus a commented API example. The proximity path is a corridor heuristic, not a trained depth model: glass, night, and off-axis obstacles will be missed or mis-ranged.
- Assumed obstacle width (0.6 m) is a prior. A wall that fills the frame is forced near; a thin pole can read far.
- Clock **offset** is min-RTT NTP and is biased by uplink/downlink asymmetry (typical on a cellular tunnel). The warning does **not** use that offset for e2e; it uses phone-clock capture → TTS start.
- Before the first few `tts_start` reports, remaining latency is the 250 ms default.
- Cleartext `ws://` only on LAN / USB. Tunnel URLs use `wss://` (see the address field). No auth on the socket.
- Mic chunks are accepted by the server and not processed.
- Emulator camera is a fake scene if you use one; a real phone is the intended client.
- JPEG encode + rotate on the phone costs a few milliseconds per frame at 640×480.
- Flash test only works if the phone is pointed at the laptop screen.

## Top 3 next steps for the AI

1. Uncomment / implement the vision call in `on_frame` and return short, hazard-first sentences (`Say`).
2. Add a simple cooldown / change detector so you do not talk over the user every frame.
3. Wire the optional mic into a speech-to-text question loop (`say` the answer, `urgent=True` if it is a hazard).
