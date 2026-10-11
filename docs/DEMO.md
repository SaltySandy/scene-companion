# Blindfolded walk: demo runbook

The phone is a proximity sensor, not a guide. The spotter is responsible for the
walker's safety at all times. The tones help; they do not replace the spotter.

## What the walker hears

| Tone | Meaning | Walker does |
|------|---------|-------------|
| Soft tick every 2 s | Clear ahead, sensor alive | Walk slowly |
| Beep every ~0.35 s (880 Hz), short buzz | Something within ~1.6 m | Slow down, reach out |
| Near-continuous high tone (1320 Hz), strong buzz | Something within ~0.8 m | **Stop** |
| Falling two-tone every second, long buzz, "sensor lost" | No fresh reading for 1 s | **Stop and wait** |

With earbuds, beeps pan toward the side with the closer obstacle. A short phrase such as
"open door ahead" may follow a new obstacle if a Claude API key is set. Silence is never
"clear": if the ticks stop, the sensor is not working.

## Safety rules (read aloud before every walk)

1. The spotter stays within arm's reach, on the walker's side, for the whole walk.
2. The stop word is **"stop"**. It beats any tone. The walker freezes at once.
3. Route: indoors, flat, no stairs, no glass walls or mirrors, cleared of low clutter.
   The chest camera cannot see objects within about 1 m of the feet, and depth models
   misread glass and mirrors.
4. Walk slowly. "Sensor lost" means stop and wait. Two losses in one walk ends the walk.
5. Only run a walk after `scripts/preflight.py` prints GO.

## One-time setup

Laptop:

```bash
./run_server.sh                     # installs server + AI requirements, starts on :8765
pip install -r scripts/requirements-export.txt
python scripts/export_depth_onnx.py # writes models/da2-metric-indoor-s-350x266.onnx (~100 MB)
export ANTHROPIC_API_KEY=...        # optional: obstacle names and pre-labeling
```

Phone: build and install the APK from a machine with JDK 17 and the Android SDK
(Android Studio is easiest):

```bash
cd android && ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Mount: phone **upright in portrait** on the chest strap, rear camera facing forward,
level, lens unobstructed. Upright frames matter: a sideways phone swaps left/right with
up/down. Other mounts need the `mount_rotation` pref (0/90/180/270).

## Before each walk (5 minutes)

1. Connect: USB is the reliable path. Run `adb reverse tcp:8765 tcp:8765`; the phone
   address stays `127.0.0.1:8765`. On Wi-Fi, use the LAN IP the server prints.
2. Start the server (`SAW_RECORD=1 ./run_server.sh` for rehearsals). The banner must show
   `Depth : da2-metric-indoor-s-...`, not `MISSING`.
3. Start the app, strap it on, and wait for "connected" and then "sensor on".
4. Run `python scripts/preflight.py`. It needs **GO**.
5. Tone check, eyes open: walk toward a wall and listen for tick, then beeps, then the
   continuous tone. Cover the lens with a hand: the levels jump. Unplug USB or stop the
   server: "sensor lost" within about a second.
6. Open `http://127.0.0.1:8765/` for the audience. The badge shows what the walker hears.

## Rehearse and tune (between walks)

```bash
SAW_RECORD=1 ./run_server.sh                        # walk the route eyes-open
python scripts/session_report.py                    # latency, drops, blind gaps
python scripts/prelabel.py --every 5                # Claude labels distances (needs key)
python scripts/eval_proximity.py sessions/<id> ...  # sweeps thresholds, prints env vars
```

Apply the printed `SAW_*` values when you start the server. The eval refuses to recommend
until it has at least 30 near/danger examples, so record walks that approach walls, doors,
and people. Spot-check labels in `labels.jsonl` before trusting a threshold.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Banner says `Depth : MISSING` | Run `scripts/export_depth_onnx.py` |
| Preflight: longest gap or rate FAIL | Use USB (`adb reverse`); hackathon Wi-Fi drops frames |
| Frame age WARN (> 300 ms) | Close other apps on the laptop; lower phone fps |
| Clock sync / camera-to-cue WARN | Phone runs an old APK; reinstall the current build |
| Constant "near" in open space | The floor is in the band: tilt the phone up, or lower `SAW_ROI_BOTTOM` |
| No obstacle names | Set `ANTHROPIC_API_KEY`; the server log says "describe disabled" |

## Settings (env vars on the server)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SAW_NEAR_M` / `SAW_DANGER_M` | 1.6 / 0.8 | Level thresholds in metres |
| `SAW_ROI_TOP` / `SAW_ROI_BOTTOM` | 0.15 / 0.75 | Image band used (fraction of height) |
| `SAW_SECTOR_PERCENTILE` | 10 | Depth percentile per sector |
| `SAW_STALE_MS` | 700 | Older frames are sent as unknown |
| `SAW_RECORD` / `SAW_RECORD_JPEG` | off / on | Record `sessions/<id>/` |
| `SAW_PROXIMITY` | 1 | 0 turns the sensor off (for `/debug/proximity` tests) |
| `SAW_DESCRIBE` / `SAW_VLM_MODEL` | 1 / claude-opus-5-5 | Spoken obstacle names |
