# Opt-in: move WORKDAY_*/JOKE_*/SPEECH_*/BEHAVIORS_* keys from vector-ai\.env
# into pod.conf (only if not already set in pod.conf). Never copies OpenRouter/LLM keys.
#
# Usage:
#   .\windows\migrate-behavior-config.ps1
#   .\windows\migrate-behavior-config.ps1 -EnvPath ... -PodConfPath ...
#
# Creates timestamped backups via pod_conf_io.py.
# Does NOT run automatically on install.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$EnvPath = "",
    [string]$PodConfPath = ""
)
$ErrorActionPreference = "Stop"

function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "WirePodPaths.ps1")
$SharedDir = Resolve-Path (Join-Path $ScriptDir "..\shared")
$ioPy = Join-Path $SharedDir "pod_conf_io.py"

if (-not $EnvPath) {
    $EnvPath = Join-Path $env:USERPROFILE "vector-pod\vector-ai\.env"
}
if (-not $PodConfPath) {
    $PodConfPath = Get-PodConfPath
}

if (-not (Test-Path $EnvPath)) {
    Warn "No .env at $EnvPath — nothing to migrate."
    exit 0
}
if (-not (Test-Path $ioPy)) {
    Write-Host "[X] Missing $ioPy" -ForegroundColor Red
    exit 1
}

$py = Find-Python3
if (-not $py) {
    Write-Host "[X] Python 3 required to migrate (tried venv, py -3, PATH, common install roots)." -ForegroundColor Red
    exit 1
}

$dir = Split-Path -Parent $PodConfPath
if ($dir -and -not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}
if (-not (Test-Path $PodConfPath)) {
    New-Item -ItemType File -Path $PodConfPath -Force | Out-Null
}

Info "Migrating behavior knobs: $EnvPath -> $PodConfPath"
$out = & $py (Resolve-Path $ioPy).Path "migrate-env" $EnvPath $PodConfPath 2>&1
$exit = $LASTEXITCODE
Write-Host $out
if ($exit -ne 0) {
    Write-Host "migrate-env failed" -ForegroundColor Red
    exit $exit
}
$joined = ($out | Out-String)
if ($joined -match 'migrated:\s*\S') {
    Info "Done. Restart vector-ai so supervisor reloads pod.conf."
} else {
    Info "Done (nothing new to migrate)."
}
