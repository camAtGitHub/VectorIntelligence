# Shared helpers: find Wire-Pod on Windows (packaged install vs source tree).
# Dot-source:  . "$PSScriptRoot\WirePodPaths.ps1"
#
# Packaged Windows Wire-Pod splits paths:
#   Install:  C:\Program Files\wire-pod\chipper\chipper.exe
#   Data:     %APPDATA%\wire-pod\apiConfig.json, certs, jdocs, vosk\models
# Source / VI full install keeps everything under one wire-pod tree.

function Get-PodConfPath {
    Join-Path $env:USERPROFILE "vector-pod\pod.conf"
}

function Get-VectorPodRoot {
    Join-Path $env:USERPROFILE "vector-pod"
}

function Read-PodConf {
    $map = @{}
    $p = Get-PodConfPath
    if (-not (Test-Path $p)) { return $map }
    Get-Content $p | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

function Test-WirePodInstallTree {
    param([string]$Dir)
    if (-not $Dir -or -not (Test-Path $Dir)) { return $false }
    $chipper = Join-Path $Dir "chipper"
    if (-not (Test-Path $chipper)) { return $false }
    return (
        (Test-Path (Join-Path $chipper "chipper.exe")) -or
        (Test-Path (Join-Path $chipper "chipper-whisper.exe")) -or
        (Test-Path (Join-Path $chipper "chipper"))
    )
}

function Test-WirePodDataTree {
    param([string]$Dir)
    if (-not $Dir -or -not (Test-Path $Dir)) { return $false }
    # Live config for packaged Windows lives at data root (not under chipper\).
    if (Test-Path (Join-Path $Dir "apiConfig.json")) { return $true }
    if (Test-Path (Join-Path $Dir "chipper\apiConfig.json")) { return $true }
    if (Test-Path (Join-Path $Dir "jdocs")) { return $true }
    return $false
}

# Back-compat name used by older scripts
function Test-WirePodTree {
    param([string]$Dir)
    return (Test-WirePodInstallTree $Dir) -or (Test-WirePodDataTree $Dir)
}

function Find-WirePodDir {
    param([string]$Explicit = "")
    if ($Explicit -and (Test-WirePodInstallTree $Explicit)) {
        return (Resolve-Path $Explicit).Path
    }
    if ($Explicit -and (Test-WirePodDataTree $Explicit) -and -not (Test-WirePodInstallTree $Explicit)) {
        # User passed data dir by mistake; still accept as install if we can
        # resolve Program Files next to it.
        $pf = Find-WirePodInstallDir
        if ($pf) { return $pf }
    }

    $conf = Read-PodConf
    if ($conf.ContainsKey("WIREPOD_DIR") -and (Test-WirePodInstallTree $conf["WIREPOD_DIR"])) {
        return $conf["WIREPOD_DIR"]
    }
    if ($env:WIREPOD_DIR -and (Test-WirePodInstallTree $env:WIREPOD_DIR)) {
        return $env:WIREPOD_DIR
    }

    return Find-WirePodInstallDir
}

function Find-WirePodInstallDir {
    $candidates = @(
        (Join-Path $env:USERPROFILE "vector-pod\wire-pod"),
        (Join-Path $env:USERPROFILE "wire-pod"),
        "C:\Program Files\wire-pod",
        "C:\Program Files (x86)\wire-pod",
        "C:\wire-pod",
        "C:\WirePod"
    )
    foreach ($c in $candidates) {
        if (Test-WirePodInstallTree $c) { return $c }
    }
    return $null
}

function Find-WirePodDataDir {
    param([string]$Explicit = "")

    if ($Explicit -and (Test-WirePodDataTree $Explicit)) {
        return (Resolve-Path $Explicit).Path
    }

    $conf = Read-PodConf
    if ($conf.ContainsKey("WIREPOD_DATA_DIR") -and (Test-WirePodDataTree $conf["WIREPOD_DATA_DIR"])) {
        return $conf["WIREPOD_DATA_DIR"]
    }
    if ($env:WIREPOD_DATA_DIR -and (Test-WirePodDataTree $env:WIREPOD_DATA_DIR)) {
        return $env:WIREPOD_DATA_DIR
    }

    # Packaged Windows installer (your layout)
    $appData = Join-Path $env:APPDATA "wire-pod"
    if (Test-WirePodDataTree $appData) { return $appData }

    # Source / VI: data co-located with install
    $install = Find-WirePodDir
    if ($install) {
        if (Test-Path (Join-Path $install "chipper\apiConfig.json")) { return $install }
        if (Test-Path (Join-Path $install "apiConfig.json")) { return $install }
    }

    return $null
}

function Get-WirePodApiConfigPath {
    param(
        [string]$WirePodDir = "",
        [string]$DataDir = ""
    )
    # Prefer live data dir (AppData on packaged Windows).
    $data = if ($DataDir) { $DataDir } else { Find-WirePodDataDir }
    if ($data) {
        $p1 = Join-Path $data "apiConfig.json"
        if (Test-Path $p1) { return $p1 }
        $p2 = Join-Path $data "chipper\apiConfig.json"
        if (Test-Path $p2) { return $p2 }
    }
    # Fall back to install tree (source builds)
    $inst = if ($WirePodDir) { $WirePodDir } else { Find-WirePodDir }
    if ($inst) {
        $p3 = Join-Path $inst "chipper\apiConfig.json"
        if (Test-Path $p3) { return $p3 }
        $p4 = Join-Path $inst "apiConfig.json"
        if (Test-Path $p4) { return $p4 }
    }
    return $null
}
