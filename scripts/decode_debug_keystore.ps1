# Decode the shared SAW debug keystore from SAW_DEBUG_KEYSTORE_B64 or a .b64 file.
# Writes a git-ignored keystore and prints only the output path (never the key).
param(
    [string]$B64File = "",
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Out) {
    if ($env:SAW_DEBUG_KEYSTORE_OUT) { $Out = $env:SAW_DEBUG_KEYSTORE_OUT }
    else { $Out = Join-Path $root "android\.secrets\saw-debug.keystore" }
}
$dir = Split-Path -Parent $Out
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir | Out-Null
}
if ($B64File) {
    $b64 = Get-Content -Raw -Path $B64File
} elseif ($env:SAW_DEBUG_KEYSTORE_B64) {
    $b64 = $env:SAW_DEBUG_KEYSTORE_B64
} else {
    throw "Set SAW_DEBUG_KEYSTORE_B64 or pass -B64File"
}
$b64 = ($b64 -replace "\s", "")
if (-not $b64) { throw "empty keystore b64" }
$bytes = [Convert]::FromBase64String($b64)
[IO.File]::WriteAllBytes($Out, $bytes)
Write-Output $Out
