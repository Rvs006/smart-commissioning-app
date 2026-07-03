#Requires -Version 7.0
<#
.SYNOPSIS
    Local smoke-test harness for the Smart Commissioning App (Windows/PowerShell).

.DESCRIPTION
    Validates a RUNNING stack end-to-end against the real API before you go
    on-site. It hits only safe, side-effect-free paths:

      1. GET  /api/v1/health                      -> 200, status "ok"
      2. GET  /api/v1/ready                        -> 200, status "ready"
      3. GET  /metrics                             -> Prometheus exposition text
      4. GET  /api/v1/configuration                -> demo-project/demo-site snapshot
      5. POST /api/v1/validation/udmi/runs         -> validate the BUNDLED UDMI
                                                      fixture (NO network I/O)
         poll /api/v1/validation/runs/{id}         -> terminal status
         GET  /api/v1/validation/runs/{id}/issues  -> normalized issues
      6. POST /api/v1/discovery/ip/runs (dry_run)  -> a PLAN comes back, NO scan,
                                                      NO authorization required

    It does NOT trigger any real (non-dry-run) active scan or any live broker
    publish. Nothing here touches real BACnet / Redis / Postgres / brokers /
    Docker directly; it only drives the HTTP API of whatever stack you point
    it at.

    Auth: a hosted (AUTH_MODE=api_key) stack needs the shared key; pass -ApiKey
    or set the SC_API_KEY env var and it is sent as X-API-Key on every request.
    A portable/local (AUTH_MODE=local, loopback) stack needs no key; omit it.

.PARAMETER BaseUrl
    Base URL of the running API. Defaults to http://127.0.0.1:8000

.PARAMETER ApiKey
    Shared API key for the hosted profile. Defaults to the SC_API_KEY env var.
    Leave unset for the local/loopback profile.

.EXAMPLE
    pwsh scripts/smoke_local.ps1
    # local / loopback (no key)

.EXAMPLE
    $env:SC_API_KEY = '<key>'; pwsh scripts/smoke_local.ps1
    # hosted, key from env

.EXAMPLE
    pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8080 -ApiKey '<key>'
    # hosted, through the nginx /api proxy on the frontend port

.NOTES
    Exit code: 0 if every check passed, 1 otherwise.
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$ApiKey = $env:SC_API_KEY
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$BaseUrl = $BaseUrl.TrimEnd('/')
$Api = "$BaseUrl/api/v1"

$CurlTimeout  = if ($env:SC_CURL_TIMEOUT)  { [int]$env:SC_CURL_TIMEOUT }  else { 15 }
$PollAttempts = if ($env:SC_POLL_ATTEMPTS) { [int]$env:SC_POLL_ATTEMPTS } else { 30 }
$PollInterval = if ($env:SC_POLL_INTERVAL) { [double]$env:SC_POLL_INTERVAL } else { 1 }

$script:PassCount = 0
$script:FailCount = 0

function Write-Pass([string]$msg) { $script:PassCount++; Write-Host 'PASS ' -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Fail([string]$msg) { $script:FailCount++; Write-Host 'FAIL ' -ForegroundColor Red   -NoNewline; Write-Host $msg }
function Write-Info([string]$msg) { Write-Host "     $msg" -ForegroundColor DarkGray }

# Auth header: empty key -> no header (local mode).
$Headers = @{}
if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
    $Headers['X-API-Key'] = $ApiKey
    Write-Info 'Auth: sending X-API-Key (hosted profile).'
} else {
    Write-Info 'Auth: no API key set, assuming local/loopback profile.'
}

# Invoke-Api: returns a hashtable @{ Status = <int>; Body = <object|null>; Ok = <bool> }
# Uses Invoke-WebRequest so we can read the status code even on non-2xx, and
# parse the JSON body ourselves.
function Invoke-Api {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [string]$Method = 'GET',
        [string]$JsonBody = $null
    )
    $params = @{
        Uri             = $Url
        Method          = $Method
        Headers         = $Headers
        TimeoutSec      = $CurlTimeout
        UseBasicParsing = $true
    }
    if ($null -ne $JsonBody) {
        $params['Body']        = $JsonBody
        $params['ContentType'] = 'application/json'
    }
    try {
        $resp = Invoke-WebRequest @params
        $status = [int]$resp.StatusCode
        $content = $resp.Content
    } catch {
        # Pull the status code + body out of the error response when present.
        $status = 0
        $content = $null
        $exResp = $_.Exception.Response
        if ($null -ne $exResp) {
            try { $status = [int]$exResp.StatusCode } catch { $status = 0 }
            try {
                $stream = $exResp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $content = $reader.ReadToEnd()
            } catch { $content = $null }
        }
    }

    $obj = $null
    if (-not [string]::IsNullOrWhiteSpace($content)) {
        try { $obj = $content | ConvertFrom-Json } catch { $obj = $null }
    }
    return @{ Status = $status; Body = $obj; Raw = $content; Ok = ($status -ge 200 -and $status -lt 300) }
}

Write-Host "Smoke test against $BaseUrl"
Write-Host '--------------------------------------------------'

# --- 1. health -------------------------------------------------------------
$r = Invoke-Api -Url "$Api/health"
if ($r.Status -eq 200 -and $null -ne $r.Body -and $r.Body.status -eq 'ok') {
    Write-Pass 'GET /api/v1/health -> 200 status=ok'
} else {
    Write-Fail "GET /api/v1/health -> HTTP $($r.Status) (expected 200/ok)"
}

# --- 2. ready --------------------------------------------------------------
$r = Invoke-Api -Url "$Api/ready"
$readyStatus = if ($null -ne $r.Body) { $r.Body.status } else { '' }
if ($r.Status -eq 200 -and $readyStatus -eq 'ready') {
    Write-Pass 'GET /api/v1/ready -> 200 status=ready'
} else {
    Write-Fail "GET /api/v1/ready -> HTTP $($r.Status) status='$readyStatus' (expected 200/ready)"
    Write-Info 'A 503 means a required dependency (DB, or Redis in queue mode) is down.'
}

# --- 3. metrics (Prometheus text, app root, NOT under /api/v1) -------------
$r = Invoke-Api -Url "$BaseUrl/metrics"
if ($r.Status -eq 200 -and $r.Raw -match '(?m)^# (HELP|TYPE) |sct_') {
    Write-Pass 'GET /metrics -> 200 Prometheus exposition text'
} else {
    Write-Fail "GET /metrics -> HTTP $($r.Status) (expected 200 Prometheus text)"
}

# --- 4. configuration ------------------------------------------------------
$r = Invoke-Api -Url "$Api/configuration"
if ($r.Status -eq 200 -and $null -ne $r.Body) {
    Write-Pass 'GET /api/v1/configuration -> 200 snapshot returned'
} else {
    Write-Fail "GET /api/v1/configuration -> HTTP $($r.Status) (expected 200)"
}

# --- 5. UDMI validation against the bundled fixture (no network) -----------
$udmiBody = @{
    project_id = 'demo-project'
    site_id    = 'demo-site'
    job_type   = 'udmi_validation'
    parameters = @{ requested_from = 'smoke_local' }
} | ConvertTo-Json -Compress

$runId = $null
$r = Invoke-Api -Url "$Api/validation/udmi/runs" -Method 'POST' -JsonBody $udmiBody
if ($r.Status -eq 200 -and $null -ne $r.Body -and $r.Body.run_id) {
    $runId = $r.Body.run_id
    Write-Pass "POST /api/v1/validation/udmi/runs -> 200 run_id=$runId"
} else {
    Write-Fail "POST /api/v1/validation/udmi/runs -> HTTP $($r.Status) (expected 200)"
}

if ($runId) {
    # Poll to a terminal status. Inline (portable) is immediate; a queued
    # (hosted) run is processed by the worker, so we poll either way.
    $runStatus = ''
    for ($i = 0; $i -lt $PollAttempts; $i++) {
        $r = Invoke-Api -Url "$Api/validation/runs/$runId"
        if ($r.Status -eq 200 -and $null -ne $r.Body) {
            $runStatus = $r.Body.status
            if ($runStatus -in @('succeeded', 'failed', 'cancelled')) { break }
        }
        Start-Sleep -Seconds $PollInterval
    }
    if ($runStatus -eq 'succeeded') {
        Write-Pass 'validation run reached terminal status=succeeded'
    } else {
        Write-Fail "validation run did not succeed (status='$runStatus' after $PollAttempts polls)"
    }

    $r = Invoke-Api -Url "$Api/validation/runs/$runId/issues"
    if ($r.Status -eq 200 -and $null -ne $r.Body -and ($r.Body.PSObject.Properties.Name -contains 'issues')) {
        Write-Pass 'GET /api/v1/validation/runs/{id}/issues -> 200 issues returned'
    } else {
        Write-Fail "GET /api/v1/validation/runs/{id}/issues -> HTTP $($r.Status) (expected 200)"
    }
} else {
    Write-Fail 'validation run polling skipped (no run_id)'
}

# --- 6. DRY-RUN IP discovery (no scan, no authorization needed) ------------
$ipBody = @{
    project_id = 'demo-project'
    site_id    = 'demo-site'
    job_type   = 'ip_discovery'
    parameters = @{ dry_run = $true; cidr = '192.0.2.0/30'; ports = @(47808, 1883) }
} | ConvertTo-Json -Compress

$discRunId = $null
$r = Invoke-Api -Url "$Api/discovery/ip/runs" -Method 'POST' -JsonBody $ipBody
if ($r.Status -eq 200 -and $null -ne $r.Body -and $r.Body.run_id) {
    $discRunId = $r.Body.run_id
    Write-Pass "POST /api/v1/discovery/ip/runs (dry_run) -> 200 run_id=$discRunId"
} else {
    Write-Fail "POST /api/v1/discovery/ip/runs (dry_run) -> HTTP $($r.Status) (expected 200)"
}

if ($discRunId) {
    $discStatus = ''
    $planTargetCount = $null
    for ($i = 0; $i -lt $PollAttempts; $i++) {
        $r = Invoke-Api -Url "$Api/discovery/runs/$discRunId"
        if ($r.Status -eq 200 -and $null -ne $r.Body) {
            $discStatus = $r.Body.status
            if ($discStatus -in @('succeeded', 'failed', 'cancelled')) { break }
        }
        Start-Sleep -Seconds $PollInterval
    }
    # The plan lives at result_summary.dry_run_plan with a positive target_count.
    $plan = $null
    if ($r.Status -eq 200 -and $null -ne $r.Body -and $null -ne $r.Body.result_summary) {
        if ($r.Body.result_summary.PSObject.Properties.Name -contains 'dry_run_plan') {
            $plan = $r.Body.result_summary.dry_run_plan
            $planTargetCount = $plan.target_count
        }
    }
    if ($discStatus -eq 'succeeded' -and $null -ne $plan -and [int]$planTargetCount -gt 0) {
        Write-Pass "dry-run IP discovery returned a plan (target_count=$planTargetCount, no scan)"
    } else {
        Write-Fail "dry-run IP discovery did not return a plan (status='$discStatus', target_count='$planTargetCount')"
    }
} else {
    Write-Fail 'dry-run IP discovery polling skipped (no run_id)'
}

# --- summary ---------------------------------------------------------------
Write-Host '--------------------------------------------------'
$total = $script:PassCount + $script:FailCount
if ($script:FailCount -eq 0) {
    Write-Host "SMOKE PASSED  $($script:PassCount)/$total checks OK" -ForegroundColor Green
    exit 0
} else {
    Write-Host "SMOKE FAILED  $($script:PassCount) passed, $($script:FailCount) failed (of $total)" -ForegroundColor Red
    exit 1
}
