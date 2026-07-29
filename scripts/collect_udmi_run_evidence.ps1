[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$EvidenceRoot = (Join-Path $env:USERPROFILE 'Documents\udmi-evidence'),
    [string]$RunId,
    [string]$CapturePath
)

<#
.SYNOPSIS
Collect one completed UDMI validation run into a shareable evidence ZIP.

.DESCRIPTION
Uses only read-only localhost endpoints. It downloads the frozen run export,
redacted run details, issues, generated reports linked to that run (if any),
and the app's masked log bundle. It also copies the newest paired MQTT JSONL
capture unless -CapturePath selects one explicitly.

It never reads or copies the app database, credentials, certificates, private
keys, or the encrypted secrets folder.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-JsonFile {
    param(
        [Parameter(Mandatory)] [object]$Value,
        [Parameter(Mandatory)] [string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText(
        $Path,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Invoke-LocalJson {
    param([Parameter(Mandatory)] [string]$Uri)

    try {
        return Invoke-RestMethod -Uri $Uri -Method Get -Headers @{ Accept = 'application/json' }
    }
    catch {
        throw "Could not read the local Smart Commissioning App at $Uri. Keep the app open and run this as the same Windows user."
    }
}

function Save-LocalDownload {
    param(
        [Parameter(Mandatory)] [string]$Uri,
        [Parameter(Mandatory)] [string]$Path
    )

    try {
        Invoke-WebRequest -Uri $Uri -Method Get -OutFile $Path -Headers @{ Accept = '*/*' } | Out-Null
    }
    catch {
        throw "Could not download local evidence from $Uri."
    }
}

$apiBase = $BaseUrl.TrimEnd('/')
if (-not $apiBase.EndsWith('/api/v1', [System.StringComparison]::OrdinalIgnoreCase)) {
    $apiBase = "$apiBase/api/v1"
}

$terminalStates = @('succeeded', 'failed', 'cancelled')
if ([string]::IsNullOrWhiteSpace($RunId)) {
    $listing = Invoke-LocalJson "$apiBase/validation/runs"
    $candidateRuns = @(
        $listing.runs |
            Where-Object {
                $_.job_type -eq 'udmi_validation' -and $_.status -in $terminalStates
            } |
            Sort-Object -Property updated_at -Descending
    )
    if ($candidateRuns.Count -eq 0) {
        throw 'No completed UDMI validation run was found.'
    }
    $RunId = [string]$candidateRuns[0].run_id
}

$runDetail = Invoke-LocalJson "$apiBase/validation/runs/$RunId"
if ($runDetail.job_type -ne 'udmi_validation') {
    throw "Run '$RunId' is not a UDMI validation run."
}
if ($runDetail.status -notin $terminalStates) {
    throw "Run '$RunId' has not finished yet."
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$safeRunId = ($RunId -replace '[^A-Za-z0-9._-]', '_')
$outputDirectory = Join-Path $EvidenceRoot "udmi-evidence-run_$safeRunId-$stamp"
$outputZip = "$outputDirectory.zip"
New-Item -ItemType Directory -Path $outputDirectory -ErrorAction Stop | Out-Null

$warnings = [System.Collections.Generic.List[string]]::new()

Save-LocalDownload "$apiBase/validation/runs/$RunId/export.json" (Join-Path $outputDirectory 'udmi-run-export.json')
Save-LocalDownload "$apiBase/validation/runs/$RunId/issues" (Join-Path $outputDirectory 'udmi-run-issues.json')
Write-JsonFile $runDetail (Join-Path $outputDirectory 'udmi-run-details.json')

try {
    Save-LocalDownload "$apiBase/logs/bundle" (Join-Path $outputDirectory 'app-logs-masked.zip')
}
catch {
    $warnings.Add('The masked app-log bundle could not be downloaded.')
}

$selectedCapture = $null
if (-not [string]::IsNullOrWhiteSpace($CapturePath)) {
    if (-not (Test-Path -LiteralPath $CapturePath -PathType Leaf)) {
        throw 'The specified paired MQTT capture file was not found.'
    }
    $selectedCapture = Get-Item -LiteralPath $CapturePath
}
else {
    $selectedCapture = Get-ChildItem -LiteralPath $EvidenceRoot -Filter 'paired-capture-*.jsonl' -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTime -Descending |
        Select-Object -First 1
}

if ($null -ne $selectedCapture) {
    Copy-Item -LiteralPath $selectedCapture.FullName -Destination (Join-Path $outputDirectory $selectedCapture.Name) -ErrorAction Stop
}
else {
    $warnings.Add('No paired MQTT capture JSONL file was found.')
}

try {
    $reportListing = Invoke-LocalJson "$apiBase/reports"
    $linkedReports = @(
        $reportListing.reports |
            Where-Object {
                $_.status -eq 'succeeded' -and $_.source_run_ids -contains $RunId
            }
    )
    foreach ($report in $linkedReports) {
        $fileName = [System.IO.Path]::GetFileName([string]$report.file_name)
        if ([string]::IsNullOrWhiteSpace($fileName)) {
            $fileName = "report-$($report.report_id).bin"
        }
        Save-LocalDownload "$apiBase/reports/$($report.report_id)/download" (Join-Path $outputDirectory $fileName)
    }
    if ($linkedReports.Count -eq 0) {
        $warnings.Add('No generated report was linked to this run.')
    }
}
catch {
    $warnings.Add('Linked generated reports could not be collected.')
}

$appExe = Join-Path $env:USERPROFILE 'Downloads\SmartCommissioningApp-windows-portable\SmartCommissioningApp.exe'
$appVersion = $null
if (Test-Path -LiteralPath $appExe -PathType Leaf) {
    $versionInfo = (Get-Item -LiteralPath $appExe).VersionInfo
    $appVersion = [ordered]@{
        product_version = $versionInfo.ProductVersion
        file_version = $versionInfo.FileVersion
    }
}

$details = [ordered]@{
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    run_id = $RunId
    run_status = $runDetail.status
    run_created_at = $runDetail.created_at
    run_updated_at = $runDetail.updated_at
    app_api = $apiBase
    app_version = $appVersion
    paired_capture_file = if ($null -ne $selectedCapture) { $selectedCapture.Name } else { $null }
    warnings = @($warnings)
}
Write-JsonFile $details (Join-Path $outputDirectory 'collection-details.json')

$manifestFiles = @(
    Get-ChildItem -LiteralPath $outputDirectory -File |
        Sort-Object -Property Name |
        ForEach-Object {
            [ordered]@{
                name = $_.Name
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            }
        }
)
$manifest = [ordered]@{
    collected_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    run_id = $RunId
    files = $manifestFiles
}
Write-JsonFile $manifest (Join-Path $outputDirectory 'sha256.json')

Compress-Archive -LiteralPath $outputDirectory -DestinationPath $outputZip -CompressionLevel Optimal -ErrorAction Stop

[pscustomobject]@{
    RunId = $RunId
    EvidenceZip = $outputZip
    PairedCapture = if ($null -ne $selectedCapture) { $selectedCapture.Name } else { $null }
    Warnings = @($warnings)
} | Format-List
