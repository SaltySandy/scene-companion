# Gives the laptop server a public https:// link so an iPhone can use the camera page.
# Run this in a SECOND PowerShell window while run_server.ps1 is running.
$ErrorActionPreference = "Continue"
$Port = if ($env:SAW_PORT) { $env:SAW_PORT } else { "8765" }

$cf = $null
$cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
if ($cmd) { $cf = $cmd.Source }
foreach ($guess in @("C:\Program Files (x86)\cloudflared\cloudflared.exe", "C:\Program Files\cloudflared\cloudflared.exe")) {
    if (-not $cf -and (Test-Path $guess)) { $cf = $guess }
}
if (-not $cf) {
    Write-Host "cloudflared is not installed. Installing it with winget..." -ForegroundColor Yellow
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
    Write-Host ""
    Write-Host "Done. Close this window, open a new PowerShell window, and run .\run_tunnel.ps1 again." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "Starting a free Cloudflare tunnel to http://localhost:$Port ..." -ForegroundColor Cyan
Write-Host "Look below for a line like  https://something-random.trycloudflare.com" -ForegroundColor Cyan
Write-Host "On the iPhone, open that link in Safari and add /phone to the end." -ForegroundColor Cyan
Write-Host "Keep this window open while testing. Press Ctrl+C to stop." -ForegroundColor Cyan
Write-Host ""
& $cf tunnel --url "http://localhost:$Port"
