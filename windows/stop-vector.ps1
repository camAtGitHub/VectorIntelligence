# stop-vector.ps1 - shut the Vector Intelligence supervisor down.
# Full install: stops chipper + vector-ai.
# Companion mode (EXTERNAL_CHIPPER=1): stops only vector-ai - never kills
# packaged Wire-Pod's chipper.

$ErrorActionPreference = "SilentlyContinue"
function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "WirePodPaths.ps1")
$conf = Read-PodConf
$external = $conf.ContainsKey("EXTERNAL_CHIPPER") -and ($conf["EXTERNAL_CHIPPER"] -match '^(1|true|yes|on)$')

Write-Host "Stopping Vector Intelligence..." -ForegroundColor Cyan
if ($external) {
    Write-Host "  (companion mode - packaged Wire-Pod will keep running)" -ForegroundColor Cyan
}

# Ask the scheduled task to stop (supervisor gets SIGTERM / process stop).
Stop-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue | Out-Null

# Give the supervisor a moment to exit cleanly, then sweep our processes only.
Start-Sleep -Seconds 4

if (-not $external) {
    Get-Process chipper* -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

# Only kill python that is clearly our stack - avoid noisy CIM errors.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $cli = $_.CommandLine
        if (-not $cli) { return }
        if ($cli -match 'uvicorn service:app|supervisor\.py') {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

Info "Stopped."
if ($external) {
    Write-Host "Brain down. Packaged Wire-Pod left alone. Start again with start-companion.ps1."
} else {
    Write-Host "Stack down. Start again with start-vector.ps1."
}
