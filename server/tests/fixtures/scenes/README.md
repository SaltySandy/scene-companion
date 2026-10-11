# Scene fixtures

These are **synthetic, AI-generated** photos (not real photos) of the same
demo room: four black stackable chairs, a folding table, and a gray door with
a red EXIT sign.

| Path | What |
| --- | --- |
| `original/` | Full-size renders |
| `640x480/` | JPEG quality 70, matching the phone stream |
| `manifest.json` | Structured, scoreable ground truth plus the original free-text notes |

Replay and score with:

```powershell
$env:SENTRY_DSN = ""; $env:PYTHONPATH = "."; python scripts/replay_scenes.py
```
