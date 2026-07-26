# install.ps1 - One-time Windows setup for the Vector AI stack.
#
# Installs and configures everything in-place on this machine:
#   - Go, Python, Git, MSYS2 (mingw, for cgo) via winget
#   - Wire-Pod (cloned from upstream, our patches applied, built locally)
#   - vector-ai Python service (FastAPI proxy -> OpenRouter by default)
#   - Windows Firewall rules and Scheduled Tasks for daily start/stop
#
# LLM: OpenRouter (set OPENROUTER_API_KEY in vector-ai\.env after install).
# Local Ollama is optional/legacy (USE_LOCAL_OLLAMA=1) - not installed by default.
#
# Run from an elevated (admin) PowerShell:  .\install.ps1
# After install completes:  daily use is start-vector.ps1 / stop-vector.ps1

#Requires -Version 5.1
[CmdletBinding()]
param(
    # Wire-Pod's web UI / config-server port (its WEBSERVER_PORT). Default 8080.
    # Saved to pod.conf so the supervisor, setup scripts and firewall agree on it.
    [ValidateRange(1, 65535)]
    [int]$WebPort = 9080,
    # vector-ai's localhost port. Default 8090 - deliberately not 8000, which
    # too many other tools squat on. Saved to pod.conf (AI_PORT); the
    # supervisor, chipper and apply-wirepod-config all read it from there.
    [ValidateRange(1, 65535)]
    [int]$AiPort = 8090
)
$ErrorActionPreference = "Stop"

# -- Self-elevate if not running as admin --------------------------------------
# $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
# if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # Write-Host "Re-launching as administrator..." -ForegroundColor Yellow
    # # Forward port flags only when the user actually passed them, so the
    # # elevated run can distinguish an explicit choice from the default (see
    # # Pod config).
    # $fwd = ""
    # if ($PSBoundParameters.ContainsKey('WebPort')) { $fwd += " -WebPort $WebPort" }
    # if ($PSBoundParameters.ContainsKey('AiPort'))  { $fwd += " -AiPort $AiPort" }
    # Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"$fwd"
    # exit
# }

# -- Paths ---------------------------------------------------------------------
# The directory holding install.ps1
$WindowsDir  = "C:\APPS\VectorAI\windows"
$ScriptDir = if ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    "$WindowsDir"
}
$SharedDir   = Resolve-Path (Join-Path $ScriptDir "..\shared")
$InstallRoot = Join-Path $env:USERPROFILE "vector-pod"
$WirePodDir  = Join-Path $InstallRoot "wire-pod"
$VectorAIDir = Join-Path $InstallRoot "vector-ai"
$VoskDir     = Join-Path $WirePodDir  "chipper\vosk"

# -- Pinned upstream commits ---------------------------------------------------
# Our patch scripts are written against these exact revisions. Bumping them
# is a deliberate, re-test-everything decision - never float to HEAD, or a
# future upstream change will silently break the patches for new installers.
$WirePodCommit = "11e7b22095166ed35765e88a8a10ed3a6ce49d5c"
$WhisperCommit = "60cd96acff3a72895cb9ae9cbabe9de21b1e9125"
$SdkCommit     = "62168f3595d67ae0bf24103a9fe1fc5f2eb9b85c"

function Step    ($msg) { Write-Host "`n-- $msg --" -ForegroundColor Cyan }
function Info    ($msg) { Write-Host "[+] $msg"     -ForegroundColor Green }
function Warn    ($msg) { Write-Host "[!] $msg"     -ForegroundColor Yellow }
function Fail    ($msg) { Write-Host "[X] $msg"     -ForegroundColor Red; exit 1 }
function CmdExists ($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

# Runs a patch script and aborts the install if it doesn't apply cleanly. A
# silently-skipped patch still compiles and would ship a broken binary - e.g. a
# re-introduced gRPC connection leak that drops Vector after a few voice
# commands - so a non-zero exit must be fatal. ($ErrorActionPreference="Stop"
# does NOT trap a native exe's exit code, so this has to be explicit; the Linux
# installer gets the same guarantee for free from `set -e`.)
function Patch ($script, $target) {
    python $script $target
    if ($LASTEXITCODE -ne 0) {
        Fail "Patch failed to apply: $(Split-Path -Leaf $script) (exit $LASTEXITCODE). The pinned Wire-Pod/SDK source has likely drifted from the patch anchor; refusing to build with a patch silently missing."
    }
}

# Detects real installs vs. Microsoft Store "open the Store" stubs (which are
# 0-byte placeholders that PowerShell finds via PATH). A real install resolves
# to a binary at least a few KB in size. Avoids running --version which has
# inconsistent syntax across tools (go uses `go version`, etc.).
function CmdWorks ($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    if (-not (Test-Path $cmd.Source)) { return $false }
    $size = (Get-Item $cmd.Source).Length
    return $size -gt 1024
}

# -- 1. Prerequisites via winget -----------------------------------------------
Step "Prerequisites"

if (-not (CmdExists "winget")) {
    Fail "winget is not available. Install 'App Installer' from the Microsoft Store first."
}

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

function Install-IfMissing ($cmd, $id, $label) {
    Refresh-Path
    if (CmdWorks $cmd) { Info "$label already installed."; return }
    Info "Installing $label via winget..."
    winget install --id $id --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    Refresh-Path
    # winget can return non-zero for "already installed, no upgrade available"
    # and other benign reasons. Re-check by actually running the command.
    if (-not (CmdWorks $cmd)) { Fail "Failed to install $label - command still not working after winget." }
    Info "$label installed."
}

# Resolves a real Python 3.11 interpreter (path to python.exe), or $null.
# We pin to 3.11 on purpose: the vector-ai deps (pydantic-core 2.20.1 and
# friends) publish cp311 wheels but none for 3.12/3.13/3.14, so a venv built
# on any newer Python forces a Rust/C source build that fails on machines
# without a compiler - that's the "No module named uvicorn" crash-loop users
# hit. A newer python already on PATH does NOT satisfy this requirement.
function Find-Python311Exe {
    if (CmdExists "py") {
        $exe = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path "$exe".Trim())) { return "$exe".Trim() }
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe"),
        "C:\Python311\python.exe"
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

Install-IfMissing "go"     "GoLang.Go"             "Go"
# Python 3.11 specifically (see Find-Python311Exe). A newer python on PATH must
# NOT short-circuit this, so we check for 3.11 itself rather than "any python".
if (-not (Find-Python311Exe)) {
    Info "Installing Python 3.11 via winget..."
    winget install --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    Refresh-Path
}
$Py311 = Find-Python311Exe
if (-not $Py311) {
    Fail "Python 3.11 is required but could not be found or installed. Install it from https://www.python.org/downloads/ (3.11.x) and re-run install.ps1."
}
Info "Python 3.11 ready: $Py311"
Install-IfMissing "git"    "Git.Git"               "Git"

# MSYS2 provides the mingw-w64 toolchain we need for cgo (Wire-Pod's VOSK +
# Opus bindings both go through cgo).
if (-not (Test-Path "C:\msys64\mingw64\bin\gcc.exe")) {
    Info "Installing MSYS2 (provides mingw64 toolchain for cgo)..."
    winget install --id MSYS2.MSYS2 --silent --accept-package-agreements --accept-source-agreements
    if (-not (Test-Path "C:\msys64\mingw64\bin\gcc.exe")) {
        Info "Updating MSYS2 base..."
        & "C:\msys64\usr\bin\bash.exe" -lc "pacman -Syu --noconfirm" | Out-Host
    }
}
# These are all required for the chipper build: gcc (compiler), pkgconf
# (Opus binding probes it), opus/opusfile (Vector's audio codec).
Info "Ensuring mingw-w64 toolchain packages are installed..."
# & "C:\msys64\usr\bin\bash.exe" -lc "pacman -S --noconfirm --needed mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf mingw-w64-x86_64-opus mingw-w64-x86_64-opusfile" | Out-Host
$MingwBin = "C:\msys64\mingw64\bin"
if (-not (Test-Path "$MingwBin\gcc.exe"))     { Fail "mingw64 gcc not found at $MingwBin\gcc.exe" }
if (-not (Test-Path "$MingwBin\pkgconf.exe")) { Fail "pkgconf not found at $MingwBin\pkgconf.exe" }
Info "mingw-w64 toolchain ready at $MingwBin"

# Refresh PATH so newly installed tools are visible to this session.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + $MingwBin

# -- 2. Wire-Pod: clone (must come before VOSK so the wire-pod dir doesn't ----
#       already exist when git clone tries to create it)
Step "Wire-Pod clone"
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
if (-not (Test-Path "$WirePodDir\.git")) {
    write-host "\$WirePodDir is $WirePodDir"
    Info "Cloning Wire-Pod..."
    # git clone "https://github.com/kercre123/wire-pod" $WirePodDir
    write-host "Just kidding didnt clone anything"
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed." }
}
# Pin to the exact commit our patches are written against (see $WirePodCommit).
Info "Checking out pinned Wire-Pod commit..."
Push-Location $WirePodDir
# git fetch --quiet origin $WirePodCommit 2>$null
# git checkout --quiet $WirePodCommit
write-host "Just kidding, didnt check anything out..."
$coExit = $LASTEXITCODE
Pop-Location
if ($coExit -ne 0) { Fail "Could not check out pinned Wire-Pod commit $WirePodCommit." }

# -- 4. VOSK Windows binary ----------------------------------------------------
Step "VOSK runtime"
$VoskVersion = "0.3.45"
$VoskZip     = Join-Path $env:TEMP "vosk-win64.zip"
if (-not (Test-Path "$VoskDir\libvosk.dll")) {
    New-Item -ItemType Directory -Force $VoskDir | Out-Null
    Info "Downloading vosk-win64-$VoskVersion..."
    Invoke-WebRequest "https://github.com/alphacep/vosk-api/releases/download/v$VoskVersion/vosk-win64-$VoskVersion.zip" -OutFile $VoskZip
    Expand-Archive -Path $VoskZip -DestinationPath $env:TEMP -Force
    Copy-Item "$env:TEMP\vosk-win64-$VoskVersion\*" $VoskDir -Force
    Remove-Item $VoskZip -Force
    Info "VOSK installed to $VoskDir"
} else {
    Info "VOSK already present."
}

# -- 5. Wire-Pod patches -------------------------------------------------------
Step "Wire-Pod patches skipped...."




# -- Patched vector-go-sdk -----------------------------------------------------
# The upstream SDK opens a gRPC connection per vector.New() but never closes
# it, so every voice query leaks one until the robot's SDK wedges. Pull the
# pinned SDK commit into chipper\third_party, patch in a Close() method, and
# point chipper's go.mod at the local copy via a replace directive.
Step "Patched vector-go-sdk"
$SdkDir = Join-Path $WirePodDir "chipper\third_party\vector-go-sdk"
if (-not (Test-Path "$SdkDir\go.mod")) {
    Info "Cloning vector-go-sdk into chipper\third_party..."
    New-Item -ItemType Directory -Force (Split-Path $SdkDir) | Out-Null
    git clone "https://github.com/fforchino/vector-go-sdk" $SdkDir | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "vector-go-sdk clone failed." }
    Push-Location $SdkDir
    git fetch --quiet origin $SdkCommit 2>$null
    git checkout --quiet $SdkCommit
    $sdkExit = $LASTEXITCODE
    Pop-Location
    if ($sdkExit -ne 0) { Fail "Could not check out pinned vector-go-sdk commit $SdkCommit." }
    # Drop .git - this is now a vendored local module, not a clone to update.
    Remove-Item -Recurse -Force (Join-Path $SdkDir ".git") -ErrorAction SilentlyContinue
}
# add-sdk-close.py is idempotent - safe to run on every install.
Patch "$SharedDir\patches\add-sdk-close.py" (Join-Path $SdkDir "pkg\vector\vector.go")
# Redirect chipper's dependency to the local patched copy.
$ChipperGoMod = Join-Path $WirePodDir "chipper\go.mod"
if ((Get-Content $ChipperGoMod -Raw) -notmatch 'replace github\.com/fforchino/vector-go-sdk') {
    Add-Content $ChipperGoMod "`nreplace github.com/fforchino/vector-go-sdk => ./third_party/vector-go-sdk"
    Info "go.mod now points at the patched vector-go-sdk."
} else {
    Info "go.mod replace directive already present."
}

Info "Installing minimal intent file (come here + go home)..."
Info "Kidding.. keeping intents untouched..."
# Copy-Item "$SharedDir\config\wirepod-intents-en-US.json" (Join-Path $WirePodDir "chipper\intent-data\en-US.json") -Force

Info "Building chipper.exe (a few minutes, downloads Go deps the first time)..."
$env:CGO_ENABLED   = "1"
$env:CC            = "$MingwBin\gcc.exe"
$env:CGO_CFLAGS    = "-I$VoskDir"
$env:CGO_LDFLAGS   = "-L$VoskDir"
$GoBin             = "C:\Program Files\Go\bin"
$env:PATH          = "$GoBin;$MingwBin;$VoskDir;" + $env:PATH
info "$VoskDir is set as: $VoskDir"
Push-Location (Join-Path $WirePodDir "chipper")
& go build -o chipper.exe .\cmd\vosk
$buildExit = $LASTEXITCODE
Pop-Location
if ($buildExit -ne 0) { Fail "chipper build failed (exit $buildExit). See output above." }
# Copy runtime DLLs into the chipper dir so it can launch from a Scheduled Task
# without inheriting MSYS2's PATH. libvosk loads VOSK, the mingw libs are
# gcc/Opus/OpenSSL runtime dependencies of chipper.exe.
$runtimeDlls = @(
    "$VoskDir\libvosk.dll",
    "$MingwBin\libgcc_s_seh-1.dll",
    "$MingwBin\libwinpthread-1.dll",
    "$MingwBin\libstdc++-6.dll",
    "$MingwBin\libopus-0.dll",
    "$MingwBin\libogg-0.dll",
    "$MingwBin\libopusfile-0.dll",
    "$MingwBin\libssl-3-x64.dll",
    "$MingwBin\libcrypto-3-x64.dll"
)
$chipperOut = Join-Path $WirePodDir "chipper"
foreach ($dll in $runtimeDlls) {
    if (Test-Path $dll) { Copy-Item $dll $chipperOut -Force }
}
Info "chipper.exe (VOSK) built (runtime DLLs bundled)."

# -- 4b. Whisper.cpp STT (better accuracy than VOSK) --------------------------
# We build a second chipper binary using the whisper.cpp STT backend. A
# small launch dispatcher picks one at runtime based on the STT_SERVICE env
# var. Whisper is the default (better at names, accents, full sentences);
# falling back to VOSK is a one-line env-var change.
Step "Whisper.cpp STT"

# Install cmake + make in MSYS2 if not present (needed to build libwhisper).
if (-not (Test-Path "$MingwBin\cmake.exe")) {
    Info "Installing mingw cmake + make via MSYS2 pacman..."
    & "C:\msys64\usr\bin\bash.exe" -lc "pacman -S --needed --noconfirm mingw-w64-x86_64-cmake mingw-w64-x86_64-make" | Out-Null
}

$WhisperRepo = Join-Path $WirePodDir "whisper.cpp"
if (-not (Test-Path "$WhisperRepo\.git")) {
    Info "Cloning whisper.cpp..."
    git clone https://github.com/kercre123/whisper.cpp.git $WhisperRepo | Out-Null
}
# Pin whisper.cpp too - its cgo binding lives in the pinned Wire-Pod tree.
Info "Checking out pinned whisper.cpp commit..."
Push-Location $WhisperRepo
# git fetch --quiet origin $WhisperCommit 2>$null
# git checkout --quiet $WhisperCommit
$wcoExit = $LASTEXITCODE
Pop-Location
if ($wcoExit -ne 0) { Fail "Could not check out pinned whisper.cpp commit $WhisperCommit." }

# # clean our build for cuda attempts
Remove-Item "$WhisperRepo\build_go" -Recurse -Force -ErrorAction SilentlyContinue

### START1 Build
$WhisperBuild = Join-Path $WhisperRepo "build_go"


$CMakeExe = "C:\Program Files\CMake\bin\cmake.exe"
$CudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"

if (-not (Test-Path $CMakeExe)) {
    Fail "Current standalone CMake not found at $CMakeExe."
}

if (-not (Test-Path "$CudaRoot\bin\nvcc.exe")) {
    Fail "CUDA compiler not found at $CudaRoot\bin\nvcc.exe."
}

$CudaProps = Join-Path `
    ${env:ProgramFiles} `
    "Microsoft Visual Studio\2022\Community\Msbuild\Microsoft\VC\v170\BuildCustomizations\CUDA 12.6.props"

$CudaTargets = Join-Path `
    ${env:ProgramFiles} `
    "Microsoft Visual Studio\2022\Community\Msbuild\Microsoft\VC\v170\BuildCustomizations\CUDA 12.6.targets"

if (-not (Test-Path $CudaProps) -or -not (Test-Path $CudaTargets)) {
    Fail "CUDA 12.6 Visual Studio integration is missing. Repair the CUDA installation and enable Visual Studio Integration."
}

Info "Building libwhisper with CUDA 12.6..."

Push-Location $WhisperRepo

#  ### ---
#  
#  $WhisperReleaseLibDir = Join-Path $WhisperBuild "src\Release"
#  $GgmlReleaseLibDir    = Join-Path $WhisperBuild "ggml\src\Release"
#  $WhisperBinDir        = Join-Path $WhisperBuild "bin\Release"
#  
#  Info "Checking CUDA whisper import libraries..."
#  
#  $RequiredImportLibraries = @(
#      "$WhisperReleaseLibDir\whisper.lib",
#      "$GgmlReleaseLibDir\ggml.lib",
#      "$GgmlReleaseLibDir\ggml-base.lib",
#      "$GgmlReleaseLibDir\ggml-cpu.lib"
#  )
#  
#  foreach ($Library in $RequiredImportLibraries) {
#      if (-not (Test-Path $Library)) {
#          Fail "Required import library was not produced: $Library"
#      }
#          Info "Found import library: $Library"
#  }

# Add the Visual Studio Release directories to CGo's linker search path.
$env:CGO_LDFLAGS = @(
    "-L`"$WhisperReleaseLibDir`""
    "-L`"$GgmlReleaseLibDir`""
    $env:CGO_LDFLAGS
) -join " "

Info "CGo linker flags: $env:CGO_LDFLAGS"


### ---




## --------------- build build_go dir
$OldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"

# PowerShell 7 only: do not convert native stderr into PowerShell errors.
$HadNativePreference = Test-Path variable:PSNativeCommandUseErrorActionPreference
if ($HadNativePreference) {
    $OldNativePreference = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
}

Info "Running CMakeExe for configure?"

$WhisperBuild = Join-Path $WhisperRepo "build_go"
$CMakeExe = "C:\Program Files\CMake\bin\cmake.exe"
$CudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"

$env:CUDA_PATH = $CudaRoot
$env:CUDA_PATH_V12_6 = $CudaRoot

$ConfigureExitCode = -1
$BuildExitCode = -1

if (-not (Test-Path $CMakeExe)) {
    Fail "CMake was not found at $CMakeExe."
}

if (-not (Test-Path "$CudaRoot\bin\nvcc.exe")) {
    Fail "CUDA 12.6 nvcc was not found under $CudaRoot."
}

if (-not (Test-Path "$WhisperRepo\CMakeLists.txt")) {
    Fail "Whisper source directory is invalid: $WhisperRepo"
}


Info "Building libwhisper with CUDA 12.6..."

# # Do not mkdir this manually. CMake creates it during configuration.
# Remove-Item $WhisperBuild `
#     -Recurse `
#     -Force `
#     -ErrorAction SilentlyContinue

$OldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"

$HadNativePreference = Test-Path variable:PSNativeCommandUseErrorActionPreference
if ($HadNativePreference) {
    $OldNativePreference = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
}

try {
    & $CMakeExe `
        -S $WhisperRepo `
        -B $WhisperBuild `
        -G "Visual Studio 17 2022" `
        -A x64 `
        -T "host=x64,cuda=$CudaRoot" `
        -DCUDAToolkit_ROOT="$CudaRoot" `
        -DCMAKE_CUDA_ARCHITECTURES=61 `
        -DGGML_CUDA=ON `
        -DGGML_CCACHE=OFF `
        -DBUILD_SHARED_LIBS=ON `
        -DWHISPER_BUILD_EXAMPLES=OFF `
        -DWHISPER_BUILD_TESTS=OFF `
        -Wno-deprecated

    $ConfigureExitCode = $LASTEXITCODE
}
finally {
    if ($HadNativePreference) {
        $PSNativeCommandUseErrorActionPreference = $OldNativePreference
    }

    $ErrorActionPreference = $OldErrorActionPreference
}

if ($ConfigureExitCode -ne 0) {
    Fail "whisper.cpp CMake configure failed with exit code $ConfigureExitCode."
}

if (-not (Test-Path "$WhisperBuild\CMakeCache.txt")) {
    Fail "CMake configure did not create $WhisperBuild\CMakeCache.txt."
}


##### -------

$OldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"

if ($HadNativePreference) {
    $OldNativePreference = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
}

try {
    & $CMakeExe `
        --build $WhisperBuild `
        --config Release `
        --parallel 4

    $BuildExitCode = $LASTEXITCODE
}
finally {
    if ($HadNativePreference) {
        $PSNativeCommandUseErrorActionPreference = $OldNativePreference
    }

    $ErrorActionPreference = $OldErrorActionPreference
}

if ($BuildExitCode -ne 0) {
    Fail "whisper.cpp CUDA build failed with exit code $BuildExitCode."
}

$BuiltDlls = Get-ChildItem $WhisperBuild -Recurse -File |
    Where-Object { $_.Name -in @(
        "whisper.dll",
        "libwhisper.dll",
        "ggml.dll",
        "ggml-base.dll",
        "ggml-cpu.dll",
        "ggml-cuda.dll"
    ) }

$BuiltDlls | ForEach-Object {
    Info "Built DLL: $($_.FullName)"
}

#   ## --------------- end build_go dir
#   
#   Info "Running CMakeExe build_go build?"
#   
#   $OldErrorActionPreference = $ErrorActionPreference
#   $ErrorActionPreference = "Continue"
#   
#   $BuildOutput = & $CMakeExe `
#       --build build_go `
#       --config Release `
#       --parallel 4 2>&1
#   
#   $BuildExitCode = $LASTEXITCODE
#   $ErrorActionPreference = $OldErrorActionPreference
#   
#   $BuildOutput | Tee-Object "$WhisperRepo\cuda-build.log"
#   
#   if ($BuildExitCode -ne 0) {
#       Pop-Location
#       Fail "whisper.cpp CUDA build failed. See $WhisperRepo\cuda-build.log."
#   }
#   
#   
#   function Invoke-NativeCommand {
#       param(
#           [Parameter(Mandatory)]
#           [scriptblock]$Command
#       )
#   
#       $OldErrorActionPreference = $ErrorActionPreference
#   
#       try {
#           # Native programs commonly write warnings/progress to stderr.
#           $ErrorActionPreference = "Continue"
#   
#           $Output = & $Command 2>&1
#           $ExitCode = $LASTEXITCODE
#       }
#       finally {
#           $ErrorActionPreference = $OldErrorActionPreference
#       }
#   
#       return @{
#           Output   = $Output
#           ExitCode = $ExitCode
#       }
#   }
#   
#   
#   Info "Running CMakeExe for build_go again?"
#   
#   $OldErrorActionPreference = $ErrorActionPreference
#   $ErrorActionPreference = "Continue"
#   
#   
#   & $CMakeExe --version
#   $ConfigureOutput = & $CMakeExe `
#       -S . `
#       -B build_go `
#       -G "Visual Studio 17 2022" `
#       -A x64 `
#       -T "host=x64,cuda=12.6" `
#       -DCUDAToolkit_ROOT="$CudaRoot" `
#       -DCMAKE_CUDA_ARCHITECTURES=61 `
#       -DGGML_CUDA=ON `
#       -DGGML_CCACHE=OFF `
#       -DBUILD_SHARED_LIBS=ON `
#       -DWHISPER_BUILD_EXAMPLES=OFF `
#       -DWHISPER_BUILD_TESTS=OFF 2>&1
#   
#   $ConfigureExitCode = $LASTEXITCODE
#   $ConfigureOutput | Tee-Object "cuda-configure.log"
#   
#   if ($ConfigureExitCode -ne 0) {
#       Pop-Location
#       Fail "whisper.cpp CUDA configure failed. See $WhisperRepo\cuda-configure.log."
#   }
#   
#   Info "Running CMakeExe for build_go take 3?"
#   & $CMakeExe `
#       --build build_go `
#       --config Release `
#       --parallel 4
#   
#   if ($LASTEXITCODE -ne 0) {
#       Pop-Location
#       Fail "whisper.cpp CUDA build failed."
#   }
#   
#   Pop-Location


### END1 Build 



## START my cuda build steps
# $CudaBuildCustomizations = Join-Path `
#     ${env:ProgramFiles} `
#     "Microsoft Visual Studio\2022\Community\MSBuild\Microsoft\VC\v170\BuildCustomizations"
# 
# Get-ChildItem $CudaBuildCustomizations -Filter "CUDA 12.6.*"
# 
# $WhisperBuild = Join-Path $WhisperRepo "build_go"
# $CudaRoot = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
# 
# $CMakeExe = "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
# $CMakeExe = "C:\Program Files\CMake\bin\cmake.exe"
# & $CMakeExe --version
# 
# $WhisperDllCandidates = @(
#     "$WhisperBuild\bin\Release\whisper.dll",
#     "$WhisperBuild\bin\Release\libwhisper.dll",
#     "$WhisperBuild\bin\whisper.dll",
#     "$WhisperBuild\bin\libwhisper.dll"
# )
# 
# $WhisperDll = $WhisperDllCandidates |
#     Where-Object { Test-Path $_ } |
#     Select-Object -First 1
# 
# if (-not $WhisperDll) {
#     Info "Building libwhisper with NVIDIA CUDA support..."
# 
#     Push-Location $WhisperRepo
# 
#     # # Required when changing from MinGW to the Visual Studio generator.
#     if (Test-Path "build_go") {
#         Remove-Item "build_go" -Recurse -Force
#     }
# 
#     & $CMakeExe `
#         -S . `
#         -B build_go `
#         -G "Visual Studio 17 2022" `
#         -A x64 `
#         -DGGML_CUDA=ON `
#         -DCMAKE_CUDA_ARCHITECTURES=61 `
#         -DBUILD_SHARED_LIBS=ON `
#         -DWHISPER_BUILD_EXAMPLES=OFF `
#         -DWHISPER_BUILD_TESTS=OFF
# 
#     if ($LASTEXITCODE -ne 0) {
#         Pop-Location
#         Fail "whisper.cpp CUDA CMake configure failed."
#     }
# 
#     & $CMakeExe `
#         --build build_go `
#         --config Release `
#         --parallel 4
# 
#     if ($LASTEXITCODE -ne 0) {
#         Pop-Location
#         Fail "whisper.cpp CUDA build failed."
#     }
# 
#     Pop-Location
# } else {
#     Info "Found $WhisperDll, skipping libwhisper build..."
# }
## END my cuda build steps

# # This is the original whisper build step
# $WhisperBuild = Join-Path $WhisperRepo "build_go"
# if (-not (Test-Path "$WhisperBuild\bin\libwhisper.dll")) {
    # Info "Building libwhisper (CPU, ~2-3 min)..."
    # Push-Location $WhisperRepo
    # & "$MingwBin\cmake.exe" -B build_go -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DWHISPER_BUILD_EXAMPLES=OFF -DWHISPER_BUILD_TESTS=OFF | Out-Null
    # if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "whisper.cpp cmake configure failed." }
    # & "$MingwBin\cmake.exe" --build build_go --config Release -j 4 | Out-Null
    # if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "whisper.cpp build failed." }
    # Pop-Location
# } else {
  # Info "Found $WhisperBuild\bin\libwhisper.dll, skipping libwhisper.dll build..."
# } 

# base.en is the default: ~2x faster than small.en on CPU (~0.5s vs ~1.5s
# per utterance) with accuracy still well above VOSK. Switch to small.en
# (setx WHISPER_MODEL small.en /M) if you want maximum accuracy and have
# the CPU headroom, or build the Vulkan GPU backend.


$WhisperReleaseLibDir = Join-Path $WhisperBuild "src\Release"
$GgmlReleaseLibDir    = Join-Path $WhisperBuild "ggml\src\Release"

$WhisperExpectedLibDir = Join-Path $WhisperBuild "src"
$GgmlExpectedLibDir    = Join-Path $WhisperBuild "ggml\src"

Info "Staging Visual Studio import libraries for CGo..."

Copy-Item `
    "$WhisperReleaseLibDir\whisper.lib" `
    "$WhisperExpectedLibDir\whisper.lib" `
    -Force

foreach ($Name in @(
    "ggml.lib",
    "ggml-base.lib",
    "ggml-cpu.lib"
)) {
    Copy-Item `
        (Join-Path $GgmlReleaseLibDir $Name) `
        (Join-Path $GgmlExpectedLibDir $Name) `
        -Force
}




$WhisperModel = Join-Path $WhisperRepo "models\ggml-base.en.bin"
if (-not (Test-Path $WhisperModel)) {
    Info "Downloading Whisper base.en model (~142 MB)..."
    $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
    ## $url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en-q5_1.bin"
    Invoke-WebRequest -Uri $url -OutFile $WhisperModel -UseBasicParsing
}
Info "Whisper model: $([math]::Round((Get-Item $WhisperModel).Length / 1MB, 1)) MB"

Info "Building chipper-whisper.exe..."
$env:CGO_ENABLED   = "1"
$env:CC            = "$MingwBin\gcc.exe"
$env:CGO_CFLAGS    = "-I$WhisperRepo\include -I$WhisperRepo\ggml\include"
$env:CGO_LDFLAGS   = "-L$WhisperRepo\build_go\src -L$WhisperRepo\build_go\ggml\src -lwhisper"
Push-Location (Join-Path $WirePodDir "chipper")
& go build -o chipper-whisper.exe .\cmd\experimental\whisper.cpp
$whisperBuildExit = $LASTEXITCODE
Pop-Location
if ($whisperBuildExit -ne 0) { Fail "chipper-whisper build failed (exit $whisperBuildExit)." }

# Copy whisper runtime DLLs + libgomp (OpenMP) into chipper dir.
$whisperDlls = @(
    "$WhisperRepo\build_go\bin\libwhisper.dll",
    "$WhisperRepo\build_go\bin\ggml.dll",
    "$WhisperRepo\build_go\bin\ggml-base.dll",
    "$WhisperRepo\build_go\bin\ggml-cpu.dll",
    "$MingwBin\libgomp-1.dll"
)
foreach ($dll in $whisperDlls) {
    if (Test-Path $dll) { Copy-Item $dll $chipperOut -Force }
}

Info "chipper-whisper.exe built (the supervisor selects it over the VOSK build)."


