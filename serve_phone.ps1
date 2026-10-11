# HTTPS front door for the /phone web client (Windows + Tailscale).
# The laptop server must already be listening on http://127.0.0.1:8765
# (run .\run_server.ps1 in another window).
#
#   .\serve_phone.ps1                 # tailnet-only: tailscale serve
#   .\serve_phone.ps1 -Mode funnel    # public HTTPS: tailscale funnel
#   .\serve_phone.ps1 -Mode cloudflare
#   .\serve_phone.ps1 -Mode print     # show URLs, do not change serve config
#
# iPhone: install Tailscale, join the same tailnet (serve) or use the funnel /
# trycloudflare URL, then open https://<host>/phone and tap Start.

param(
    [ValidateSet("serve", "funnel", "cloudflare", "print")]
    [string]$Mode = "serve"
)

$ErrorActionPreference = "Stop"
$Port = if ($env:SAW_PORT) { $env:SAW_PORT } else { "8765" }
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
Write-Host ""

$health = Get-Health
if (-not $health) {
    Write-Host "Nothing is listening on $Local."
    Write-Host "In another PowerShell window run:  .\run_server.ps1"
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
        Write-Host "Phone URL (after serve/funnel): https://$hostName/phone"
    }
    Write-Host "Desktop: $Local/phone"
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
    & tailscale funnel --bg $Port
} else {
    Write-Host "Enabling Tailscale Serve (tailnet-only HTTPS) -> $Local"
    & tailscale serve --bg $Port
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "tailscale $Mode failed (exit $LASTEXITCODE)."
    Write-Host "Try the older syntax:"
    Write-Host "  tailscale serve --https=443 $Local"
    Write-Host "  tailscale funnel --https=443 $Local"
    exit $LASTEXITCODE
}

Write-Host ""
& tailscale serve status
Write-Host ""
$hostName = Get-TailscaleHost
if ($hostName) {
    Write-Host "On the iPhone (Safari):"
    Write-Host "  https://$hostName/phone"
} else {
    Write-Host "On the iPhone open https://<your-machine>.<tailnet>.ts.net/phone"
    Write-Host "Hostname: tailscale status"
}
Write-Host ""
Write-Host "Leave this config up for the demo. Reset later with:"
Write-Host "  tailscale serve reset"
Write-Host "  tailscale funnel reset"
