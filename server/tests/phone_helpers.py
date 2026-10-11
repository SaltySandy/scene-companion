"""Shared helpers for the /phone web-client tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
PHONE = REPO / "server" / "static" / "phone"
WT_ROOT = Path("/tmp/saw-phone-compat-worktrees")
COMPAT = PHONE / "compat_client.js"


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def server_env(extra: dict[str, str] | None = None, pythonpath: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["SENTRY_DSN"] = ""
    env["SAW_DEMO_ANNOUNCE_SEC"] = "0.3"
    env["SAW_PROXIMITY"] = "0"
    env["SAW_PROXIMITY_ENABLED"] = "0"
    env["WHISPER_MODEL"] = "tiny.en"
    env["PYTHONPATH"] = str(pythonpath or REPO)
    if extra:
        env.update(extra)
    return env


def start_uvicorn(port: int, cwd: Path | None = None, extra_env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    env = server_env(extra_env, pythonpath=cwd or REPO)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(cwd or REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 25
    last_err = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"uvicorn exited {proc.returncode}: {out}")
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.4)
            if r.status_code == 200:
                return proc
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.1)
    proc.kill()
    out = proc.stdout.read() if proc.stdout else ""
    raise TimeoutError(f"server on {port} did not become healthy ({last_err}): {out}")


def stop_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_compat(url: str, frames: int = 8, wait_ms: int = 2500) -> dict:
    proc = start_compat(url, frames=frames, wait_ms=wait_ms)
    return finish_compat(proc)


def start_compat(url: str, frames: int = 8, wait_ms: int = 2500) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["node", str(COMPAT), "--url", url, "--frames", str(frames), "--wait-ms", str(wait_ms)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_compat(proc: subprocess.Popen[str], timeout: float = 20) -> dict:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise AssertionError(f"compat_client hung: {out} {err}") from None
    if proc.returncode != 0:
        raise AssertionError(f"compat_client failed: {out} {err}")
    return json.loads(out)


def ensure_worktree(name: str, branch: str) -> Path:
    dest = WT_ROOT / name
    WT_ROOT.mkdir(parents=True, exist_ok=True)
    if (dest / "server" / "app.py").exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "origin", branch], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-f", f"origin/{branch}"], check=False, capture_output=True)
        return dest
    subprocess.run(
        ["git", "-C", str(REPO), "worktree", "add", "--detach", str(dest), f"origin/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest
