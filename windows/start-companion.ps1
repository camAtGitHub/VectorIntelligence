# start-companion.ps1 - start vector-ai (OpenRouter brain) alongside packaged Wire-Pod.
# Does NOT replace or restart your packaged Wire-Pod process.
#
# Order:
#   1. Starts VectorPod-Supervisor in companion mode (vector-ai only)
#   2. Checks health
#   3. Reminds you to run Wire-Pod if :443 is not up

$ErrorActionPreference = "Continue"
function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "WirePodPaths.ps1")

Write-Host "Starting Vector AI companion..." -ForegroundColor Cyan

$conf = Read-PodConf
if (-not ($conf.ContainsKey("EXTERNAL_CHIPPER") -and $conf["EXTERNAL_CHIPPER"] -match '^(1|true|yes|on)$')) {
    Warn "pod.conf is not in companion mode (EXTERNAL_CHIPPER=1)."
    Warn "Run setup-companion.ps1 first, or use start-vector.ps1 for a full VI stack."
}

$task = Get-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
if (-not $task) {
    Warn "VectorPod-Supervisor task not found - run setup-companion.ps1 first."
    exit 1
}

Start-ScheduledTask -TaskName "VectorPod-Supervisor"
Info "Supervisor starting (vector-ai / OpenRouter only)."

$AiPort = 8090
if ($conf.ContainsKey("AI_PORT") -and $conf["AI_PORT"] -match '^\d+$') {
    $AiPort = [int]$conf["AI_PORT"]
}

Start-Sleep -Seconds 8
try {
    $h = Invoke-RestMethod "http://127.0.0.1:$AiPort/health" -TimeoutSec 5
    Info "vector-ai up - model=$($h.model) api_key_set=$($h.api_key_set) base=$($h.llm_base)"
    if (-not $h.api_key_set) {
        Warn "OPENROUTER_API_KEY is empty - edit vector-pod\vector-ai\.env"
    }
} catch {
    Warn "vector-ai not up yet - check $env:USERPROFILE\vector-pod\vector-ai.log"
}

if (Test-NetConnection 127.0.0.1 -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Info "Wire-Pod (port 443) is listening - good."
} else {
    Warn "Nothing on port 443 - start your packaged Wire-Pod now."
    $wp = Find-WirePodDir
    if ($wp) {
        Write-Host "    Wire-Pod tree: $wp" -ForegroundColor Yellow
        $exe = @(
            (Join-Path $wp "chipper\chipper-whisper.exe"),
            (Join-Path $wp "chipper\chipper.exe"),
            (Join-Path $wp "wire-pod.exe"),
            (Join-Path $wp "start.bat"),
            (Join-Path $wp "start.cmd")
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($exe) {
            Write-Host "    Found launcher: $exe" -ForegroundColor Yellow
            Write-Host "    Start it the same way you usually run Wire-Pod." -ForegroundColor Yellow
        }
    }
}

Write-Host ""
Write-Host "Ready when both vector-ai and Wire-Pod are up. Say 'Hey Vector'." -ForegroundColor Green
Write-Host "Stop brain: .\stop-vector.ps1  (does not stop packaged Wire-Pod)" -ForegroundColor Cyan
