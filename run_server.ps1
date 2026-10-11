# Scene Companion laptop server (Windows PowerShell).
# "Continue", not "Stop": in Windows PowerShell 5.1 a pip error on stderr would otherwise abort the script.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
$Py = ".\.venv\Scripts\python.exe"

& $Py -c "import sys, struct; print('Python', sys.version.split()[0], str(struct.calcsize('P')*8) + '-bit')"

Write-Host "Installing core packages..."
& $Py -m pip install -q --upgrade pip
& $Py -m pip install -q -r server\requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Core package install failed. See the error above." -ForegroundColor Red
    exit 1
}

Write-Host "Installing optional packages (speech-to-text, Snowflake, TigerData, tests)..."
foreach ($raw in Get-Content server\requirements-optional.txt) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $out = cmd /c "`"$Py`" -m pip install -q $line 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  skipped $line (not available for this Python; that feature stays off)" -ForegroundColor Yellow
    } else {
        Write-Host "  installed $line"
    }
}

$env:PYTHONPATH = $Root
if (-not $env:SAW_PORT) {
    $env:SAW_PORT = "8765"
}
& $Py -m uvicorn server.app:app --host 0.0.0.0 --port $env:SAW_PORT
