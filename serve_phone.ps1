# HTTPS front door for the /phone web client (Windows + Tailscale).
# The laptop server must already be listening on http://127.0.0.1:<port>
# (run .\run_server.ps1 in another window; same port).
#
# Port: -Port, then $env:SAW_PORT, then 8765.
#
#   .\serve_phone.ps1                      # tailnet-only Tailscale Serve on 8765
#   .\serve_phone.ps1 -Port 8767           # same, other port (verified on stage)
#   $env:SAW_PORT = "8767"; .\serve_phone.ps1
#   .\serve_phone.ps1 -Mode cloudflare
#   .\serve_phone.ps1 -Mode print
#
# Serve (verified):
#   tailscale serve --bg --https=443 http://127.0.0.1:<port>
# Stop:
#   tailscale serve --https=443 off
#
# iPhone: Tailscale app, same tailnet, then https://<magicdns>/phone
# Full steps: docs/IPHONE.md

param(
    [ValidateSet("serve", "funnel", "cloudflare", "print")]
    [string]$Mode = "serve",
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

if ($Port -le 0) {
    if ($env:SAW_PORT) {
        $Port = [int]$env:SAW_PORT
    } else {
        $Port = 8765
    }
}

$Local = "http://127.0.0.1:$Port"

function Get-Health {
    try {
        return Invoke-RestMethod -Uri "$Local/health" -TimeoutSec 2
    } catch {
        return $null
    }
}

Write-Host "Scene Companion phone HTTPS helper"
Write-Host "Local server: $Local/phone"
Write-Host "Port: $Port  (override with -Port or SAW_PORT)"
Write-Host ""

$health = Get-Health
if (-not $health) {
    Write-Host "Nothing is listening on $Local."
    Write-Host "In another PowerShell window, start the server on the SAME port:"
    Write-Host "  `$env:SAW_PORT = '$Port'; .\run_server.ps1"
    Write-Host "Then re-run this script."
    if ($Mode -ne "print") {
        exit 1
    }
} else {
    Write-Host "Server is up. proto=$($health.proto) lan=$($health.lan_ip)"
}

function Get-TailscaleHost {
    $dns = $null
    try {
        $status = tailscale status --json | ConvertFrom-Json
        $dns = $status.Self.DNSName
        if ($dns) { $dns = $dns.TrimEnd(".") }
    } catch {
        $dns = $null
    }
    if (-not $dns) {
        try {
            $dns = (tailscale ip -4 2>$null | Select-Object -First 1)
        } catch {
            $dns = $null
        }
    }
    return $dns
}

if ($Mode -eq "print") {
    $hostName = Get-TailscaleHost
    if ($hostName) {
        Write-Host "Tailscale host: $hostName"
        Write-Host "Phone URL (after serve): https://$hostName/phone"
    }
    Write-Host "Desktop: $Local/phone"
    Write-Host "See docs/IPHONE.md"
    exit 0
}

if ($Mode -eq "cloudflare") {
    $cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cloudflared) {
        Write-Host "cloudflared is not on PATH."
        Write-Host "Install: winget install Cloudflare.cloudflared"
        Write-Host "Then:    cloudflared tunnel --url $Local"
        exit 1
    }
    Write-Host "Starting a Cloudflare quick tunnel. Open the printed https URL + /phone"
    Write-Host "Example: https://random-words.trycloudflare.com/phone"
    & cloudflared tunnel --url $Local
    exit $LASTEXITCODE
}

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    Write-Host "tailscale is not on PATH. Install Tailscale for Windows and sign in."
    exit 1
}

if ($Mode -eq "funnel") {
    Write-Host "Enabling Tailscale Funnel (public HTTPS) -> $Local"
    Write-Host "Anyone with the URL can reach the camera page. Stage use only."
    Write-Host "Prefer Serve for testers: .\serve_phone.ps1 -Port $Port"
    & tailscale funnel --bg --https=443 $Local
} else {
    Write-Host "Enabling Tailscale Serve (tailnet-only HTTPS) -> $Local"
    Write-Host "Command: tailscale serve --bg --https=443 $Local"
    & tailscale serve --bg --https=443 $Local
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "tailscale $Mode failed (exit $LASTEXITCODE)."
    Write-Host "Verified syntax:"
    Write-Host "  tailscale serve --bg --https=443 $Local"
    Write-Host "  tailscale serve --https=443 off"
    exit $LASTEXITCODE
}

Write-Host ""
& tailscale serve status
Write-Host ""
$hostName = Get-TailscaleHost
if ($hostName) {
    Write-Host "On the iPhone (Safari, Tailscale app signed into this tailnet):"
    Write-Host "  https://$hostName/phone"
} else {
    Write-Host "On the iPhone open https://<your-machine>.<tailnet>.ts.net/phone"
    Write-Host "Hostname: tailscale status"
}
Write-Host ""
Write-Host "Leave this config up for the demo. Turn it off with:"
Write-Host "  tailscale serve --https=443 off"
Write-Host "See docs/IPHONE.md"
