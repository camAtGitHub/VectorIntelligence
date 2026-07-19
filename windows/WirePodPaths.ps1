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
    Get-Content $p -Encoding UTF8 | ForEach-Object {
        $line = $_
        if ($line.Length -gt 0 -and [int][char]$line[0] -eq 0xFEFF) {
            $line = $line.Substring(1)
        }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

# Find any Python 3.x usable for pod_conf_io (not necessarily 3.11).
# Covers venv, py launcher, PATH, and common install roots.
function Find-Python3 {
    $venvPy = Join-Path $env:USERPROFILE "vector-pod\vector-ai\venv\Scripts\python.exe"
    $pathLike = @($venvPy, "python3", "python")
    foreach ($c in $pathLike) {
        if (-not $c) { continue }
        if ($c -match '[\\/]' -and -not (Test-Path $c)) { continue }
        if (Test-Python3Major $c) { return $c }
    }
    foreach ($launcher in @("py")) {
        try {
            $exe = & $launcher -3 -c "import sys; print(sys.executable)" 2>$null
            if ($exe) {
                $exe = "$exe".Trim()
                if ((Test-Path $exe) -and (Test-Python3Major $exe)) { return $exe }
            }
        } catch { }
        try {
            $exe = & $launcher -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($exe) {
                $exe = "$exe".Trim()
                if ((Test-Path $exe) -and (Test-Python3Major $exe)) { return $exe }
            }
        } catch { }
    }
    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        "C:\Python311\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($c in $roots) {
        if ($c -and (Test-Path $c) -and (Test-Python3Major $c)) { return $c }
    }
    return $null
}

function Test-Python3Major {
    param([string]$Exe)
    if (-not $Exe) { return $false }
    try {
        $ver = & $Exe -c "import sys; print(sys.version_info[0])" 2>$null
        $n = 0
        if ([int]::TryParse(("$ver").Trim(), [ref]$n) -and $n -ge 3) {
            return $true
        }
    } catch { }
    return $false
}

# Line-preserving upsert into pod.conf. Only keys in -Set are written/updated;
# comments, blanks, and foreign keys (WORKDAY_*, JOKE_*, …) are never deleted.
# Prefer shared/pod_conf_io.py when Python is available so Windows/Linux share
# one merge implementation; pure PowerShell is the fallback.
function Update-PodConf {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Set,
        [string]$Path = ""
    )
    if (-not $Path) { $Path = Get-PodConfPath }
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force $dir | Out-Null
    }

    $ioPy = Join-Path $PSScriptRoot "..\shared\pod_conf_io.py"
    $py = Find-Python3
    if ($py -and (Test-Path $ioPy)) {
        $argv = @((Resolve-Path $ioPy).Path, "upsert", $Path)
        # Stable order for CLI args (PS 5.1 hashtable order is undefined).
        foreach ($k in ($Set.Keys | Sort-Object)) {
            $argv += ("{0}={1}" -f $k, $Set[$k])
        }
        & $py @argv
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Write-Host "[!] pod_conf_io.py upsert failed (exit $LASTEXITCODE); falling back to PowerShell merge" -ForegroundColor Yellow
    }

    # Pure PowerShell fallback (same semantics as pod_conf_io.upsert_pod_conf_text).
    $remaining = @{}
    $updateKeys = @{}
    foreach ($k in $Set.Keys) {
        $remaining[$k] = [string]$Set[$k]
        $updateKeys[$k] = $true
    }

    $lines = @()
    if (Test-Path $Path) {
        try {
            # UTF-8 with BOM strip (Notepad-written files).
            $bytes = [System.IO.File]::ReadAllBytes($Path)
            $utf8 = New-Object System.Text.UTF8Encoding $false
            if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
                $text = $utf8.GetString($bytes, 3, $bytes.Length - 3)
            } else {
                $text = $utf8.GetString($bytes)
            }
            # Splitlines-equivalent: keep empty lines, drop final empty from trailing NL.
            $lines = @($text -split "`r?`n", -1)
            if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq "") {
                $lines = $lines[0..($lines.Count - 2)]
            }
        } catch {
            throw "refusing to upsert pod.conf: existing file is unreadable ($Path): $_"
        }
    }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') {
            [void]$out.Add($line)
            continue
        }
        if ($line -match '^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $indent = $Matches[1]
            $key = $Matches[2]
            if ($remaining.ContainsKey($key)) {
                [void]$out.Add("${indent}${key}=$($remaining[$key])")
                $remaining.Remove($key)
                continue
            }
            if ($updateKeys.ContainsKey($key)) {
                # Duplicate of a key we already rewrote — drop (match Python).
                continue
            }
        }
        [void]$out.Add($line)
    }
    foreach ($k in ($Set.Keys | Sort-Object)) {
        if ($remaining.ContainsKey($k)) {
            [void]$out.Add("$k=$($remaining[$k])")
            $remaining.Remove($k)
        }
    }
    # UTF-8 without BOM when possible (supervisor strips BOM anyway).
    $content = ($out -join "`n")
    if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
        $content += "`n"
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, $content, $utf8NoBom)
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
