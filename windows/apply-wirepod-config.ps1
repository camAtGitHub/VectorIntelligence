# apply-wirepod-config.ps1 - merge AI config into Wire-Pod's live apiConfig.json
# AFTER first-run setup. Sets knowledge endpoint -> local vector-ai.
#
# Packaged Windows:
#   Install: C:\Program Files\wire-pod   (chipper.exe)
#   Data:    %APPDATA%\wire-pod          (apiConfig.json <- we edit this)
# Source/VI install: both under one wire-pod tree.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$WirePodDir = "",
    [string]$DataDir = "",
    [switch]$SkipRestart
)
$ErrorActionPreference = "Stop"

function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "WirePodPaths.ps1")
$SharedDir = Resolve-Path (Join-Path $ScriptDir "..\shared")
$ConfigSrc = Join-Path $SharedDir "config\wirepod-apiConfig.json"

$wpInstall = Find-WirePodDir -Explicit $WirePodDir
$wpData    = Find-WirePodDataDir -Explicit $DataDir
$ConfigDst = Get-WirePodApiConfigPath -WirePodDir $wpInstall -DataDir $wpData

if (-not $ConfigDst) {
    $conf = Read-PodConf
    $WebPort = 8080
    if ($conf.ContainsKey("WEB_PORT") -and $conf["WEB_PORT"] -match '^\d+$') {
        $WebPort = [int]$conf["WEB_PORT"]
    }
    Write-Host "[!] Could not find live apiConfig.json" -ForegroundColor Yellow
    Write-Host "    Packaged Windows: expect %APPDATA%\wire-pod\apiConfig.json"
    Write-Host "    Complete Wire-Pod setup at http://localhost:$WebPort then re-run."
    Write-Host "    Or pass -DataDir `"$env:APPDATA\wire-pod`""
    exit 1
}

if ($wpInstall) { Info "Wire-Pod install: $wpInstall" }
if ($wpData)    { Info "Wire-Pod data:    $wpData" }
Info "apiConfig:          $ConfigDst"

$python = Join-Path $env:USERPROFILE "vector-pod\vector-ai\venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$podConfPath = Get-PodConfPath
& $python -c @"
import json, sys
src, dst = r'$ConfigSrc', r'$ConfigDst'
with open(src, encoding='utf-8')  as f: our  = json.load(f)
with open(dst, encoding='utf-8')  as f: live = json.load(f)
for k in ('knowledge', 'STT', 'weather'):
    if k in our:
        # Merge knowledge: keep live keys Wire-Pod may need, overlay ours
        if k == 'knowledge' and isinstance(live.get(k), dict) and isinstance(our[k], dict):
            merged = dict(live[k])
            merged.update(our[k])
            live[k] = merged
        else:
            live[k] = our[k]
ai_port = '8090'
try:
    for line in open(r'$podConfPath', encoding='utf-8'):
        k, _, v = line.strip().partition('=')
        if k.strip() == 'AI_PORT' and v.strip().isdigit():
            ai_port = v.strip()
except OSError:
    pass
live.setdefault('knowledge', {})
live['knowledge']['enable'] = True
live['knowledge']['provider'] = 'custom'
live['knowledge']['endpoint'] = f'http://127.0.0.1:{ai_port}/v1'
if not live['knowledge'].get('key'):
    live['knowledge']['key'] = 'placeholder'
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(live, f, indent=2, ensure_ascii=False)
print('Config merged (vector-ai endpoint :' + ai_port + ').')
"@
if ($LASTEXITCODE -ne 0) { Write-Host "Merge failed (need write access to apiConfig?)." -ForegroundColor Red; exit 1 }
Info "Knowledge endpoint -> vector-ai (custom provider)."

# Persist managed companion keys only (upsert — leave WORKDAY/JOKE/etc. alone).
# Never invent EXTERNAL_CHIPPER=1: full-stack installs must stay non-companion.
$confMap = Read-PodConf
$set = @{}
if ($confMap.ContainsKey("WEB_PORT") -and $confMap['WEB_PORT']) {
    $set['WEB_PORT'] = "$($confMap['WEB_PORT'])"
} else {
    $set['WEB_PORT'] = '8080'
}
if ($confMap.ContainsKey("AI_PORT") -and $confMap['AI_PORT']) {
    $set['AI_PORT'] = "$($confMap['AI_PORT'])"
} else {
    $set['AI_PORT'] = '8090'
}
# Only re-assert EXTERNAL_CHIPPER when already present (mirror USE_LOCAL_OLLAMA).
if ($confMap.ContainsKey("EXTERNAL_CHIPPER")) {
    $set['EXTERNAL_CHIPPER'] = "$($confMap['EXTERNAL_CHIPPER'])"
}
if ($wpInstall) { $set['WIREPOD_DIR'] = "$wpInstall" }
if ($wpData)    { $set['WIREPOD_DATA_DIR'] = "$wpData" }
if ($confMap.ContainsKey("USE_LOCAL_OLLAMA")) {
    $set['USE_LOCAL_OLLAMA'] = "$($confMap['USE_LOCAL_OLLAMA'])"
}
New-Item -ItemType Directory -Force (Split-Path (Get-PodConfPath)) | Out-Null
Update-PodConf -Path (Get-PodConfPath) -Set $set
Info "pod.conf updated."

if ($SkipRestart) {
    Info "Config applied (restart skipped)."
    Warn "Restart packaged Wire-Pod so it reloads apiConfig.json."
    exit 0
}

$sup = Get-ScheduledTask -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
if ($sup) {
    Stop-ScheduledTask  -TaskName "VectorPod-Supervisor" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName "VectorPod-Supervisor"
    Info "VectorPod-Supervisor restarted (vector-ai)."
}

Warn "Restart packaged Wire-Pod (systray/quit + start) so knowledge settings load."
