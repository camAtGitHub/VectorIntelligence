# dump-vector-ai-state.ps1 — GET every read-only vector-ai endpoint and dump bodies.
#
# Usage:
#   .\dump-vector-ai-state.ps1
#   .\dump-vector-ai-state.ps1 -Port 8090
#   .\dump-vector-ai-state.ps1 -BaseUrl http://127.0.0.1:8090
#   .\dump-vector-ai-state.ps1 -SessionId default -OutFile $env:TEMP\vector-ai-dump.json
#   .\dump-vector-ai-state.ps1 -OpenApi
#
# Resolves AI_PORT from (in order): -Port / -BaseUrl, $env:AI_PORT, ~/vector-pod/pod.conf, 8090.
# Pass -OpenApi to also discover GETs from /openapi.json and include it in the dump.

[CmdletBinding()]
param(
    [string]$BaseUrl = "",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 0,
    [string]$SessionId = "default",
    [string]$OutFile = "",
    [int]$TimeoutSec = 8,
    [switch]$OpenApi
)

$ErrorActionPreference = "Continue"
function Info ($m) { Write-Host "[+] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Err  ($m) { Write-Host "[x] $m" -ForegroundColor Red }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WirePodPaths = Join-Path $ScriptDir "WirePodPaths.ps1"
if (Test-Path $WirePodPaths) {
    . $WirePodPaths
}

function Resolve-AiBaseUrl {
    param([string]$Base, [string]$HostName, [int]$Port)
    if ($Base) { return $Base.TrimEnd("/") }

    $p = $Port
    if ($p -le 0 -and $env:AI_PORT -match '^\d+$') {
        $p = [int]$env:AI_PORT
    }
    if ($p -le 0 -and (Get-Command Read-PodConf -ErrorAction SilentlyContinue)) {
        $conf = Read-PodConf
        if ($conf.ContainsKey("AI_PORT") -and $conf["AI_PORT"] -match '^\d+$') {
            $p = [int]$conf["AI_PORT"]
        }
    }
    if ($p -le 0) {
        $podConf = Join-Path $env:USERPROFILE "vector-pod\pod.conf"
        if (Test-Path $podConf) {
            $m = Get-Content $podConf -ErrorAction SilentlyContinue |
                Where-Object { $_ -match '^\s*AI_PORT\s*=\s*(\d+)\s*$' } |
                Select-Object -First 1
            if ($m -match 'AI_PORT\s*=\s*(\d+)') { $p = [int]$Matches[1] }
        }
    }
    if ($p -le 0) { $p = 8090 }
    return "http://${HostName}:${p}"
}

function Add-UniquePath {
    param([System.Collections.Generic.List[string]]$List, [string]$Path)
    if (-not $List.Contains($Path)) { [void]$List.Add($Path) }
}

function Invoke-GetJson {
    param(
        [string]$Url,
        [int]$TimeoutSec
    )
    $result = [ordered]@{
        url         = $Url
        http_status = 0
        body        = $null
        body_text   = $null
        error       = $null
    }
    try {
        # Prefer Invoke-WebRequest so we keep status + raw content for non-JSON too.
        $resp = Invoke-WebRequest -Uri $Url -Method GET -TimeoutSec $TimeoutSec `
            -Headers @{ Accept = "application/json" } -UseBasicParsing
        $result.http_status = [int]$resp.StatusCode
        $text = [string]$resp.Content
        if ($text) {
            try {
                $result.body = $text | ConvertFrom-Json
            } catch {
                $result.body_text = $text
            }
        }
    } catch {
        $ex = $_.Exception
        $status = 0
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = 0 }
        }
        # PowerShell 7+ HttpResponseException
        if ($status -eq 0 -and $ex.Response -and $ex.Response.StatusCode) {
            try { $status = [int]$ex.Response.StatusCode.value__ } catch { }
        }
        $result.http_status = $status
        $result.error = "$ex"
        # Try to read error body
        try {
            $stream = $_.Exception.Response.GetResponseStream()
            if ($stream) {
                $reader = New-Object System.IO.StreamReader($stream)
                $errText = $reader.ReadToEnd()
                $reader.Close()
                if ($errText) {
                    try { $result.body = $errText | ConvertFrom-Json }
                    catch { $result.body_text = $errText }
                }
            }
        } catch { }
    }
    return $result
}

function Format-JsonPretty {
    param($Object)
    if ($null -eq $Object) { return "(null)" }
    try {
        return ($Object | ConvertTo-Json -Depth 100)
    } catch {
        return "$Object"
    }
}

$Base = Resolve-AiBaseUrl -Base $BaseUrl -HostName $HostName -Port $Port
$fetchedAt = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")

Write-Host "Vector AI state dump" -ForegroundColor Cyan
Write-Host "  base:       $Base"
Write-Host "  session_id: $SessionId"
Write-Host "  fetched:    $fetchedAt"
Write-Host ""

# Known GET routes (kept in sync with routes/* + test_service_modules EXPECTED_ROUTE_METHODS).
$paths = New-Object System.Collections.Generic.List[string]
@(
    "/health"
    "/v1/soul"
    "/v1/mood"
    "/v1/memory/list"
    "/v1/state/face"
    "/v1/ambient/state"
    "/v1/chat/session?session_id=$SessionId"
    "/v1/behaviors/state"
) | ForEach-Object { Add-UniquePath $paths $_ }

# OpenAPI discovery + dump — only when -OpenApi is passed.
if ($OpenApi) {
    try {
        $oa = Invoke-GetJson -Url "$Base/openapi.json" -TimeoutSec $TimeoutSec
        if ($oa.http_status -eq 200 -and $oa.body -and $oa.body.paths) {
            foreach ($prop in $oa.body.paths.PSObject.Properties) {
                $path = $prop.Name
                $methods = $prop.Value
                $hasGet = $false
                foreach ($m in $methods.PSObject.Properties) {
                    if ($m.Name -ieq "get") { $hasGet = $true; break }
                }
                if ($hasGet -and ($path -notmatch '\{')) {
                    Add-UniquePath $paths $path
                }
            }
            Info "Discovered GET paths from OpenAPI."
        }
    } catch {
        Warn "OpenAPI discovery skipped: $_"
    }
    Add-UniquePath $paths "/openapi.json"
}

$results = [ordered]@{}
$fails = 0

foreach ($path in $paths) {
    $url = "$Base$path"
    $r = Invoke-GetJson -Url $url -TimeoutSec $TimeoutSec
    $results[$path] = $r
}

# Expand behavior detail GETs from /v1/behaviors/state
$behaviorIds = New-Object System.Collections.Generic.List[string]
$stateEntry = $results["/v1/behaviors/state"]
if ($stateEntry -and $stateEntry.http_status -eq 200 -and $stateEntry.body -and $stateEntry.body.behaviors) {
    $behaviors = $stateEntry.body.behaviors
    foreach ($prop in $behaviors.PSObject.Properties) {
        [void]$behaviorIds.Add($prop.Name)
    }
}
if ($behaviorIds.Count -eq 0) {
    @("workday", "joke_idle") | ForEach-Object { [void]$behaviorIds.Add($_) }
    Warn "No behavior ids from /v1/behaviors/state; trying workday + joke_idle."
}

foreach ($id in $behaviorIds) {
    $bp = "/v1/behaviors/$id"
    if (-not $results.Contains($bp)) {
        $r = Invoke-GetJson -Url "$Base$bp" -TimeoutSec $TimeoutSec
        $results[$bp] = $r
    }
}

# Human-readable sections
foreach ($path in $results.Keys) {
    $r = $results[$path]
    $code = $r.http_status
    if ($code -eq 200) {
        Write-Host "=== GET $path  (HTTP $code) ===" -ForegroundColor Green
    } elseif ($code -eq 404) {
        Write-Host "=== GET $path  (HTTP $code) ===" -ForegroundColor Yellow
        $fails++
    } else {
        Write-Host "=== GET $path  (HTTP $code) ===" -ForegroundColor Red
        $fails++
    }
    if ($null -ne $r.body) {
        Write-Host (Format-JsonPretty $r.body)
    } elseif ($r.body_text) {
        Write-Host $r.body_text
    } elseif ($r.error) {
        Write-Host $r.error
    } else {
        Write-Host "(empty body)"
    }
    Write-Host ""
}

Info "Fetched $($results.Count) GET endpoint(s). Non-OK responses: $fails."

if ($OutFile) {
    $endpoints = [ordered]@{}
    foreach ($path in $results.Keys) {
        $r = $results[$path]
        $entry = [ordered]@{
            http_status = $r.http_status
        }
        if ($null -ne $r.body) {
            $entry.body = $r.body
        } elseif ($r.body_text) {
            $entry.body_text = $r.body_text
        } else {
            $entry.body = $null
        }
        if ($r.error) { $entry.error = $r.error }
        $endpoints[$path] = $entry
    }
    $doc = [ordered]@{
        meta = [ordered]@{
            base_url       = $Base
            session_id     = $SessionId
            fetched_at     = $fetchedAt
            endpoint_count = $endpoints.Count
        }
        endpoints = $endpoints
    }
    $json = $doc | ConvertTo-Json -Depth 100
    $outDir = Split-Path -Parent $OutFile
    if ($outDir -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    }
    Set-Content -Path $OutFile -Value $json -Encoding UTF8
    Info "Wrote combined dump: $OutFile"
}

$health = $results["/health"]
if (-not $health -or $health.http_status -ne 200) {
    $code = if ($health) { $health.http_status } else { 0 }
    Err "vector-ai health check failed (HTTP $code) at $Base — is the service up?"
    exit 1
}
if ($fails -gt 0) {
    Warn "Some endpoints returned non-200 (often 404 for disabled behaviors)."
}
exit 0
