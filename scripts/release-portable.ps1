#Requires -Version 5.1
<#
.SYNOPSIS
    Publish a Smart Commissioning App portable release from the CI-built
    artifact, or re-verify an already-published release. Windows PowerShell 5.1
    only (the dev laptop has no pwsh) - keep it 5.1-clean.

.DESCRIPTION
    The "Windows Portable Bundle" workflow (.github/workflows/windows-portable.yml)
    builds + boot-smokes the bundle on a windows-2022 runner and uploads it as the
    artifact 'SmartCommissioningApp-windows-portable'. This script downloads THAT
    artifact archive, verifies it, and attaches it to a GitHub Release - so the
    bytes field engineers download are the exact bytes CI built and booted, with
    the exe SHA-256 recorded in the release notes.

    Engineered around four real Windows PowerShell 5.1 failures hit publishing
    v0.1.15 by hand. Each is a silent-corruption or silent-wrong-asset trap, so
    each has a guard here rather than a comment saying "be careful":

    1. `gh api .../zip > file.zip` CORRUPTS the download. In PS 5.1 the `>`
       operator re-encodes native-command stdout as text (UTF-16 + CRLF
       translation), roughly doubling the size and producing an unopenable zip.
       Binary downloads MUST use Invoke-WebRequest -OutFile, never redirection.

    2. GitHub's artifact-zip endpoint 302-redirects to Azure blob storage, and
       PS 5.1's Invoke-WebRequest FORWARDS the Authorization header across the
       redirect. Azure blob rejects a request that carries both its SAS token and
       a GitHub bearer header (403). So we split the redirect by hand: hit the API
       endpoint with -MaximumRedirection 0 (which makes 5.1 THROW on the 3xx
       instead of following it), read the signed URL out of the exception
       response's Location header, then download that URL with a plain
       Invoke-WebRequest -OutFile and NO auth header.

    3. Never re-zip locally. .NET Framework's
       [IO.Compression.ZipFile]::CreateFromDirectory breaks on >260-char paths
       and writes backslash entry names, giving a zip that extracts wrong (or not
       at all) on other machines. We ship the CI artifact archive itself: bundle
       contents at the zip root, forward-slash entries. This script only ever
       downloads and re-attaches that archive; it does not build one.

    4. Run-list metadata cannot prove which VERSION a run built. Workflow inputs
       (the -Version stamped into the exe) are not queryable from `gh run list`,
       so a green run for v0.1.14 looks identical to one for v0.1.15. We prove
       the version from inside the artifact: README_FIRST.txt carries
       "Version: <v>", written by build.ps1 from the same -Version that stamps the
       exe. Mismatch => wrong run => fail before touching Releases.

.PARAMETER Version
    Release tag, e.g. v0.1.16. Validated against ^v\d+\.\d+\.\d+$.

.PARAMETER RunId
    Optional workflow run id to publish from. When omitted, the newest completed
    + successful "Windows Portable Bundle" workflow_dispatch run on main is used.

.PARAMETER NotesFile
    Markdown release-notes template (required for publishing). Tokens
    {{EXE_SHA256}}, {{ZIP_SHA256}} and {{COMMIT}} are substituted before upload.

.PARAMETER Title
    Release title. Defaults to $Version.

.PARAMETER RepoSlug
    owner/repo. Defaults to Rvs006/smart-commissioning-app.

.PARAMETER VerifyExisting
    Skip publishing. Instead download the already-published release asset for
    -Version and re-verify its digest and contained exe hash. A harmless,
    read-only way to exercise this script end to end.

.EXAMPLE
    # Publish v0.1.16 from the latest green dispatch run:
    powershell -NoProfile -File scripts\release-portable.ps1 -Version v0.1.16 -NotesFile docs\release-notes.md

.EXAMPLE
    # Re-verify what is already published (no mutations):
    powershell -NoProfile -File scripts\release-portable.ps1 -Version v0.1.16 -VerifyExisting
#>
[CmdletBinding(DefaultParameterSetName = 'Publish')]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^v\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(ParameterSetName = 'Publish')]
    [long]$RunId,

    [Parameter(Mandatory, ParameterSetName = 'Publish')]
    [string]$NotesFile,

    [string]$Title,

    [string]$RepoSlug = 'Rvs006/smart-commissioning-app',

    [Parameter(Mandatory, ParameterSetName = 'Verify')]
    [switch]$VerifyExisting
)

$ErrorActionPreference = 'Stop'

# Some GitHub/Azure endpoints refuse pre-TLS1.2; PS 5.1 does not always enable it
# by default. -bor so we add Tls12 without dropping whatever is already enabled.
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# --- constants tied to the workflow + build.ps1 output ---
$WorkflowName = 'Windows Portable Bundle'                       # windows-portable.yml `name:`
$ArtifactName = 'SmartCommissioningApp-windows-portable'        # upload-artifact `name:`
$ZipName      = 'Smart_Commissioning_App_Windows_Portable.zip'  # release asset filename
$ExeEntry     = 'SmartCommissioningApp.exe'                     # root entry in the bundle zip
$ReadmeEntry  = 'README_FIRST.txt'                             # root entry carrying "Version: <v>"
$EntryFloor   = 1000                                           # sanity floor: a real bundle has thousands of entries

if ([string]::IsNullOrWhiteSpace($Title)) { $Title = $Version }

# gh path resolved once, used by every helper.
$script:Gh = $null

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Resolve-Gh {
    # Prefer the machine-wide install (this is how MEMORY records gh here), fall
    # back to PATH. -ErrorAction Stop so a missing gh fails loudly, not later.
    $candidate = Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return (Get-Command gh -ErrorAction Stop).Source
}

function Invoke-Gh {
    # Run gh and fail on non-zero exit. Returns stdout (array of lines); callers
    # that expect JSON join with "`n" before ConvertFrom-Json.
    param(
        [Parameter(Mandatory)][string[]]$GhArgs,
        [string]$What = 'gh command'
    )
    $out = & $script:Gh @GhArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (gh exit $LASTEXITCODE): gh $($GhArgs -join ' ')"
    }
    return $out
}

function Get-RunInfo {
    # Returns @{ Id; HeadSha } for the run to publish from. Auto-locate uses the
    # run list; an explicit -RunId is validated + its head sha fetched via the API
    # (run list does not include an arbitrary run we did not list).
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [long]$RunId,
        [bool]$AutoLocate
    )
    if ($AutoLocate) {
        $raw = Invoke-Gh @(
            'run', 'list',
            '--repo', $RepoSlug,
            '--workflow', $WorkflowName,
            '--branch', 'main',
            '--event', 'workflow_dispatch',
            '--limit', '10',
            '--json', 'databaseId,status,conclusion,headSha'
        ) 'gh run list'
        $runs = ($raw -join "`n") | ConvertFrom-Json
        # gh returns newest first; take the first completed + successful one.
        $match = $runs |
            Where-Object { $_.status -eq 'completed' -and $_.conclusion -eq 'success' } |
            Select-Object -First 1
        if ($null -eq $match) {
            throw "No completed+successful '$WorkflowName' workflow_dispatch run found on main. Dispatch the workflow for $Version first, or pass -RunId explicitly."
        }
        return [pscustomobject]@{ Id = [long]$match.databaseId; HeadSha = [string]$match.headSha }
    }

    # Explicit run: fetch + validate. --jq reshapes to the same field names.
    $raw = Invoke-Gh @(
        'api', "repos/$RepoSlug/actions/runs/$RunId",
        '--jq', '{databaseId: .id, status: .status, conclusion: .conclusion, headSha: .head_sha}'
    ) "gh api run $RunId"
    $run = ($raw -join "`n") | ConvertFrom-Json
    if ($run.status -ne 'completed' -or $run.conclusion -ne 'success') {
        throw "Run $RunId is status='$($run.status)' conclusion='$($run.conclusion)' - refusing to publish from a run that did not complete successfully."
    }
    return [pscustomobject]@{ Id = [long]$run.databaseId; HeadSha = [string]$run.headSha }
}

function Get-ArtifactInfo {
    # Find the portable-bundle artifact on a run. Fails clearly if it is absent
    # (wrong run / build failed before upload) or expired (retention lapsed).
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [Parameter(Mandatory)][long]$RunId
    )
    $raw = Invoke-Gh @('api', "repos/$RepoSlug/actions/runs/$RunId/artifacts") "gh api artifacts for run $RunId"
    $data = ($raw -join "`n") | ConvertFrom-Json
    $art = $data.artifacts | Where-Object { $_.name -eq $ArtifactName } | Select-Object -First 1
    if ($null -eq $art) {
        throw "Run $RunId has no artifact named '$ArtifactName'. Wrong run, or the build failed before the upload step."
    }
    if ($art.expired) {
        throw "Artifact '$ArtifactName' on run $RunId has EXPIRED (retention lapsed). Re-run the workflow to produce a fresh artifact."
    }
    return $art
}

function Get-ArtifactArchive {
    # Download the artifact zip using the redirect-splitting technique (failures
    # #1 and #2 in the header). $ArchiveUrl is the api.github.com .../zip endpoint;
    # it answers 302 to a signed Azure blob URL.
    param(
        [Parameter(Mandatory)][string]$ArchiveUrl,
        [Parameter(Mandatory)][string]$OutFile
    )
    $token = (& $script:Gh auth token)
    if ($LASTEXITCODE -ne 0) { throw "gh auth token failed (exit $LASTEXITCODE) - is gh logged in?" }
    $token = "$token".Trim()

    $authHeaders = @{
        Authorization = "Bearer $token"
        Accept        = 'application/vnd.github+json'
        'User-Agent'  = 'smart-commissioning-release-portable'
    }

    $location = $null
    try {
        # -MaximumRedirection 0 makes PS 5.1 THROW on the 302 rather than follow
        # it with the Authorization header still attached (which Azure rejects).
        # -OutFile is harmless here: on the expected 302 nothing is written; only
        # a direct 200 (which GitHub does not currently send) would write bytes.
        Invoke-WebRequest -Uri $ArchiveUrl -Headers $authHeaders `
            -MaximumRedirection 0 -UseBasicParsing -OutFile $OutFile -ErrorAction Stop
        Write-Host "    endpoint answered 200 directly (no redirect) - archive written"
    }
    catch {
        $resp = $_.Exception.Response
        if ($null -eq $resp) {
            throw "Artifact download failed with no HTTP response: $($_.Exception.Message)"
        }
        $status = [int]$resp.StatusCode
        if ($status -ge 300 -and $status -lt 400) {
            # The signed, pre-authenticated blob URL. Read it straight off the
            # exception response's headers - PS 5.1 exposes it here.
            $location = $resp.Headers['Location']
            if ([string]::IsNullOrWhiteSpace($location)) {
                throw "Artifact endpoint returned redirect $status but no Location header."
            }
        }
        else {
            throw "Artifact endpoint returned HTTP $status (expected a 302 redirect to blob storage)."
        }
    }

    if ($location) {
        # Signed URL already carries its SAS token - send NO auth header, and use
        # -OutFile (never `>`, failure #1) so the binary lands byte-exact.
        Write-Host "    following signed redirect to blob storage (no auth header)"
        Invoke-WebRequest -Uri $location -UseBasicParsing -OutFile $OutFile -ErrorAction Stop
    }

    if (-not (Test-Path -LiteralPath $OutFile)) {
        throw "Download reported success but $OutFile is missing."
    }
}

function Test-BundleZip {
    # Verify the downloaded archive BEFORE anything touches Releases, and extract
    # the exe + readme so we can hash the exe and prove the version. Returns
    # @{ ExeSha256; ExePath; ReadmePath }. Throws on any structural problem.
    param(
        [Parameter(Mandatory)][string]$ZipPath,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$StageDir
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = $zip.Entries

        # Sanity floor: a real bundle (exe + _internal + backend + core + frontend
        # dist) is thousands of entries. A tiny archive means a corrupt or wrong
        # download.
        if ($entries.Count -le $EntryFloor) {
            throw "Bundle zip has only $($entries.Count) entries (expected > $EntryFloor). Likely a corrupt or wrong archive."
        }

        # Entry names must use '/', not '\'. A backslash entry means the archive
        # was re-zipped by .NET on Windows (failure #3) and will extract wrong
        # elsewhere. Sample the entries; any backslash is fatal.
        $backslashed = $entries | Where-Object { $_.FullName -like '*\*' } | Select-Object -First 1
        if ($null -ne $backslashed) {
            throw "Bundle zip contains a backslash entry name ('$($backslashed.FullName)') - this is a locally re-zipped folder, not the CI artifact. Ship the CI artifact archive."
        }
        $forwardSlashSeen = $entries | Where-Object { $_.FullName -like '*/*' } | Select-Object -First 1
        if ($null -eq $forwardSlashSeen) {
            throw "Bundle zip has no nested entries at all - not a portable bundle."
        }

        # Root entries (no folder prefix) must be present - the bundle contents
        # sit at the zip root, so these are exact-name matches.
        $exe = $entries | Where-Object { $_.FullName -eq $ExeEntry } | Select-Object -First 1
        if ($null -eq $exe) {
            throw "Bundle zip is missing root entry '$ExeEntry'."
        }
        $readme = $entries | Where-Object { $_.FullName -eq $ReadmeEntry } | Select-Object -First 1
        if ($null -eq $readme) {
            throw "Bundle zip is missing root entry '$ReadmeEntry'."
        }

        # Extract both to the staging dir (short path, avoiding the >260-char
        # hazard). $true = overwrite.
        $exePath    = Join-Path $StageDir $ExeEntry
        $readmePath = Join-Path $StageDir $ReadmeEntry
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($exe, $exePath, $true)
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($readme, $readmePath, $true)

        # Prove the version from inside the artifact (failure #4). build.ps1 writes
        # "  Version: <BuildVersion>" into README_FIRST.txt from the same -Version
        # that stamps the exe metadata.
        $readmeText = Get-Content -LiteralPath $readmePath -Raw
        if (-not $readmeText.Contains("Version: $Version")) {
            $found = (Get-Content -LiteralPath $readmePath |
                Where-Object { $_ -match 'Version:' }) -join ' | '
            if ([string]::IsNullOrWhiteSpace($found)) { $found = '<no Version: line found>' }
            throw "README_FIRST.txt does not declare 'Version: $Version' - this artifact was built for a different version. Found: $found"
        }

        $exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
        return [pscustomobject]@{
            ExeSha256  = $exeHash
            ExePath    = $exePath
            ReadmePath = $readmePath
        }
    }
    finally {
        $zip.Dispose()
    }
}

function New-StageDir {
    # Fresh, SHORT staging dir under %TEMP% (short so extraction stays clear of
    # the 260-char path limit). Recreated each run.
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    return $Path
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$stage = Join-Path $env:TEMP "release-$Version"

try {
    $script:Gh = Resolve-Gh
    Write-Host "gh       : $script:Gh"
    Write-Host "repo     : $RepoSlug"
    Write-Host "version  : $Version"
    Write-Host "mode     : $(if ($VerifyExisting) { 'VERIFY (read-only)' } else { 'PUBLISH' })"

    if ($VerifyExisting) {
        # ------------------------- VERIFY EXISTING -------------------------
        # Download the already-published asset (public, no auth), re-hash it, and
        # confirm it matches the release's recorded digest. No mutations.
        New-StageDir -Path $stage | Out-Null

        $viewRaw = Invoke-Gh @(
            'release', 'view', $Version,
            '--repo', $RepoSlug,
            '--json', 'assets,url,targetCommitish'
        ) "gh release view $Version"
        $view = ($viewRaw -join "`n") | ConvertFrom-Json

        $asset = $view.assets | Where-Object { $_.name -eq $ZipName } | Select-Object -First 1
        if ($null -eq $asset) {
            throw "Release $Version has no asset named '$ZipName'."
        }

        $zipPath = Join-Path $stage $ZipName
        Write-Host ""
        Write-Host "Downloading published asset (public URL, no auth):"
        Write-Host "    $($asset.url)"
        # Public release asset - plain -OutFile, no Authorization header, never `>`.
        Invoke-WebRequest -Uri $asset.url -UseBasicParsing -OutFile $zipPath -ErrorAction Stop

        $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
        $zipLen  = (Get-Item -LiteralPath $zipPath).Length

        # Compare against the release-recorded digest (format "sha256:<hex>").
        $assetDigest = $null
        if ($asset.PSObject.Properties['digest']) { $assetDigest = $asset.digest }
        if ([string]::IsNullOrWhiteSpace($assetDigest)) {
            Write-Warning "Release asset has no digest field - cannot cross-check the release-side hash (older gh?). Comparing size only."
        }
        else {
            $assetHex = $assetDigest -replace '^sha256:', ''
            if ($assetHex -ine $zipHash) {
                throw "Downloaded asset SHA-256 ($zipHash) does not match the release digest ($assetDigest). The published asset is not what the release claims - investigate."
            }
        }
        if ([int64]$asset.size -ne [int64]$zipLen) {
            throw "Downloaded asset size ($zipLen bytes) does not match the release asset size ($($asset.size) bytes)."
        }

        # Open the zip and hash the contained exe (also re-proves the version).
        $bundle = Test-BundleZip -ZipPath $zipPath -Version $Version -StageDir $stage

        Write-Host ""
        Write-Host "===================== VERIFY SUMMARY ====================="
        Write-Host "  version        : $Version"
        Write-Host "  release        : $($view.url)"
        Write-Host "  target commit  : $($view.targetCommitish)"
        Write-Host "  asset          : $ZipName ($zipLen bytes)"
        Write-Host "  exe SHA-256    : $($bundle.ExeSha256)"
        Write-Host "  zip SHA-256    : $zipHash"
        Write-Host "  digest match   : $(if ([string]::IsNullOrWhiteSpace($assetDigest)) { 'skipped (no digest field)' } else { 'OK' })"
        Write-Host "=========================================================="

        Remove-Item -LiteralPath $stage -Recurse -Force
        Write-Host ""
        Write-Host "VERIFY OK - no changes made."
        exit 0
    }

    # ------------------------------ PUBLISH ------------------------------
    if (-not (Test-Path -LiteralPath $NotesFile)) {
        throw "Notes file not found: $NotesFile"
    }

    # 2. Resolve the run (auto-locate unless -RunId was passed).
    $autoLocate = -not $PSBoundParameters.ContainsKey('RunId')
    Write-Host ""
    if ($autoLocate) {
        Write-Host "Locating newest completed+successful '$WorkflowName' run on main..."
    }
    else {
        Write-Host "Validating run $RunId..."
    }
    $run = Get-RunInfo -RepoSlug $RepoSlug -RunId $RunId -AutoLocate $autoLocate
    $shortSha = if ($run.HeadSha.Length -ge 7) { $run.HeadSha.Substring(0, 7) } else { $run.HeadSha }
    Write-Host "    run id     : $($run.Id)"
    Write-Host "    head sha   : $($run.HeadSha) (short $shortSha)"

    # 3. Find the artifact on that run.
    $art = Get-ArtifactInfo -RepoSlug $RepoSlug -RunId $run.Id
    Write-Host "    artifact   : $($art.name) (id $($art.id), $($art.size_in_bytes) bytes)"

    # 4. Download the artifact archive into a fresh short staging dir.
    New-StageDir -Path $stage | Out-Null
    $zipPath = Join-Path $stage $ZipName
    Write-Host ""
    Write-Host "Downloading artifact archive..."
    Get-ArtifactArchive -ArchiveUrl $art.archive_download_url -OutFile $zipPath

    # 5. Verify the archive before it can reach Releases.
    Write-Host ""
    Write-Host "Verifying bundle zip..."
    $bundle  = Test-BundleZip -ZipPath $zipPath -Version $Version -StageDir $stage
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    $zipLen  = (Get-Item -LiteralPath $zipPath).Length
    Write-Host "    exe SHA-256 : $($bundle.ExeSha256)"
    Write-Host "    zip SHA-256 : $zipHash"

    # 6. Resolve notes tokens.
    Write-Host ""
    Write-Host "Resolving release notes tokens..."
    $notes = Get-Content -LiteralPath $NotesFile -Raw
    # .Replace (literal), not -replace (regex) - the {{...}} braces are literal.
    $notes = $notes.Replace('{{EXE_SHA256}}', $bundle.ExeSha256)
    $notes = $notes.Replace('{{ZIP_SHA256}}', $zipHash)
    $notes = $notes.Replace('{{COMMIT}}', $shortSha)
    $resolvedNotes = Join-Path $stage 'release-notes-resolved.md'
    Set-Content -LiteralPath $resolvedNotes -Value $notes -Encoding UTF8
    Write-Host "    wrote $resolvedNotes"

    # 7. Create the release with the verified archive attached.
    Write-Host ""
    Write-Host "Creating release $Version..."
    Invoke-Gh @(
        'release', 'create', $Version,
        '--repo', $RepoSlug,
        '--target', 'main',
        '--title', $Title,
        '--notes-file', $resolvedNotes,
        $zipPath
    ) "gh release create $Version" | ForEach-Object { Write-Host "    $_" }

    # 8. Post-verify: the release-side asset must match what we uploaded. Never
    #    leave a silent bad asset.
    Write-Host ""
    Write-Host "Post-verifying published asset..."
    $viewRaw = Invoke-Gh @(
        'release', 'view', $Version,
        '--repo', $RepoSlug,
        '--json', 'assets,targetCommitish,url'
    ) "gh release view $Version"
    $view = ($viewRaw -join "`n") | ConvertFrom-Json

    $asset = $view.assets | Where-Object { $_.name -eq $ZipName } | Select-Object -First 1
    if ($null -eq $asset) {
        throw "Post-verify: release $Version has no asset '$ZipName' after create. DELETE the release (gh release delete $Version --repo $RepoSlug) and investigate."
    }

    $assetDigest = $null
    if ($asset.PSObject.Properties['digest']) { $assetDigest = $asset.digest }
    if ([string]::IsNullOrWhiteSpace($assetDigest)) {
        Write-Warning "Post-verify: asset has no digest field (older gh?) - relying on size + the pre-upload hash. Consider re-verifying with -VerifyExisting."
    }
    else {
        $assetHex = $assetDigest -replace '^sha256:', ''
        if ($assetHex -ine $zipHash) {
            throw "Post-verify MISMATCH: published asset digest ($assetDigest) != uploaded zip SHA-256 ($zipHash). DELETE the release (gh release delete $Version --repo $RepoSlug) and investigate before anyone downloads it."
        }
    }
    if ([int64]$asset.size -ne [int64]$zipLen) {
        throw "Post-verify MISMATCH: published asset size ($($asset.size)) != uploaded size ($zipLen). DELETE the release (gh release delete $Version --repo $RepoSlug) and investigate."
    }
    if ($view.targetCommitish -and $view.targetCommitish -ne $run.HeadSha) {
        # Non-fatal: main may have advanced between build and publish. Surface it.
        Write-Warning "Release target commit ($($view.targetCommitish)) differs from the build's head sha ($($run.HeadSha)). Expected if main moved on since the run."
    }

    # 9. Summary + cleanup.
    Write-Host ""
    Write-Host "===================== RELEASE SUMMARY ===================="
    Write-Host "  version        : $Version"
    Write-Host "  run id         : $($run.Id)"
    Write-Host "  commit         : $shortSha"
    Write-Host "  exe SHA-256    : $($bundle.ExeSha256)"
    Write-Host "  zip SHA-256    : $zipHash"
    Write-Host "  asset          : $ZipName ($zipLen bytes)"
    Write-Host "  release URL    : $($view.url)"
    Write-Host "=========================================================="

    Remove-Item -LiteralPath $stage -Recurse -Force
    Write-Host ""
    Write-Host "PUBLISH OK."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        # Leave the staging dir for forensics on failure (partial download,
        # extracted files, resolved notes).
        Write-Host "Staging dir left for forensics: $stage" -ForegroundColor Yellow
    }
    exit 1
}
