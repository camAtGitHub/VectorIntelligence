# setup-companion.ps1 - Install vector-ai ONLY and tie it to an existing
# packaged/prebuilt Wire-Pod on Windows (no chipper rebuild required).
#
# Use this when you already run the official/packaged Wire-Pod and want
# Vector Intelligence's brain (OpenRouter + memory + persona) without replacing
# your Wire-Pod install.
#
# What it does:
#   1. Creates %USERPROFILE%\vector-pod\vector-ai (venv, service, .env, persona)
#   2. Writes pod.conf with EXTERNAL_CHIPPER=1 + WIREPOD_DIR=...
#   3. Copies supervisor (companion mode: only vector-ai)
#   4. Registers VectorPod-Supervisor scheduled task
#   5. Points Wire-Pod knowledge endpoint at vector-ai (apiConfig merge)
#
# Usage (admin PowerShell recommended for the scheduled task):
#   .\windows\setup-companion.ps1
#   .\windows\setup-companion.ps1 -WirePodDir "D:\apps\wire-pod"
#   .\windows\setup-companion.ps1 -AiPort 8090
#
# After setup:
#   1. Edit %USERPROFILE%\vector-pod\vector-ai\.env  (runtime - NOT only repo shared\)
#      OPENROUTER_API_KEY=...
#   2. Start your packaged Wire-Pod as usual
#   3. .\windows\start-companion.ps1
# See NEXT_STEPS.md in the repo root for the full checklist.

#Requires -Version 5.1
[CmdletBinding()]
param(
    # Install root (chipper.exe). Packaged: C:\Program Files\wire-pod
    [string]$WirePodDir = "",
    # Live data (apiConfig, certs, vosk models). Packaged: %APPDATA%\wire-pod
    [string]$DataDir = "",
    [ValidateRange(1, 65535)]
    [int]$AiPort = 8090,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8080
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Re-launching as administrator (scheduled task registration)..." -ForegroundColor Yellow
    $fwd = " -AiPort $AiPort -WebPort $WebPort"
    if ($WirePodDir) { $fwd += " -WirePodDir `"$WirePodDir`"" }
    if ($DataDir)    { $fwd += " -DataDir `"$DataDir`"" }
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"$fwd"
    exit
}

function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir "WirePodPaths.ps1")
$SharedDir   = Resolve-Path (Join-Path $ScriptDir "..\shared")
$InstallRoot = Get-VectorPodRoot
$VectorAIDir = Join-Path $InstallRoot "vector-ai"

Write-Host "`n-- Vector Intelligence companion setup (packaged Wire-Pod) --" -ForegroundColor Cyan

# -- Locate Wire-Pod (install + data may differ on Windows package) -----------
$wp = Find-WirePodDir -Explicit $WirePodDir
if (-not $wp) {
    $wp = "C:\Program Files\wire-pod"
    if (-not (Test-WirePodInstallTree $wp)) {
        Fail "Could not find Wire-Pod install (chipper.exe). Pass -WirePodDir 'C:\Program Files\wire-pod'"
    }
}
$wpData = Find-WirePodDataDir -Explicit $DataDir
if (-not $wpData) {
    $wpData = Join-Path $env:APPDATA "wire-pod"
}
Info "Wire-Pod install: $wp"
Info "Wire-Pod data:    $wpData"
$apiCfg = Get-WirePodApiConfigPath -WirePodDir $wp -DataDir $wpData
if (-not $apiCfg) {
    Warn "apiConfig.json not found under data dir - complete Wire-Pod setup, then run apply-wirepod-config.ps1"
}
else {
    Info "Live apiConfig:  $apiCfg"
}

# -- Python 3.11 ---------------------------------------------------------------
function Find-Python311 {
    foreach ($cmd in @("py", "python", "python3")) {
        try {
            $exe = & $cmd -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
        } catch {}
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}
$Py311 = Find-Python311
if (-not $Py311) {
    Fail "Python 3.11 required. Install from https://www.python.org/downloads/ (3.11.x) and re-run."
}
Info "Python: $Py311"

# -- vector-ai files -----------------------------------------------------------
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
New-Item -ItemType Directory -Force $VectorAIDir | Out-Null
Copy-Item "$SharedDir\vector-ai\service.py"       (Join-Path $VectorAIDir "service.py")       -Force
Copy-Item "$SharedDir\vector-ai\memory.py"        (Join-Path $VectorAIDir "memory.py")        -Force
Copy-Item "$SharedDir\vector-ai\requirements.txt" (Join-Path $VectorAIDir "requirements.txt") -Force
if (-not (Test-Path (Join-Path $VectorAIDir "persona.txt"))) {
    Copy-Item "$SharedDir\vector-ai\persona.txt" (Join-Path $VectorAIDir "persona.txt") -Force
    Info "persona.txt installed - edit for personality."
}
if (-not (Test-Path (Join-Path $VectorAIDir ".env"))) {
    Copy-Item "$SharedDir\vector-ai\.env" (Join-Path $VectorAIDir ".env") -Force
    Info ".env installed - set OPENROUTER_API_KEY."
} else {
    Info ".env already present - not overwriting."
}
Copy-Item "$SharedDir\supervisor.py" (Join-Path $InstallRoot "supervisor.py") -Force

# -- pod.conf (companion mode) -------------------------------------------------
$podConf = Get-PodConfPath
@(
    "WEB_PORT=$WebPort"
    "AI_PORT=$AiPort"
    "EXTERNAL_CHIPPER=1"
    "WIREPOD_DIR=$wp"
    "WIREPOD_DATA_DIR=$wpData"
) | Set-Content -Path $podConf -Encoding UTF8
Info "pod.conf written (EXTERNAL_CHIPPER=1, install + data dirs, AI_PORT=$AiPort)"

# -- venv ----------------------------------------------------------------------
$VenvDir = Join-Path $VectorAIDir "venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $VenvPy) {
    $venvVer = (& $VenvPy -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null)
    if ("$venvVer".Trim() -ne "3.11") {
        Warn "Rebuilding venv on Python 3.11 (was $venvVer)"
        Remove-Item -Recurse -Force $VenvDir
    }
}
if (-not (Test-Path $VenvPy)) {
    Info "Creating venv..."
    & $Py311 -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) { Fail "venv create failed" }
}
Info "Installing Python deps..."
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $VectorAIDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
& $VenvPy -c "import uvicorn, fastapi, httpx, zeroconf, dotenv, pydantic"
if ($LASTEXITCODE -ne 0) { Fail "import check failed" }
Info "vector-ai ready."

# -- Scheduled task (same name as full stack - companion supervisor) -----------
$SupervisorPy = Join-Path $InstallRoot "supervisor.py"
$action    = New-ScheduledTaskAction -Execute $VenvPy -Argument "`"$SupervisorPy`"" -WorkingDirectory $InstallRoot
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$settings.ExecutionTimeLimit = "PT0S"
Register-ScheduledTask -TaskName "VectorPod-Supervisor" -Action $action -Principal $taskPrincipal -Settings $settings -Force | Out-Null
Info "Scheduled task VectorPod-Supervisor registered (companion: vector-ai only)."

# -- Point Wire-Pod knowledge at vector-ai -------------------------------------
if ($apiCfg) {
    Info "Merging knowledge endpoint into Wire-Pod apiConfig..."
    & (Join-Path $ScriptDir "apply-wirepod-config.ps1") -WirePodDir $wp -DataDir $wpData -SkipRestart
} else {
    Warn "Skipped apiConfig merge (file missing). After Wire-Pod setup, run:"
    Warn "  .\apply-wirepod-config.ps1 -DataDir `"$env:APPDATA\wire-pod`""
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " Companion setup complete" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "1. Edit API key:" -ForegroundColor Yellow
Write-Host "     $VectorAIDir\.env"
Write-Host "     OPENROUTER_API_KEY=sk-or-..."
Write-Host "2. Personality (optional):"
Write-Host "     $VectorAIDir\persona.txt"
Write-Host "3. Start packaged Wire-Pod as you already do."
Write-Host "4. Start the brain:"
Write-Host "     .\windows\start-companion.ps1"
Write-Host "   or: .\windows\start-vector.ps1"
Write-Host ""
Write-Host "Wire-Pod must use knowledge provider 'custom' -> http://127.0.0.1:$AiPort/v1"
Write-Host "(apply-wirepod-config sets this when apiConfig exists)."
Write-Host ""
Write-Host "Note: ambient/sensor/face-probe loops need the PATCHED chipper from"
Write-Host "full install.ps1. Packaged Wire-Pod still gets LLM chat + memory via"
Write-Host "vector-ai; rebuild with install.ps1 later if you want those loops."
Write-Host ""
Write-Host "Live install dir (API key / persona / logs):" -ForegroundColor Yellow
Write-Host "  $InstallRoot"
Write-Host "  $VectorAIDir\.env"
Write-Host "  $InstallRoot\supervisor.log"
Write-Host "  $InstallRoot\vector-ai.log"
Write-Host ""
# Elevated "Run as admin" opens a new window that would otherwise close
# immediately - pause so you can read the summary.
Write-Host "Press Enter to close this window..." -ForegroundColor Cyan
try { [void](Read-Host) } catch { Start-Sleep -Seconds 30 }
