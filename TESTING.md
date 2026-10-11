# SAW field checklist

Automated tests cover clock math, warning policy, injected latency, and a fake phone. This page is what only a real device can show. Each item is pass/fail.

Build/install: `adb install -r /opt/cursor/artifacts/app-debug.apk` (or `android/app/build/outputs/apk/debug/app-debug.apk`). USB: `adb reverse tcp:8765 tcp:8765`, address `127.0.0.1:8765`. Laptop: `./run_server.sh` or `.\run_server.ps1`. Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/).

---

## 1. Capture timestamp source

1. Start streaming on a real phone (not the emulator).
2. Watch laptop logs / `GET /latency` after a few seconds of pings.
3. Uplink p50 should be tens of ms on USB, not thousands, and not equal to `System.currentTimeMillis()` pipeline delay alone.

**Pass:** Viewer “Uplink (synced)” is in a believable band (USB ~10–80 ms, Wi-Fi often 30–200 ms) after the first clock-offset sample.  
**Fail:** Offset never appears, or uplink is multiple seconds.

## 2. Clock sync (USB vs Wi-Fi vs tunnel)

Repeat start/stop on:

| Path | Address | Expect |
| --- | --- | --- |
| USB `adb reverse` | `127.0.0.1:8765` | Smallest RTT, most stable offset |
| Same LAN | `LAPTOP_LAN_IP:8765` | Higher jitter; min-RTT offset still settles |
| Cellular tunnel | `https://….trycloudflare.com` or the hostname | `wss://` connects; RTT larger; offset may be biased (asymmetric). E2E p50/p95 still finite |

**Pass:** `GET /latency` → `clock.offset_ms` and `clock.rtt_ms` populate on each path; e2e p50 appears after any spoken `say`.  
**Fail:** Tunnel falls back to `ws://` (connection error), or e2e is negative / multi-second nonsense after several `say`s.

## 3. Flash test (scene → processed, one laptop clock)

1. Hold the phone so the rear camera fills with the viewer window.
2. Click **Flash test**. The page must go white for a fraction of a second.
3. Read the viewer line or `GET /latency` → `flash.last.scene_to_processed_ms`.

**Pass:** A number appears (often 50–250 ms USB, more on Wi-Fi). A second click replaces it. With the phone pointed away, a flash does **not** latch a detection (or times out).  
**Fail:** Always-on white detection without clicking; or no result with the camera on the white screen.

## 4. Walk toward a wall

1. USB path. Low quality (8 fps). Start walking from ~3 m toward a plain wall at a normal pace (~1.4 m/s).
2. The phone must say **stop, obstacle ahead** (or `SAW_PROXIMITY_TEXT`) *before* you are a foot from the wall — roughly when you still have ~0.5–1.0 m of floor left, given ~200–400 ms e2e.
3. Keep walking / standing. It must **not** repeat every frame. After you step back past ~0.7 m, a new approach may warn again.

**Pass:** One urgent warning on the way in; TalkBack/TTS is the warning, not “frame received…”. Viewer predicted distance drops through 0.3 m around the same moment.  
**Fail:** First warning only after you could touch the wall; or a burst of repeats; or warnings in an empty corridor.

## 5. Urgent interrupts normal speech

1. Let the demo stub speak (`SAW_DEMO_ANNOUNCE_SEC=5`, default) or type a long sentence in the viewer and **Say**.
2. While it is speaking, walk into the wall (or `curl` `POST /say` with `"urgent": true`).

**Pass:** The hazard / urgent line cuts off the current utterance. It is not queued behind it.  
**Fail:** You hear the whole normal sentence first.

## 6. Quality-switch rebind gap

1. While streaming and walking slowly in an empty hall, switch Low → High → Low on the phone picker.
2. Camera will pause briefly.

**Pass:** Stream resumes; no sudden “stop, obstacle ahead” from the gap. Config log on the laptop shows `level`.  
**Fail:** A warning at the moment of the switch with nothing in front of you.

## 7. Reconnect

1. Toggle airplane mode for 2 s, or `adb reverse --remove tcp:8765` then restore.
2. Phone says “connection lost, retrying” then “connected”.

**Pass:** Frames return; clock offset re-settles; a later wall approach still warns.  
**Fail:** App stuck, or warnings fire on the first frames after reconnect.

## 8. Fast / filling object

1. Have a helper step in front of the camera from the side at ~1 m (fills the frame quickly).

**Pass:** Warning on that approach.  
**Fail:** Silence, or a warning that then chatters.

## 9. Viewer and APK smoke

**Pass:** Viewer still shows the live JPEG, quality picker still works while streaming, `ws://host:port` and a pasted `wss://` / `https://` tunnel URL still connect.  
**Fail:** Blank viewer, or the address field no longer accepts a tunnel URL.
