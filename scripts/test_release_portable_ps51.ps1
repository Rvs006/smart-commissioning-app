param(
    [string]$PublisherPath = (Join-Path $PSScriptRoot 'release-portable.ps1')
)

$ErrorActionPreference = 'Stop'

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $PublisherPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    throw "release-portable.ps1 does not parse in Windows PowerShell 5.1: $($parseErrors[0].Message)"
}

foreach ($functionName in @(
    'Assert-ReleaseBodyDigests',
    'Get-TrackedReleaseNotesTemplate',
    'Resolve-ReleaseNotesBody',
    'Get-UniqueReleaseAsset',
    'Assert-ReleaseViewUnchanged'
)) {
    $functionAst = $ast.Find(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $functionName
        },
        $true
    )
    if ($null -eq $functionAst) {
        throw "$functionName was not found in release-portable.ps1."
    }
    # Define the publisher's actual guards in this test scope, rather than
    # copying implementations into a test that could drift from production.
    Invoke-Expression $functionAst.Extent.Text
}

$historicalCommit = (& git rev-parse 'v0.1.27^{commit}' 2>$null)
if ($LASTEXITCODE -eq 0 -and $historicalCommit -match '^[0-9a-fA-F]{40}$') {
    $historicalTemplate = Get-TrackedReleaseNotesTemplate `
        -Version 'v0.1.27' -CommitSha $historicalCommit.Trim()
    foreach ($token in @('{{COMMIT}}', '{{EXE_SHA256}}', '{{ZIP_SHA256}}')) {
        if (-not $historicalTemplate.Contains($token)) {
            throw "Tracked v0.1.27 release notes lost required token $token."
        }
    }
}

$exeSha = 'a' * 64
$zipSha = 'b' * 64
$commit = 'c' * 40
$imageReference = "ghcr.io/rvs006/smart-commissioning-api@sha256:$('d' * 64)"
$bodyWithoutImage = "$exeSha`n$zipSha`n$commit"

$rejectedMissingReference = $false
try {
    Assert-ReleaseBodyDigests `
        -Body $bodyWithoutImage `
        -ExeSha256 $exeSha `
        -ZipSha256 $zipSha `
        -CommitSha $commit `
        -ExpectedImageReferences @($imageReference)
}
catch {
    if ($_.Exception.Message -notlike '*exact immutable image reference*') {
        throw
    }
    $rejectedMissingReference = $true
}
if (-not $rejectedMissingReference) {
    throw 'Missing immutable image reference was accepted under Windows PowerShell 5.1.'
}

Assert-ReleaseBodyDigests `
    -Body "$bodyWithoutImage`n$imageReference" `
    -ExeSha256 $exeSha `
    -ZipSha256 $zipSha `
    -CommitSha $commit `
    -ExpectedImageReferences @($imageReference)

$template = "Release $commit`n{{COMMIT}}`n{{EXE_SHA256}}`n{{ZIP_SHA256}}"
$resolved = Resolve-ReleaseNotesBody `
    -Template $template `
    -Version 'v0.1.27' `
    -CommitSha $commit `
    -ExeSha256 $exeSha `
    -ZipSha256 $zipSha
Assert-ReleaseBodyDigests `
    -Body ([string]$resolved.Body) `
    -ExeSha256 $exeSha `
    -ZipSha256 $zipSha `
    -CommitSha $commit `
    -ExpectedBody ([string]$resolved.Body)

$rejectedBodyDrift = $false
try {
    Assert-ReleaseBodyDigests `
        -Body "$($resolved.Body)`nUnreviewed edit" `
        -ExeSha256 $exeSha `
        -ZipSha256 $zipSha `
        -CommitSha $commit `
        -ExpectedBody ([string]$resolved.Body)
}
catch {
    if ($_.Exception.Message -notlike '*exact resolved notes content*') {
        throw
    }
    $rejectedBodyDrift = $true
}
if (-not $rejectedBodyDrift) {
    throw 'A post-publication release-body edit was accepted.'
}

$asset = [pscustomobject]@{
    name = 'asset.zip'
    id = 'asset-node-id'
    size = 123
    digest = "sha256:$exeSha"
}
$initialView = [pscustomobject]@{
    body = [string]$resolved.Body
    url = 'https://github.example/release'
    targetCommitish = $commit
    isDraft = $false
    assets = @($asset)
}
$unchangedView = [pscustomobject]@{
    body = [string]$resolved.Body
    url = 'https://github.example/release'
    targetCommitish = $commit
    isDraft = $false
    assets = @($asset)
}
Assert-ReleaseViewUnchanged `
    -Initial $initialView -Current $unchangedView -ExpectedAssetNames @('asset.zip')

$changedView = [pscustomobject]@{
    body = [string]$resolved.Body
    url = 'https://github.example/release'
    targetCommitish = $commit
    isDraft = $false
    assets = @([pscustomobject]@{
        name = 'asset.zip'
        id = 'replacement-node-id'
        size = 123
        digest = "sha256:$exeSha"
    })
}
$rejectedAssetSwap = $false
try {
    Assert-ReleaseViewUnchanged `
        -Initial $initialView -Current $changedView -ExpectedAssetNames @('asset.zip')
}
catch {
    if ($_.Exception.Message -notlike '*asset identity changed*') {
        throw
    }
    $rejectedAssetSwap = $true
}
if (-not $rejectedAssetSwap) {
    throw 'A release asset replacement was accepted during final re-fetch.'
}

Write-Host 'Windows PowerShell 5.1 release-body regression: OK'
