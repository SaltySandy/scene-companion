# Scene Companion laptop server (Windows PowerShell).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -q -r server\requirements.txt
& .\.venv\Scripts\python.exe -m pip install -q -r server\requirements-ai.txt
$env:PYTHONPATH = $Root
if (-not $env:SAW_PORT) {
    $env:SAW_PORT = "8765"
}
& .\.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port $env:SAW_PORT
