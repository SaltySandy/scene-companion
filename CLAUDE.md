# Scene Companion — project notes for Claude Code

Hackathon project. An Android phone (chest-mounted) streams camera JPEGs to a laptop
FastAPI server. The laptop runs AI to act as a **proximity sensor for a visually impaired
person**: avoid obstacles, navigate safely. The demo is a blindfolded walk with a spotter.

## Run and test

```bash
./run_server.sh                                  # server on :8765, viewer at http://127.0.0.1:8765/
python -m server.tests.pump_frames --count 200   # fake phone
SENTRY_DSN= python -m pytest -q                  # ALWAYS blank SENTRY_DSN: app.py has a default DSN baked in
```

## Architecture in one paragraph

`app.py` receives SAW v1 binary frames on `/v1/stream` and calls `hub.submit_frame`.
`hub.py` keeps ONE latest-frame slot; a single `worker_loop` task takes the newest frame
(`take_latest() -> PendingFrame`) and awaits `ai_hook.on_frame`. Older frames are
overwritten and counted (`frames_dropped`, `PendingFrame.dropped_before`). Responses go
back to the phone via `hub.say()` / `hub.play()`. Protocol lives in `server/protocol.py`
and must stay in sync with `android/.../Protocol.kt`.

## Non-negotiable design rules

- **Drop, don't queue.** Freshness beats completeness at every hop.
- **Silence must never mean "clear".** Proximity output is a heartbeat sent on every
  processed frame; the phone plays a "sensor lost" cue if updates stop for ~1 s.
- **Never touch `hub` from a worker thread.** Blocking inference runs in
  `asyncio.to_thread`; the thread gets bytes and returns a result, the event loop does
  all hub bookkeeping. No locks are needed as long as this holds.
- **Lineage:** every frame keeps `capture_ts` (phone clock) and `recv_ts` (laptop clock),
  plus `infer_start`/`infer_end` and `clock_offset_ms`. The phone acks each frame with
  `cue_start_ts` (phone clock, `sessions/<id>/acks.jsonl`), so camera-to-ear latency
  needs no clock sync.
  `capture_ts` is the frame key until a real `frame_id` exists.
- **Camera frames never leave the laptop** and are never committed (`sessions/` is
  git-ignored). Sentry already excludes images.
- **Database sinks are off the hot path** (enqueue only, background thread).

## Commit plan (branch `feat/proximity`, one commit per step, conventional commits)

Owners: A = data and lineage, B = AI/ML, C = Android and demo.

| # | Commit | Owner | Status |
|---|--------|-------|--------|
| 1 | feat(server): carry recv_ts and count dropped frames | A | done |
| 2 | feat(server): FrameRecord + JSONL recorder (`server/frame_record.py`, `server/recorder.py`, `SAW_RECORD=1`) | A | done |
| 3 | feat(tools): `scripts/session_report.py` (latency p50/p95, drops, capture_ts gaps) | A | done |
| 3a | feat(lineage): ping/pong clock sync, `clock_offset_ms`/`clock_rtt_ms` in FrameRecord | A+C | done |
| 4 | fix(android): fixed mount rotation; skip encoding while disconnected | C | done |
| 5 | feat(tools): `scripts/prelabel.py` VLM pre-labeling | B | done |
| 6 | feat(ai): `server/ai/depth.py` + `server/ai/proximity.py` (sectors, levels) | B | done |
| 7 | feat(tools): `scripts/eval_proximity.py` offline eval, tune thresholds | A+B | done (needs real labeled walks) |
| 8 | feat(protocol): `proximity` message + `POST /debug/proximity` | C | done |
| 9 | feat(ai): depth in `on_frame` via `to_thread`, staleness gate, heartbeat | B | done |
| 10 | feat(android): `ProximityCue.kt` tones/haptics + watchdog | C | done |
| 11 | feat(lineage): phone `ack {frame_key, cue_start_ts}` | A+C | done |
| 12 | feat(ai): `server/ai/describe.py` VLM "what is it" on new obstacles | B | done |
| 13–15 | stretch: voice questions, structured sink events, demo runbook | | |

File ownership to avoid conflicts: A owns `hub.py`, `recorder.py`, `frame_record.py`;
B owns `server/ai/` and `ai_hook.py`; C owns `android/` and both protocol files.

## Target per-frame record (commit 2)

`session_id, frame_key, capture_ts, recv_ts, infer_start, infer_end, model,
sectors_m{left,center,right}, level, decision (sent|suppressed|stale|none), message,
dropped_since_last, clock_offset_ms, clock_rtt_ms` — written as one JSON line per processed frame to
`sessions/<session_id>/frames.jsonl`.
