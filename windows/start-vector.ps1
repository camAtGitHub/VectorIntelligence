# start-vector.ps1 - bring Vector's stack up.
# Everything is owned by the VectorPod-Supervisor: it launches
# chipper, and vector-ai, advertises mDNS, and auto-recovers from drops,
# sleep, and IP changes. So "start" is just "start the supervisor".
# Safe to double-click. No admin needed (the task handles elevation).

$ErrorActionPreference = "Continue"
function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

Write-Host "Starting Vector..." -ForegroundColor Cyan

$task = Get-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
if (-not $task) {
    Warn "VectorPod-Supervisor task not found - run install.ps1 first."
    exit 1
}

Start-ScheduledTask -TaskName "VectorPod-Supervisor"
Info "Supervisor starting - Wire-Pod (chipper) + vector-ai (OpenRouter)."

# Give the supervisor a moment, then report what it got up.
Start-Sleep -Seconds 18
# vector-ai's port comes from pod.conf (AI_PORT, default 8090).
$AiPort = 8090
$PodConf = Join-Path $env:USERPROFILE "vector-pod\pod.conf"
if (Test-Path $PodConf) {
    $m = Get-Content $PodConf | Where-Object { $_ -match '^\s*AI_PORT\s*=\s*(\d+)\s*$' } | Select-Object -First 1
    if ($m -match 'AI_PORT\s*=\s*(\d+)') { $AiPort = [int]$Matches[1] }
}
$ok = $true
try { Invoke-RestMethod "http://127.0.0.1:$AiPort/health" -TimeoutSec 5 | Out-Null; Info "vector-ai up." }
catch { Warn "vector-ai not up yet (supervisor will keep retrying)."; $ok = $false }
if (Test-NetConnection 127.0.0.1 -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Info "Wire-Pod (chipper) up."
} else {
    Warn "chipper not up yet (supervisor will keep retrying)."; $ok = $false
}

Write-Host ""
if ($ok) { Write-Host "Ready. Say 'Hey Vector' to chat." -ForegroundColor Green }
else     { Write-Host "Still coming up - give it a few more seconds, then check ~/vector-pod/supervisor.log" -ForegroundColor Yellow }
Write-Host "Stop with stop-vector.ps1 when done."
Write-Host "(Packaged Wire-Pod only? Use setup-companion.ps1 + start-companion.ps1 instead.)"
