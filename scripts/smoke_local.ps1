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
      3b. GET /electracom-logo.png                 -> 200 image/png (frontend
                                                      static serving; skipped
                                                      when / is not the bundle)
      4. GET  /api/v1/configuration                -> demo-project/demo-site snapshot
      5. POST /api/v1/validation/udmi/runs         -> validate the BUNDLED UDMI
                                                      fixture (NO network I/O)
         poll /api/v1/validation/runs/{id}         -> terminal status
         GET  /api/v1/validation/runs/{id}/issues  -> normalized issues
      6. POST /api/v1/discovery/ip/runs (dry_run)  -> a PLAN comes back, NO scan,
                                                      NO authorization required

    With -Preflight, three PRE-SITE checks fold in (from the retired
    scripts/phase5_preflight.ps1) — still safe / side-effect-free:

      P1. cert refs (certificates section)         -> non-empty secret:// refs
      P2. POST /api/v1/discovery/mqtt/runs(dry_run)-> plan w/ broker host:port
      P3. TCP connect to broker host:port          -> reachable, NO MQTT bytes

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

.PARAMETER Preflight
    Also run the three pre-site checks (cert refs, dry-run MQTT discovery,
    broker TCP probe) folded in from the retired scripts/phase5_preflight.ps1.

.PARAMETER ProjectId
    -Preflight only: project whose broker the MQTT dry-run resolves.
    Defaults to SC_PROJECT_ID or 'demo-project'.

.PARAMETER SiteId
    -Preflight only: site whose broker the MQTT dry-run resolves.
    Defaults to SC_SITE_ID or 'demo-site'.

.EXAMPLE
    pwsh scripts/smoke_local.ps1
    # local / loopback (no key)

.EXAMPLE
    pwsh scripts/smoke_local.ps1 -Preflight
    # + pre-site checks (cert refs, MQTT dry-run, broker TCP probe)

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
    [string]$ApiKey = $env:SC_API_KEY,
    [switch]$Preflight,
    [string]$ProjectId = $(if ($env:SC_PROJECT_ID) { $env:SC_PROJECT_ID } else { 'demo-project' }),
    [string]$SiteId    = $(if ($env:SC_SITE_ID)    { $env:SC_SITE_ID }    else { 'demo-site' })
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$BaseUrl = $BaseUrl.TrimEnd('/')
$Api = "$BaseUrl/api/v1"

$CurlTimeout  = if ($env:SC_CURL_TIMEOUT)  { [int]$env:SC_CURL_TIMEOUT }  else { 15 }
$PollAttempts = if ($env:SC_POLL_ATTEMPTS) { [int]$env:SC_POLL_ATTEMPTS } else { 30 }
$PollInterval = if ($env:SC_POLL_INTERVAL) { [double]$env:SC_POLL_INTERVAL } else { 1 }
$TcpTimeout   = if ($env:SC_TCP_TIMEOUT)   { [int]$env:SC_TCP_TIMEOUT }   else { 5 }   # -Preflight broker probe

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

# --- 3b. frontend static serving (the ELECTRACOM logo) ---------------------
# Only meaningful when this stack actually serves the built frontend: the
# portable bundle does, but a split-mode dev backend legitimately answers JSON
# at / (main.py root()), so the check is conditional rather than a hard Fail.
# Invoke-WebRequest directly instead of Invoke-Api: Invoke-Api discards response
# headers and would try ConvertFrom-Json on the binary PNG body.
try {
    $rootResp = Invoke-WebRequest -Uri "$BaseUrl/" -Headers $Headers -TimeoutSec $CurlTimeout -UseBasicParsing
    $rootCt = [string]$rootResp.Headers['Content-Type']
    if ($rootCt -like 'text/html*') {
        $logoResp = Invoke-WebRequest -Uri "$BaseUrl/electracom-logo.png" -Headers $Headers -TimeoutSec $CurlTimeout -UseBasicParsing
        $logoStatus = [int]$logoResp.StatusCode
        $logoCt = [string]$logoResp.Headers['Content-Type']
        if ($logoStatus -eq 200 -and $logoCt -like 'image/png*') {
            Write-Pass 'GET /electracom-logo.png -> 200 image/png (frontend static serving)'
        } else {
            Write-Fail "GET /electracom-logo.png -> HTTP $logoStatus content-type='$logoCt' (expected 200 image/png)"
            Write-Info 'text/html here means the SPA fallback swallowed the asset request.'
        }
    } else {
        Write-Info "frontend bundle not served at / (content-type='$rootCt') — logo check skipped (backend-only stack)."
    }
} catch {
    Write-Fail "GET /electracom-logo.png -> request failed ($($_.Exception.Message))"
}

# --- 4. configuration ------------------------------------------------------
$cfg = $null
$r = Invoke-Api -Url "$Api/configuration"
if ($r.Status -eq 200 -and $null -ne $r.Body) {
    $cfg = $r.Body
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

# --- preflight-only checks (-Preflight) --------------------------------------
# Folded in from the retired scripts/phase5_preflight.ps1: cert-ref shape,
# dry-run MQTT discovery, and a TCP-only broker probe. Still SAFE /
# side-effect-free: no scan, no MQTT bytes, no secret material printed.
if ($Preflight) {

    # --- P1. secret:// cert refs (presence + shape; NO material) ------------
    # NOTE (drift kept on purpose): the retired preflight fetched /configuration
    # scoped with ?project_id&site_id and asserted an mqtt section; this script
    # keeps the smoke variant of check 4 (unscoped snapshot), so the cert fields
    # inspected here come from the default snapshot.
    if ($null -ne $cfg -and $null -ne $cfg.certificates -and $null -ne $cfg.certificates.values) {
        $certValues = $cfg.certificates.values
        $secretCount = 0; $valueCount = 0; $empty = @()
        foreach ($field in @('CA Certificate', 'Client Certificate', 'Private Key')) {
            $val = $certValues.$field
            if ([string]::IsNullOrWhiteSpace($val)) { $empty += "'$field'" }
            elseif ($val.StartsWith('secret://')) { $secretCount++ }
            else { $valueCount++ }   # plain path / value — valid TLS config too
        }
        if ($empty.Count -eq 0) {
            Write-Pass "MQTT-TLS cert fields all set ($secretCount secret:// ref, $valueCount path/value)"
            Write-Info 'Confirm live TLS load on site: secret:// refs decrypt server-side; the in-memory CA + temp client-cert load is on-site-untested.'
        } else {
            # Empty cert fields are NOT a hard failure: the broker may be plaintext.
            Write-Info "MQTT-TLS cert field(s) empty: $($empty -join ' ') — OK if this broker is plaintext; set them before a TLS (8883) broker run."
        }
    } else { Write-Fail 'cert-ref check skipped (no configuration snapshot)' }

    # --- P2. DRY-RUN MQTT discovery (plan only; resolves configured broker) -
    $mqttBody = @{ project_id = $ProjectId; site_id = $SiteId; job_type = 'mqtt_discovery'; parameters = @{ dry_run = $true } } | ConvertTo-Json -Compress
    $mqId = $null; $brokerHost = $null; $brokerPort = $null
    $r = Invoke-Api -Url "$Api/discovery/mqtt/runs" -Method 'POST' -JsonBody $mqttBody
    if ($r.Status -eq 200 -and $null -ne $r.Body -and $r.Body.run_id) { $mqId = $r.Body.run_id }
    if ($mqId) {
        $st = ''; $plan = $null
        for ($i = 0; $i -lt $PollAttempts; $i++) {
            $r = Invoke-Api -Url "$Api/discovery/runs/$mqId"
            if ($r.Status -eq 200 -and $null -ne $r.Body) { $st = $r.Body.status; if ($st -in @('succeeded','failed','cancelled')) { break } }
            Start-Sleep -Seconds $PollInterval
        }
        if ($r.Status -eq 200 -and $null -ne $r.Body.result_summary -and ($r.Body.result_summary.PSObject.Properties.Name -contains 'dry_run_plan')) {
            $plan = $r.Body.result_summary.dry_run_plan
            $brokerHost = $plan.broker_host; $brokerPort = $plan.broker_port
        }
        if ($st -eq 'succeeded' -and $null -ne $plan) {
            Write-Pass "dry-run MQTT discovery returned a plan (broker $brokerHost`:$brokerPort, no connection)"
        } else { Write-Fail "dry-run MQTT discovery did not return a plan (status='$st')" }
    } else { Write-Fail "POST /api/v1/discovery/mqtt/runs (dry_run) -> HTTP $($r.Status) (expected 200)" }

    # --- P3. Broker TCP reachability (NO MQTT bytes, no handshake) ----------
    if (-not [string]::IsNullOrWhiteSpace($brokerHost) -and $brokerPort) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $iar = $client.BeginConnect($brokerHost, [int]$brokerPort, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TcpTimeout)) -and $client.Connected) {
                $client.EndConnect($iar)
                Write-Pass "TCP reach $brokerHost`:$brokerPort (socket opened+closed, no MQTT bytes)"
            } else {
                Write-Fail "TCP reach $brokerHost`:$brokerPort FAILED (firewall/route/broker down?)"
            }
        } catch {
            Write-Fail "TCP reach $brokerHost`:$brokerPort FAILED (firewall/route/broker down?)"
        } finally { $client.Close() }
    } else { Write-Fail 'broker TCP probe skipped (no broker host:port from MQTT dry-run plan)' }

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
