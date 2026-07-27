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
    bytes field engineers download are the exact bytes CI built and booted. For
    v0.1.27 and later it also verifies and publishes the exact-SHA SBOMs,
    migration guide, release evidence, and checksum manifest carried by the
    artifact.

    Engineered around five real Windows PowerShell 5.1 failures hit publishing
    v0.1.15 by hand and v0.1.16 with this script. Each is a silent-corruption,
    silent-wrong-asset, or hard-stop trap, so each has a guard here rather than
    a comment saying "be careful":

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

    5. `Invoke-WebRequest -MaximumRedirection 0` is NOT a reliable way to catch
       the 302: on some .NET Framework patch levels it throws a bare
       InvalidOperationException ("Operation is not valid due to the current
       state of the object") with Exception.Response = $null, so the Location
       header is unreachable (hit publishing v0.1.16 — deterministic on that
       machine). The redirect probe therefore uses raw HttpWebRequest with
       AllowAutoRedirect = $false, where a 3xx is a NORMAL response and the
       Location header is read without exception games.

.PARAMETER Version
    Release tag, e.g. v0.1.16. Validated against ^v\d+\.\d+\.\d+$.

.PARAMETER RunId
    Optional workflow run id to publish from. When omitted, the newest completed
    + successful "Windows Portable Bundle" workflow_dispatch run on main is used.
    In verify mode, pass the original run id to prove the published tag points
    at the exact commit that produced the asset. Mandatory for v0.1.27 and later
    in both publish and verify modes; automatic selection is legacy-only.

.PARAMETER ReleaseGateRunId
    Successful "v0.1.27 Release Gates" workflow run that built the hosted
    images and image SBOMs. Required when publishing or verifying v0.1.27 or
    later. Its 40-character head SHA must equal the Windows run and release tag.

.PARAMETER NotesFile
    Markdown release-notes template (required for publishing). Tokens
    {{EXE_SHA256}}, {{ZIP_SHA256}} and {{COMMIT}} are substituted before upload.

.PARAMETER Title
    Release title. Defaults to $Version.

.PARAMETER RepoSlug
    owner/repo. Defaults to Rvs006/smart-commissioning-app.

.PARAMETER VerifyExisting
    Skip publishing. Instead download all required already-published assets for
    -Version and re-verify their digests, evidence, SBOMs, and contained exe
    hash. A harmless, read-only way to exercise this script end to end.

.EXAMPLE
    # Publish v0.1.27 from matching Windows and hosted release-gate runs:
    powershell -NoProfile -File scripts\release-portable.ps1 -Version v0.1.27 -RunId 123456789 -ReleaseGateRunId 123456790 -NotesFile docs\release-notes-v0.1.27.md

.EXAMPLE
    # Re-verify what is already published (no mutations):
    powershell -NoProfile -File scripts\release-portable.ps1 -Version v0.1.16 -RunId 123456789 -VerifyExisting
#>
[CmdletBinding(DefaultParameterSetName = 'Publish')]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^v\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(ParameterSetName = 'Publish')]
    [Parameter(ParameterSetName = 'Verify')]
    [long]$RunId,

    [Parameter(ParameterSetName = 'Publish')]
    [Parameter(ParameterSetName = 'Verify')]
    [long]$ReleaseGateRunId,

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
$ReleaseGateWorkflowName = 'v0.1.27 Release Gates'
$ZipName      = 'Smart_Commissioning_App_Windows_Portable.zip'  # release asset filename
$ExeEntry     = 'SmartCommissioningApp.exe'                     # root entry in the bundle zip
$ReadmeEntry  = 'README_FIRST.txt'                             # root entry carrying "Version: <v>"
$PythonSbomEntry = 'SBOM.python.cdx.json'
$NpmSbomEntry = 'SBOM.npm.cdx.json'
$MigrationEntry = 'MIGRATION_ROLLBACK.md'
$EvidenceEntry = 'release-evidence.json'
$WindowsEvidenceEntry = 'release-evidence.windows.json'
$ChecksumsEntry = 'SHA256SUMS.txt'
$ImageApiSbomEntry = 'SBOM.image-api.cdx.json'
$ImageWorkerSbomEntry = 'SBOM.image-worker.cdx.json'
$ImageFrontendSbomEntry = 'SBOM.image-frontend.cdx.json'
$WindowsAcceptanceEntry = 'windows-acceptance.json'
$FrontendVersionEntry = 'frontend/dist/.app-version'
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
    # Returns exact run identity for the run to publish from. Auto-locate uses the
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
        return Get-RunInfo -RepoSlug $RepoSlug -RunId ([long]$match.databaseId) -AutoLocate $false
    }

    # Explicit run: fetch + validate. --jq reshapes to the same field names.
    $raw = Invoke-Gh @(
        'api', "repos/$RepoSlug/actions/runs/$RunId",
        '--jq', '{databaseId: .id, status: .status, conclusion: .conclusion, headSha: .head_sha, name: .name, path: .path, event: .event, headBranch: .head_branch, runAttempt: .run_attempt, htmlUrl: .html_url}'
    ) "gh api run $RunId"
    $run = ($raw -join "`n") | ConvertFrom-Json
    if ($run.status -ne 'completed' -or $run.conclusion -ne 'success') {
        throw "Run $RunId is status='$($run.status)' conclusion='$($run.conclusion)' - refusing to publish from a run that did not complete successfully."
    }
    if ($run.name -ne $WorkflowName -or $run.path -notlike '.github/workflows/windows-portable.yml*') {
        throw "Run $RunId is '$($run.name)' at '$($run.path)', not the $WorkflowName workflow."
    }
    if ($run.event -ne 'workflow_dispatch' -or $run.headBranch -ne 'main') {
        throw "Run $RunId used event='$($run.event)' branch='$($run.headBranch)'; publishing requires a workflow_dispatch run from main."
    }
    $expectedUrl = "https://github.com/$RepoSlug/actions/runs/$RunId"
    if ([int]$run.runAttempt -lt 1 -or [string]$run.htmlUrl -cne $expectedUrl) {
        throw "Run $RunId has invalid attempt or URL identity ('$($run.runAttempt)', '$($run.htmlUrl)')."
    }
    return [pscustomobject]@{
        Id = [long]$run.databaseId
        HeadSha = ([string]$run.headSha).ToLowerInvariant()
        RunAttempt = [int]$run.runAttempt
        HtmlUrl = [string]$run.htmlUrl
    }
}

function Get-ArtifactInfo {
    # Find the portable-bundle artifact on a run. Fails clearly if it is absent
    # (wrong run / build failed before upload) or expired (retention lapsed).
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [Parameter(Mandatory)][long]$RunId,
        [string]$Name = $ArtifactName
    )
    $raw = Invoke-Gh @('api', "repos/$RepoSlug/actions/runs/$RunId/artifacts?per_page=100") "gh api artifacts for run $RunId"
    $data = ($raw -join "`n") | ConvertFrom-Json
    if ([int]$data.total_count -gt @($data.artifacts).Count) {
        throw "Run $RunId has more than 100 artifacts; uniqueness cannot be proven from one fail-closed API page."
    }
    $matches = @($data.artifacts | Where-Object { $_.name -ceq $Name })
    if ($matches.Count -ne 1) {
        throw "Run $RunId has $($matches.Count) artifacts named '$Name'; exactly one is required."
    }
    $art = $matches[0]
    if ($art.expired) {
        throw "Artifact '$Name' on run $RunId has EXPIRED (retention lapsed). Re-run the workflow to produce a fresh artifact."
    }
    return $art
}

function Get-ReleaseGateRunInfo {
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [Parameter(Mandatory)][long]$RunId,
        [Parameter(Mandatory)][string]$ExpectedSha
    )
    $raw = Invoke-Gh @(
        'api', "repos/$RepoSlug/actions/runs/$RunId",
        '--jq', '{databaseId: .id, status: .status, conclusion: .conclusion, headSha: .head_sha, name: .name, path: .path, event: .event, headBranch: .head_branch, runAttempt: .run_attempt, htmlUrl: .html_url}'
    ) "gh api release-gates run $RunId"
    $run = ($raw -join "`n") | ConvertFrom-Json
    if ($run.status -ne 'completed' -or $run.conclusion -ne 'success') {
        throw "Release-gates run $RunId is status='$($run.status)' conclusion='$($run.conclusion)'."
    }
    if ($run.name -ne $ReleaseGateWorkflowName -or $run.path -notlike '.github/workflows/release-gates.yml*') {
        throw "Run $RunId is '$($run.name)' at '$($run.path)', not $ReleaseGateWorkflowName."
    }
    if ($run.event -ne 'workflow_dispatch' -or $run.headBranch -ne 'main') {
        throw "Release-gates run $RunId used event='$($run.event)' branch='$($run.headBranch)'; expected a main workflow_dispatch."
    }
    $headSha = ([string]$run.headSha).ToLowerInvariant()
    if ($headSha -ine $ExpectedSha) {
        throw "Release-gates run $RunId built $headSha, not exact release SHA $ExpectedSha."
    }
    $expectedUrl = "https://github.com/$RepoSlug/actions/runs/$RunId"
    if ([int]$run.runAttempt -lt 1 -or [string]$run.htmlUrl -cne $expectedUrl) {
        throw "Release-gates run $RunId has invalid attempt or URL identity ('$($run.runAttempt)', '$($run.htmlUrl)')."
    }
    return [pscustomobject]@{
        Id = [long]$run.databaseId
        HeadSha = $headSha
        RunAttempt = [int]$run.runAttempt
        HtmlUrl = [string]$run.htmlUrl
    }
}

function Resolve-CommitSha {
    # GitHub's commit endpoint resolves branches, lightweight tags, and
    # annotated tags to the commit they name. Do not trust targetCommitish from
    # `gh release view`: GitHub may return the literal branch name instead of a
    # commit SHA.
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [Parameter(Mandatory)][string]$Reference
    )
    $raw = Invoke-Gh @(
        'api', "repos/$RepoSlug/commits/$Reference",
        '--jq', '.sha'
    ) "resolve commit '$Reference'"
    $sha = ($raw -join '').Trim()
    if ($sha -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Reference '$Reference' resolved to an invalid commit SHA '$sha'."
    }
    return $sha.ToLowerInvariant()
}

function Resolve-LocalCommitSha {
    param([Parameter(Mandatory)][string]$Reference)
    $sha = (& git rev-parse $Reference 2>$null)
    if ($LASTEXITCODE -ne 0 -or $sha -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Local reference '$Reference' is missing or invalid."
    }
    return $sha.Trim().ToLowerInvariant()
}

function Assert-SignedAnnotatedTag {
    # Publication is allowed only after the exact tag has been signed and
    # verified twice: by the local git trust store and by GitHub's tag-object
    # verification. Requiring a tag object (not a commit ref) rejects lightweight
    # tags, and matching object ids proves GitHub received the locally verified
    # annotation unchanged.
    param(
        [Parameter(Mandatory)][string]$RepoSlug,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$ExpectedSha
    )

    $localTagObject = (& git rev-parse "refs/tags/$Version^{tag}" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $localTagObject -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Local tag $Version is missing or lightweight; create a signed annotated tag first."
    }
    & git verify-tag $Version 2>&1 | ForEach-Object { Write-Host "    $_" }
    if ($LASTEXITCODE -ne 0) {
        throw "Local signature verification failed for annotated tag $Version."
    }
    $localTarget = (& git rev-parse "$Version^{commit}").Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $localTarget -ine $ExpectedSha) {
        throw "Local tag $Version targets $localTarget, not exact release SHA $ExpectedSha."
    }

    $refRaw = Invoke-Gh @('api', "repos/$RepoSlug/git/ref/tags/$Version") `
        "resolve GitHub tag ref '$Version'"
    $ref = ($refRaw -join "`n") | ConvertFrom-Json
    if ([string]$ref.object.type -cne 'tag') {
        throw "GitHub tag $Version is lightweight; a signed annotated tag is required."
    }
    if ([string]$ref.object.sha -ine $localTagObject.Trim()) {
        throw "GitHub tag object '$($ref.object.sha)' does not match locally verified '$($localTagObject.Trim())'."
    }
    $tagRaw = Invoke-Gh @('api', "repos/$RepoSlug/git/tags/$($ref.object.sha)") `
        "read GitHub annotated tag '$Version'"
    $tag = ($tagRaw -join "`n") | ConvertFrom-Json
    if ([string]$tag.tag -cne $Version -or [string]$tag.object.type -cne 'commit') {
        throw "GitHub tag object is not the expected annotated commit tag $Version."
    }
    if ([string]$tag.object.sha -ine $ExpectedSha) {
        throw "GitHub annotated tag $Version targets '$($tag.object.sha)', not '$ExpectedSha'."
    }
    if ($null -eq $tag.verification -or $tag.verification.verified -ne $true) {
        $reason = if ($null -eq $tag.verification) { 'missing verification' } else { [string]$tag.verification.reason }
        throw "GitHub has not verified the signature for $Version ($reason). Publish the signing key to GitHub before release publication."
    }
    Write-Host "    signed tag : locally verified and GitHub verified"
}

function Assert-ReleaseBodyDigests {
    param(
        [Parameter(Mandatory)][string]$Body,
        [Parameter(Mandatory)][string]$ExeSha256,
        [Parameter(Mandatory)][string]$ZipSha256,
        [Parameter(Mandatory)][string]$CommitSha,
        [AllowEmptyString()][string]$ExpectedBody
    )
    foreach ($expected in @($ExeSha256, $ZipSha256)) {
        if ($expected -notmatch '^[0-9a-fA-F]{64}$') {
            throw "Cannot verify release notes against invalid SHA-256 '$expected'."
        }
        if ($Body.IndexOf($expected, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "Release notes do not contain verified SHA-256 $expected."
        }
    }
    if ($CommitSha -notmatch '^[0-9a-fA-F]{40}$' -or
        $Body.IndexOf($CommitSha, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Release notes do not contain the exact verified 40-character commit $CommitSha."
    }
    if ($PSBoundParameters.ContainsKey('ExpectedBody')) {
        $actualText = $Body.Replace("`r`n", "`n").TrimEnd([char[]]"`r`n")
        $expectedText = $ExpectedBody.Replace("`r`n", "`n").TrimEnd([char[]]"`r`n")
        if ($actualText -cne $expectedText) {
            throw "Release notes body is not the exact resolved notes content."
        }
    }
}

function Write-ReleaseChecksums {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string[]]$Files
    )
    $lines = foreach ($file in $Files) {
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([IO.Path]::GetFileName($file))"
    }
    [IO.File]::WriteAllLines($Path, $lines, [Text.Encoding]::ASCII)
}

function Get-ArtifactArchive {
    # Download the artifact zip using the redirect-splitting technique (failures
    # #1 and #2 in the header). $ArchiveUrl is the api.github.com .../zip endpoint;
    # it answers 302 to a signed Azure blob URL.
    param(
        [Parameter(Mandatory)][string]$ArchiveUrl,
        [Parameter(Mandatory)][string]$OutFile,
        [string]$Accept = 'application/vnd.github+json'
    )
    $apiUri = $null
    if (-not [Uri]::TryCreate($ArchiveUrl, [UriKind]::Absolute, [ref]$apiUri) -or
        $apiUri.Scheme -cne 'https' -or $apiUri.DnsSafeHost -cne 'api.github.com' -or
        -not $apiUri.IsDefaultPort -or -not [string]::IsNullOrEmpty($apiUri.UserInfo)) {
        throw "Refusing to attach a GitHub token to non-api.github.com URL '$ArchiveUrl'."
    }
    $token = (& $script:Gh auth token)
    if ($LASTEXITCODE -ne 0) { throw "gh auth token failed (exit $LASTEXITCODE) - is gh logged in?" }
    $token = "$token".Trim()

    # Failure #5 (found publishing v0.1.16): Invoke-WebRequest -MaximumRedirection 0
    # on some .NET Framework patch levels throws a bare InvalidOperationException
    # ("Operation is not valid due to the current state of the object") with
    # $_.Exception.Response = $null, so the 302's Location header is unreachable
    # from the catch block. Probe the redirect with raw HttpWebRequest instead:
    # with AllowAutoRedirect = $false a 3xx comes back as a NORMAL response (only
    # >= 400 throws), so the Location header is read without exception games.
    $location = $null
    $req = [System.Net.WebRequest]::CreateHttp($ArchiveUrl)
    $req.Method = 'GET'
    $req.AllowAutoRedirect = $false
    $req.Accept = $Accept
    $req.UserAgent = 'smart-commissioning-release-portable'
    $req.Headers.Add('Authorization', "Bearer $token")
    $resp = $req.GetResponse()
    try {
        $status = [int]$resp.StatusCode
        if ($status -ge 300 -and $status -lt 400) {
            # The signed, pre-authenticated blob URL.
            $location = $resp.Headers['Location']
            if ([string]::IsNullOrWhiteSpace($location)) {
                throw "Artifact endpoint returned redirect $status but no Location header."
            }
        }
        elseif ($status -eq 200) {
            # GitHub does not currently answer 200 directly, but if it ever does,
            # stream the body to disk byte-exact (never `>`, failure #1).
            Write-Host "    endpoint answered 200 directly (no redirect) - writing archive"
            $inStream = $resp.GetResponseStream()
            $outStream = [System.IO.File]::Create($OutFile)
            try { $inStream.CopyTo($outStream) }
            finally { $outStream.Dispose(); $inStream.Dispose() }
        }
        else {
            throw "Artifact endpoint returned HTTP $status (expected a 302 redirect to blob storage)."
        }
    }
    finally {
        $resp.Close()
    }

    if ($location) {
        # Signed URL already carries its SAS token - send NO auth header, and use
        # -OutFile (never `>`, failure #1) so the binary lands byte-exact.
        $redirectUri = $null
        if (-not [Uri]::TryCreate($location, [UriKind]::Absolute, [ref]$redirectUri) -or
            $redirectUri.Scheme -cne 'https') {
            throw "GitHub download endpoint returned a non-HTTPS redirect."
        }
        Write-Host "    following signed redirect to blob storage (no auth header)"
        Invoke-WebRequest -Uri $location -UseBasicParsing -OutFile $OutFile -ErrorAction Stop
    }

    if (-not (Test-Path -LiteralPath $OutFile)) {
        throw "Download reported success but $OutFile is missing."
    }
}

function Assert-ArtifactArchive {
    param(
        [Parameter(Mandatory)]$Artifact,
        [Parameter(Mandatory)][string]$ArchivePath,
        [switch]$RequireMetadata
    )
    $actualSize = [int64](Get-Item -LiteralPath $ArchivePath).Length
    $actualHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash
    $hasSize = ($Artifact.PSObject.Properties['size_in_bytes'] -and
        [int64]$Artifact.size_in_bytes -gt 0)
    if ($RequireMetadata -and -not $hasSize) {
        throw "Actions artifact '$($Artifact.name)' has no positive size_in_bytes metadata."
    }
    if ($hasSize -and [int64]$Artifact.size_in_bytes -ne $actualSize) {
        throw "Actions artifact '$($Artifact.name)' archive size $actualSize does not match GitHub size $($Artifact.size_in_bytes)."
    }
    $hasDigest = ($Artifact.PSObject.Properties['digest'] -and
        -not [string]::IsNullOrWhiteSpace([string]$Artifact.digest))
    if ($RequireMetadata -and -not $hasDigest) {
        throw "Actions artifact '$($Artifact.name)' has no SHA-256 digest metadata."
    }
    if ($hasDigest) {
        $digest = [string]$Artifact.digest
        if ($digest -notmatch '^sha256:[0-9a-fA-F]{64}$') {
            throw "Actions artifact '$($Artifact.name)' has invalid digest '$digest'."
        }
        if (($digest -replace '^sha256:', '') -ine $actualHash) {
            throw "Actions artifact '$($Artifact.name)' archive SHA-256 does not match GitHub digest."
        }
    }
}

function Get-UniqueReleaseAsset {
    param(
        [Parameter(Mandatory)][object[]]$Assets,
        [Parameter(Mandatory)][string]$Name
    )
    $matches = @($Assets | Where-Object { $_.name -ceq $Name })
    if ($matches.Count -ne 1) {
        throw "Release has $($matches.Count) assets named '$Name'; exactly one is required."
    }
    return $matches[0]
}

function Assert-ReleaseAssetMatchesFile {
    param(
        [Parameter(Mandatory)]$Asset,
        [Parameter(Mandatory)][string]$LocalFile,
        [Parameter(Mandatory)][string]$DownloadDirectory
    )
    if ([string]::IsNullOrWhiteSpace([string]$Asset.apiUrl)) {
        throw "Release asset '$($Asset.name)' has no authenticated API URL."
    }
    $downloaded = Join-Path $DownloadDirectory ("release-asset-{0}-{1}" -f $Asset.id, $Asset.name)
    Get-ArtifactArchive -ArchiveUrl ([string]$Asset.apiUrl) -OutFile $downloaded `
        -Accept 'application/octet-stream'
    $localSize = [int64](Get-Item -LiteralPath $LocalFile).Length
    $actualSize = [int64](Get-Item -LiteralPath $downloaded).Length
    $localHash = (Get-FileHash -LiteralPath $LocalFile -Algorithm SHA256).Hash
    $actualHash = (Get-FileHash -LiteralPath $downloaded -Algorithm SHA256).Hash
    if ($actualSize -ne $localSize -or $actualHash -ine $localHash) {
        throw "Downloaded release asset '$($Asset.name)' is not byte-identical to the verified upload."
    }
    if ([int64]$Asset.size -ne $actualSize) {
        throw "Release asset '$($Asset.name)' size does not match its downloaded bytes."
    }
    if ($Asset.PSObject.Properties['digest'] -and
        -not [string]::IsNullOrWhiteSpace([string]$Asset.digest)) {
        $digest = [string]$Asset.digest
        if ($digest -notmatch '^sha256:[0-9a-fA-F]{64}$' -or
            ($digest -replace '^sha256:', '') -ine $actualHash) {
            throw "Release asset '$($Asset.name)' digest does not match its downloaded bytes."
        }
    }
    return $downloaded
}

function Test-BundleZip {
    # Verify the downloaded archive BEFORE anything touches Releases, and extract
    # the exe + readme so we can hash the exe and prove the version. Returns
    # @{ ExeSha256; ExePath; ReadmePath }. Throws on any structural problem.
    param(
        [Parameter(Mandatory)][string]$ZipPath,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha,
        [Parameter(Mandatory)][string]$StageDir,
        [long]$WorkflowRunId,
        [int]$WorkflowRunAttempt,
        [string]$WorkflowArtifactName,
        [string]$Repository
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
        $requireEvidence = ([version]($Version.TrimStart('v')) -ge [version]'0.1.26')
        $requireV0127Evidence = ([version]($Version.TrimStart('v')) -ge [version]'0.1.27')
        $requiredNames = @($ReadmeEntry)
        if ($requireEvidence) {
            $requiredNames += @(
                $PythonSbomEntry, $NpmSbomEntry, $MigrationEntry,
                $EvidenceEntry, $ChecksumsEntry
            )
        }
        if ($requireV0127Evidence) {
            $requiredNames += @($WindowsAcceptanceEntry, $FrontendVersionEntry)
        }
        $requiredEntries = @{}
        foreach ($requiredName in $requiredNames) {
            $found = $entries | Where-Object { $_.FullName -eq $requiredName } | Select-Object -First 1
            if ($null -eq $found) { throw "Bundle zip is missing root entry '$requiredName'." }
            $requiredEntries[$requiredName] = $found
        }

        # Extract both to the staging dir (short path, avoiding the >260-char
        # hazard). $true = overwrite.
        $exePath    = Join-Path $StageDir $ExeEntry
        $readmePath = Join-Path $StageDir $ReadmeEntry
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($exe, $exePath, $true)
        foreach ($requiredName in $requiredEntries.Keys) {
            $target = Join-Path $StageDir $requiredName
            $targetDirectory = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $targetDirectory)) {
                New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
            }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile(
                $requiredEntries[$requiredName], $target, $true
            )
        }

        # Prove the version from inside the artifact (failure #4). build.ps1 writes
        # "  Version: <BuildVersion>" into README_FIRST.txt from the same -Version
        # that stamps the exe metadata.
        $readmeLines = Get-Content -LiteralPath $readmePath
        $expectedVersionLine = "Version: $Version"
        $exactVersionLine = $readmeLines |
            Where-Object { $_.Trim() -ceq $expectedVersionLine } |
            Select-Object -First 1
        if ($null -eq $exactVersionLine) {
            $found = ($readmeLines |
                Where-Object { $_ -match 'Version:' }) -join ' | '
            if ([string]::IsNullOrWhiteSpace($found)) { $found = '<no Version: line found>' }
            throw "README_FIRST.txt does not contain the exact line '$expectedVersionLine' - this artifact was built for a different version. Found: $found"
        }

        $exeProductVersion = (Get-Item -LiteralPath $exePath).VersionInfo.ProductVersion
        if ($exeProductVersion -cne $Version) {
            throw "SmartCommissioningApp.exe ProductVersion '$exeProductVersion' does not equal '$Version'."
        }
        if ($requireV0127Evidence) {
            $frontendVersionPath = Join-Path $StageDir $FrontendVersionEntry
            $frontendVersion = (Get-Content -LiteralPath $frontendVersionPath -Raw).Trim()
            if ($frontendVersion -cne $Version) {
                throw "Frontend build stamp '$frontendVersion' does not equal '$Version'."
            }
        }

        $exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
        $evidencePath = $null
        if ($requireEvidence) {
            $evidencePath = Join-Path $StageDir $EvidenceEntry
            $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
            if ([string]$evidence.release_version -cne $Version) {
                throw "Release evidence version '$($evidence.release_version)' does not equal '$Version'."
            }
            if ([string]$evidence.source_commit -ine $CommitSha) {
                throw "Release evidence commit '$($evidence.source_commit)' does not equal '$CommitSha'."
            }
            if ([string]$evidence.gates.windows_build -cne 'passed') {
                throw "Release evidence does not record windows_build=passed."
            }
            if ($requireV0127Evidence) {
                if ([string]$evidence.schema_version -cne '1.1' -or
                    [string]$evidence.evidence_kind -cne 'windows' -or
                    [string]$evidence.product_version -cne $Version) {
                    throw "Windows evidence lacks v0.1.27 schema, kind, or ProductVersion metadata."
                }
                $expectedRunUrl = "https://github.com/$Repository/actions/runs/$WorkflowRunId"
                if ([string]$evidence.workflow.name -cne $WorkflowName -or
                    [string]$evidence.workflow.event -cne 'workflow_dispatch' -or
                    [string]$evidence.workflow.repository -cne $Repository -or
                    [string]$evidence.workflow.artifact_name -cne $WorkflowArtifactName -or
                    [int]$evidence.workflow.run_attempt -ne $WorkflowRunAttempt -or
                    [string]$evidence.workflow.run_url -cne $expectedRunUrl) {
                    throw "Windows evidence was not produced by a release dispatch of '$WorkflowName'."
                }
                if ($WorkflowRunId -le 0 -or $WorkflowRunAttempt -le 0 -or
                    [string]::IsNullOrWhiteSpace($WorkflowArtifactName) -or
                    [string]::IsNullOrWhiteSpace($Repository) -or
                    [long]$evidence.workflow.run_id -ne $WorkflowRunId) {
                    throw "Windows evidence run '$($evidence.workflow.run_id)' is not selected run '$WorkflowRunId'."
                }
                foreach ($gate in @(
                    'windows_readiness', 'windows_long_heartbeat', 'windows_cancellation',
                    'report_provenance', 'report_byte_equality', 'path_with_spaces',
                    'log_thread_inspection', 'app_root', 'frontend_build_stamp',
                    'sqlite_lease_configuration', 'sqlite_heartbeat_renewals'
                )) {
                    $property = $evidence.gates.PSObject.Properties[$gate]
                    if ($null -eq $property -or [string]$property.Value -cne 'passed') {
                        throw "Windows evidence does not record $gate=passed."
                    }
                }
                foreach ($test in @(
                    'release_heartbeat_integration', 'portable_readiness',
                    'portable_long_heartbeat', 'portable_cancellation', 'canonical_udmi',
                    'portable_report_provenance', 'portable_report_byte_equality',
                    'portable_path_with_spaces', 'portable_log_thread_inspection',
                    'portable_app_root', 'portable_frontend_build_stamp',
                    'portable_sqlite_lease_configuration',
                    'portable_sqlite_heartbeat_renewals'
                )) {
                    $property = $evidence.tests.PSObject.Properties[$test]
                    if ($null -eq $property -or [string]$property.Value -cne 'passed') {
                        throw "Windows evidence does not record test $test=passed."
                    }
                }
            }
            $exeRecord = $evidence.files | Where-Object { $_.name -eq $ExeEntry } | Select-Object -First 1
            if ($null -eq $exeRecord -or [string]$exeRecord.sha256 -ine $exeHash) {
                throw "Release evidence does not match the contained executable SHA-256."
            }
            foreach ($sbomName in @($PythonSbomEntry, $NpmSbomEntry)) {
                $sbomPath = Join-Path $StageDir $sbomName
                $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
                if ([string]$sbom.bomFormat -cne 'CycloneDX' -or $null -eq $sbom.components) {
                    throw "$sbomName is not a populated CycloneDX document."
                }
                $record = $evidence.files | Where-Object { $_.name -eq $sbomName } | Select-Object -First 1
                $actualHash = (Get-FileHash -LiteralPath $sbomPath -Algorithm SHA256).Hash
                if ($null -eq $record -or [string]$record.sha256 -ine $actualHash) {
                    throw "Release evidence does not match $sbomName."
                }
            }
            $migrationPath = Join-Path $StageDir $MigrationEntry
            $migrationRecord = $evidence.files |
                Where-Object { $_.name -eq $MigrationEntry } |
                Select-Object -First 1
            $migrationHash = (Get-FileHash -LiteralPath $migrationPath -Algorithm SHA256).Hash
            if ($null -eq $migrationRecord -or [string]$migrationRecord.sha256 -ine $migrationHash) {
                throw "Release evidence does not match $MigrationEntry."
            }

            # Validate the workflow-generated manifest before the publish path
            # replaces it with the release-level manifest that also includes the
            # outer zip. Every internal evidence payload must be named exactly
            # once and hash to the recorded value.
            $internalChecksumsPath = Join-Path $StageDir $ChecksumsEntry
            $internalChecksums = @{}
            foreach ($line in Get-Content -LiteralPath $internalChecksumsPath) {
                if ($line -notmatch '^([0-9a-f]{64})  ([^/\\]+)$') {
                    throw "Invalid $ChecksumsEntry line: '$line'."
                }
                $name = $Matches[2]
                if ($internalChecksums.ContainsKey($name)) {
                    throw "Duplicate $ChecksumsEntry entry '$name'."
                }
                $internalChecksums[$name] = $Matches[1]
            }
            $internalPayloads = @{
                $ExeEntry = $exePath
                $PythonSbomEntry = (Join-Path $StageDir $PythonSbomEntry)
                $NpmSbomEntry = (Join-Path $StageDir $NpmSbomEntry)
                $MigrationEntry = $migrationPath
                $EvidenceEntry = $evidencePath
            }
            if ($requireV0127Evidence) {
                $internalPayloads[$WindowsAcceptanceEntry] = Join-Path $StageDir $WindowsAcceptanceEntry
            }
            if ($internalChecksums.Count -ne $internalPayloads.Count) {
                throw "$ChecksumsEntry contains an unexpected number of entries."
            }
            foreach ($name in $internalPayloads.Keys) {
                if (-not $internalChecksums.ContainsKey($name)) {
                    throw "$ChecksumsEntry is missing '$name'."
                }
                $actualHash = (Get-FileHash -LiteralPath $internalPayloads[$name] -Algorithm SHA256).Hash
                if ([string]$internalChecksums[$name] -ine $actualHash) {
                    throw "$ChecksumsEntry does not match '$name'."
                }
            }
        }
        return [pscustomobject]@{
            ExeSha256  = $exeHash
            ExePath    = $exePath
            ReadmePath = $readmePath
            PythonSbomPath = (Join-Path $StageDir $PythonSbomEntry)
            NpmSbomPath = (Join-Path $StageDir $NpmSbomEntry)
            MigrationPath = (Join-Path $StageDir $MigrationEntry)
            EvidencePath = $evidencePath
            ChecksumsPath = (Join-Path $StageDir $ChecksumsEntry)
            AcceptancePath = (Join-Path $StageDir $WindowsAcceptanceEntry)
            FrontendVersionPath = (Join-Path $StageDir $FrontendVersionEntry)
        }
    }
    finally {
        $zip.Dispose()
    }
}

function Test-HostedEvidence {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha,
        [long]$WorkflowRunId,
        [int]$WorkflowRunAttempt,
        [string]$WorkflowArtifactName,
        [string]$Repository
    )
    $evidenceMatches = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $EvidenceEntry)
    if ($evidenceMatches.Count -ne 1) {
        throw "Hosted evidence archive contains $($evidenceMatches.Count) '$EvidenceEntry' files; expected exactly one."
    }
    $evidencePath = $evidenceMatches[0].FullName
    $base = $evidenceMatches[0].DirectoryName
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    if ([string]$evidence.release_version -cne $Version) {
        throw "Hosted evidence version '$($evidence.release_version)' does not equal '$Version'."
    }
    if ([string]$evidence.source_commit -ine $CommitSha) {
        throw "Hosted evidence commit '$($evidence.source_commit)' does not equal '$CommitSha'."
    }
    if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27') {
        if ([string]$evidence.schema_version -cne '1.1' -or
            [string]$evidence.evidence_kind -cne 'hosted' -or
            [string]$evidence.product_version -cne $Version) {
            throw "Hosted evidence lacks v0.1.27 schema, kind, or ProductVersion metadata."
        }
        $expectedRunUrl = "https://github.com/$Repository/actions/runs/$WorkflowRunId"
        if ([string]$evidence.workflow.name -cne $ReleaseGateWorkflowName -or
            [string]$evidence.workflow.event -cne 'workflow_dispatch' -or
            [string]$evidence.workflow.repository -cne $Repository -or
            [string]$evidence.workflow.artifact_name -cne $WorkflowArtifactName -or
            [int]$evidence.workflow.run_attempt -ne $WorkflowRunAttempt -or
            [string]$evidence.workflow.run_url -cne $expectedRunUrl) {
            throw "Hosted evidence was not produced by a release dispatch of '$ReleaseGateWorkflowName'."
        }
        if ($WorkflowRunId -le 0 -or $WorkflowRunAttempt -le 0 -or
            [string]::IsNullOrWhiteSpace($WorkflowArtifactName) -or
            [string]::IsNullOrWhiteSpace($Repository) -or
            [long]$evidence.workflow.run_id -ne $WorkflowRunId) {
            throw "Hosted evidence run '$($evidence.workflow.run_id)' is not selected run '$WorkflowRunId'."
        }
        foreach ($test in @(
            'ruff', 'core_unittest', 'backend_unittest', 'worker_unittest',
            'frontend_lint', 'frontend_typecheck', 'frontend_vitest',
            'frontend_build', 'migration_rollback', 'hosted_queue_smoke'
        )) {
            $property = $evidence.tests.PSObject.Properties[$test]
            if ($null -eq $property -or [string]$property.Value -cne 'passed') {
                throw "Hosted evidence does not record test $test=passed."
            }
        }
    }
    foreach ($gate in @('python', 'frontend', 'hosted_compose', 'backup_rollback')) {
        $property = $evidence.gates.PSObject.Properties[$gate]
        if ($null -eq $property -or [string]$property.Value -cne 'passed') {
            throw "Hosted evidence does not record $gate=passed."
        }
    }

    $requiredNames = @(
        $PythonSbomEntry, $NpmSbomEntry,
        $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
        $MigrationEntry
    )
    $paths = @{}
    foreach ($name in $requiredNames) {
        $path = Join-Path $base $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Hosted evidence is missing '$name'."
        }
        $recordMatches = @($evidence.files | Where-Object { $_.name -eq $name })
        if ($recordMatches.Count -ne 1) {
            throw "Hosted evidence must contain one digest record for '$name'."
        }
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ([string]$recordMatches[0].sha256 -ine $actualHash) {
            throw "Hosted evidence digest does not match '$name'."
        }
        if ([int64]$recordMatches[0].size -ne [int64](Get-Item -LiteralPath $path).Length) {
            throw "Hosted evidence size does not match '$name'."
        }
        if ($name -like '*.cdx.json') {
            $sbom = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            if ([string]$sbom.bomFormat -cne 'CycloneDX' -or @($sbom.components).Count -eq 0) {
                throw "Hosted evidence '$name' is not a populated CycloneDX document."
            }
        }
        $paths[$name] = $path
    }

    $hostedChecksumsPath = Join-Path $base $ChecksumsEntry
    if (-not (Test-Path -LiteralPath $hostedChecksumsPath -PathType Leaf)) {
        throw "Hosted evidence is missing '$ChecksumsEntry'."
    }
    $checksumRecords = @{}
    foreach ($line in Get-Content -LiteralPath $hostedChecksumsPath) {
        if ($line -notmatch '^([0-9a-f]{64})  ([^/\\]+)$') {
            throw "Invalid hosted $ChecksumsEntry line: '$line'."
        }
        $name = $Matches[2]
        if ($checksumRecords.ContainsKey($name)) {
            throw "Duplicate hosted $ChecksumsEntry entry '$name'."
        }
        $checksumRecords[$name] = $Matches[1]
    }
    $checksumPayloads = @{}
    foreach ($name in $requiredNames) { $checksumPayloads[$name] = $paths[$name] }
    $checksumPayloads[$EvidenceEntry] = $evidencePath
    if ($checksumRecords.Count -ne $checksumPayloads.Count) {
        throw "Hosted $ChecksumsEntry contains an unexpected number of entries."
    }
    foreach ($name in $checksumPayloads.Keys) {
        if (-not $checksumRecords.ContainsKey($name)) {
            throw "Hosted $ChecksumsEntry is missing '$name'."
        }
        $actualHash = (Get-FileHash -LiteralPath $checksumPayloads[$name] -Algorithm SHA256).Hash
        if ([string]$checksumRecords[$name] -ine $actualHash) {
            throw "Hosted $ChecksumsEntry does not match '$name'."
        }
    }

    return [pscustomobject]@{
        PythonSbomPath = $paths[$PythonSbomEntry]
        NpmSbomPath = $paths[$NpmSbomEntry]
        ImageApiSbomPath = $paths[$ImageApiSbomEntry]
        ImageWorkerSbomPath = $paths[$ImageWorkerSbomEntry]
        ImageFrontendSbomPath = $paths[$ImageFrontendSbomEntry]
        MigrationPath = $paths[$MigrationEntry]
        EvidencePath = $evidencePath
    }
}

function New-StageDir {
    # Fresh, SHORT staging dir under %TEMP% (short so extraction stays clear of
    # the 260-char path limit). Recreated each run.
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Version
    )
    $separators = [char[]]"\/"
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd($separators)
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd($separators)
    $parent = [IO.Directory]::GetParent($fullPath)
    $expectedName = "release-$Version"
    if ($null -eq $parent -or
        -not $parent.FullName.Equals($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($fullPath) -cne $expectedName) {
        throw "Refusing to manage unsafe staging path '$fullPath'; expected direct TEMP child '$expectedName'."
    }
    if (Test-Path -LiteralPath $fullPath) {
        $existing = Get-Item -LiteralPath $fullPath -Force
        if (($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to recursively delete reparse-point staging path '$fullPath'."
        }
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullPath -Force | Out-Null
    return $fullPath
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$stage = Join-Path $env:TEMP "release-$Version"
$draftCreated = $false
$draftPublished = $false
$publishCommandSucceeded = $false

try {
    $script:Gh = Resolve-Gh
    Write-Host "gh       : $script:Gh"
    Write-Host "repo     : $RepoSlug"
    Write-Host "version  : $Version"
    Write-Host "mode     : $(if ($VerifyExisting) { 'VERIFY (read-only)' } else { 'PUBLISH' })"

    $requireV0127 = ([version]($Version.TrimStart('v')) -ge [version]'0.1.27')
    if ($requireV0127 -and
        (-not $PSBoundParameters.ContainsKey('RunId') -or $RunId -le 0 -or
         -not $PSBoundParameters.ContainsKey('ReleaseGateRunId') -or $ReleaseGateRunId -le 0)) {
        throw "$Version requires both -RunId and -ReleaseGateRunId; automatic workflow selection is forbidden for publish and verification."
    }

    if ($VerifyExisting) {
        # ------------------------- VERIFY EXISTING -------------------------
        # Download the already-published asset (public, no auth), re-hash it, and
        # confirm it matches the release's recorded digest. No mutations.
        $stage = New-StageDir -Path $stage -Version $Version

        $viewRaw = Invoke-Gh @(
            'release', 'view', $Version,
            '--repo', $RepoSlug,
            '--json', 'assets,body,url,targetCommitish,isDraft'
        ) "gh release view $Version"
        $view = ($viewRaw -join "`n") | ConvertFrom-Json
        if ($requireV0127 -and $view.isDraft -eq $true) {
            throw "Release $Version is still a draft; VerifyExisting requires the published release."
        }

        $tagSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference $Version
        if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27') {
            $mainSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
            if ($mainSha -ine $tagSha) {
                throw "Remote main is $mainSha, but release tag $Version targets $tagSha."
            }
            foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
                $localSha = Resolve-LocalCommitSha -Reference $localReference
                if ($localSha -ine $tagSha) {
                    throw "Local reference $localReference is $localSha, not release SHA $tagSha."
                }
            }
            Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $tagSha
        }
        $verifiedRun = $null
        $verifiedHostedRun = $null
        $claimedWindowsArchive = $null
        $claimedHostedEvidence = $null
        if ($PSBoundParameters.ContainsKey('RunId')) {
            $verifiedRun = Get-RunInfo -RepoSlug $RepoSlug -RunId $RunId -AutoLocate $false
            if ($tagSha -ine $verifiedRun.HeadSha) {
                throw "Release tag $Version resolves to $tagSha, but workflow run $RunId built $($verifiedRun.HeadSha)."
            }
        }

        if ($requireV0127) {
            $windowsArtifact = Get-ArtifactInfo -RepoSlug $RepoSlug -RunId $verifiedRun.Id -Name $ArtifactName
            $claimedWindowsArchive = Join-Path $stage 'claimed-windows-workflow-artifact.zip'
            Get-ArtifactArchive -ArchiveUrl $windowsArtifact.archive_download_url -OutFile $claimedWindowsArchive
            Assert-ArtifactArchive -Artifact $windowsArtifact -ArchivePath $claimedWindowsArchive `
                -RequireMetadata:$requireV0127

            $verifiedHostedRun = Get-ReleaseGateRunInfo `
                -RepoSlug $RepoSlug -RunId $ReleaseGateRunId -ExpectedSha $tagSha
            $hostedArtifactName = "$Version-release-evidence-$tagSha"
            $hostedArtifact = Get-ArtifactInfo `
                -RepoSlug $RepoSlug -RunId $verifiedHostedRun.Id -Name $hostedArtifactName
            $claimedHostedArchive = Join-Path $stage 'claimed-hosted-workflow-artifact.zip'
            $claimedHostedRoot = Join-Path $stage 'claimed-hosted-workflow-artifact'
            New-Item -ItemType Directory -Path $claimedHostedRoot -Force | Out-Null
            Get-ArtifactArchive -ArchiveUrl $hostedArtifact.archive_download_url -OutFile $claimedHostedArchive
            Assert-ArtifactArchive -Artifact $hostedArtifact -ArchivePath $claimedHostedArchive `
                -RequireMetadata:$requireV0127
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [System.IO.Compression.ZipFile]::ExtractToDirectory($claimedHostedArchive, $claimedHostedRoot)
            $claimedHostedEvidence = Test-HostedEvidence `
                -Root $claimedHostedRoot -Version $Version -CommitSha $tagSha `
                -WorkflowRunId $verifiedHostedRun.Id `
                -WorkflowRunAttempt $verifiedHostedRun.RunAttempt `
                -WorkflowArtifactName $hostedArtifactName -Repository $RepoSlug
        }

        $asset = Get-UniqueReleaseAsset -Assets @($view.assets) -Name $ZipName

        $zipPath = Join-Path $stage $ZipName
        Write-Host ""
        Write-Host "Downloading published asset (public URL, no auth):"
        Write-Host "    $($asset.url)"
        # Public release asset - plain -OutFile, no Authorization header, never `>`.
        Invoke-WebRequest -Uri $asset.url -UseBasicParsing -OutFile $zipPath -ErrorAction Stop

        $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
        $zipLen  = (Get-Item -LiteralPath $zipPath).Length
        $repeatZipPath = Join-Path $stage 'repeat-download.zip'
        Invoke-WebRequest -Uri $asset.url -UseBasicParsing -OutFile $repeatZipPath -ErrorAction Stop
        $repeatZipHash = (Get-FileHash -LiteralPath $repeatZipPath -Algorithm SHA256).Hash
        $repeatZipLen = (Get-Item -LiteralPath $repeatZipPath).Length
        if ($repeatZipHash -ine $zipHash -or [int64]$repeatZipLen -ne [int64]$zipLen) {
            throw "Repeated downloads of '$ZipName' were not byte-identical."
        }
        if ($requireV0127) {
            $claimedHash = (Get-FileHash -LiteralPath $claimedWindowsArchive -Algorithm SHA256).Hash
            $claimedSize = [int64](Get-Item -LiteralPath $claimedWindowsArchive).Length
            if ($claimedHash -ine $zipHash -or $claimedSize -ne [int64]$zipLen) {
                throw "Published Windows zip is not byte-identical to run $($verifiedRun.Id) artifact '$ArtifactName'."
            }
        }

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
        $bundle = Test-BundleZip -ZipPath $zipPath -Version $Version -CommitSha $tagSha `
            -StageDir $stage -WorkflowRunId $RunId `
            -WorkflowRunAttempt $(if ($verifiedRun) { $verifiedRun.RunAttempt } else { 0 }) `
            -WorkflowArtifactName $(if ($verifiedRun) { $ArtifactName } else { '' }) `
            -Repository $(if ($verifiedRun) { $RepoSlug } else { '' })
        Assert-ReleaseBodyDigests `
            -Body ([string]$view.body) `
            -ExeSha256 $bundle.ExeSha256 `
            -ZipSha256 $zipHash `
            -CommitSha $tagSha
        if ($bundle.EvidencePath) {
            if (-not $requireV0127 -and $PSBoundParameters.ContainsKey('ReleaseGateRunId')) {
                Get-ReleaseGateRunInfo `
                    -RepoSlug $RepoSlug -RunId $ReleaseGateRunId -ExpectedSha $tagSha | Out-Null
            }
            $payloadNames = @(
                $ZipName, $PythonSbomEntry, $NpmSbomEntry,
                $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
                $MigrationEntry, $EvidenceEntry, $WindowsEvidenceEntry
            )
            $expectedAssetNames = @($payloadNames + $ChecksumsEntry)
            if (@($view.assets).Count -ne $expectedAssetNames.Count) {
                throw "Release $Version has an unexpected number of assets."
            }
            foreach ($expectedName in $expectedAssetNames) {
                Get-UniqueReleaseAsset -Assets @($view.assets) -Name $expectedName | Out-Null
            }
            $checksumsAsset = Get-UniqueReleaseAsset -Assets @($view.assets) -Name $ChecksumsEntry
            $publishedDir = Join-Path $stage 'published-evidence'
            New-Item -ItemType Directory -Path $publishedDir -Force | Out-Null
            $publishedChecksumsPath = Join-Path $publishedDir $ChecksumsEntry
            Invoke-WebRequest -Uri $checksumsAsset.url -UseBasicParsing `
                -OutFile $publishedChecksumsPath -ErrorAction Stop
            $publishedChecksumsHash = (Get-FileHash -LiteralPath $publishedChecksumsPath -Algorithm SHA256).Hash
            if ([int64]$checksumsAsset.size -ne [int64](Get-Item -LiteralPath $publishedChecksumsPath).Length) {
                throw "Published size mismatch for '$ChecksumsEntry'."
            }
            if ($checksumsAsset.PSObject.Properties['digest'] -and $checksumsAsset.digest) {
                $recordedHash = ([string]$checksumsAsset.digest) -replace '^sha256:', ''
                if ($recordedHash -ine $publishedChecksumsHash) {
                    throw "GitHub digest mismatch for '$ChecksumsEntry'."
                }
            }
            $checksumRecords = @{}
            foreach ($line in Get-Content -LiteralPath $publishedChecksumsPath) {
                if ($line -notmatch '^([0-9a-f]{64})  ([^/\\]+)$') {
                    throw "Invalid published $ChecksumsEntry line: '$line'."
                }
                if ($checksumRecords.ContainsKey($Matches[2])) {
                    throw "Duplicate published $ChecksumsEntry entry '$($Matches[2])'."
                }
                $checksumRecords[$Matches[2]] = $Matches[1]
            }
            if ($checksumRecords.Count -ne $payloadNames.Count) {
                throw "Published $ChecksumsEntry contains an unexpected number of entries."
            }
            $publishedFiles = @{ $ZipName = $zipPath }
            foreach ($name in $payloadNames | Where-Object { $_ -ne $ZipName }) {
                $evidenceAsset = Get-UniqueReleaseAsset -Assets @($view.assets) -Name $name
                $downloaded = Join-Path $publishedDir $name
                Invoke-WebRequest -Uri $evidenceAsset.url -UseBasicParsing `
                    -OutFile $downloaded -ErrorAction Stop
                $publishedFiles[$name] = $downloaded
                $downloadHash = (Get-FileHash -LiteralPath $downloaded -Algorithm SHA256).Hash
                if (-not $checksumRecords.ContainsKey($name) -or $checksumRecords[$name] -ine $downloadHash) {
                    throw "Published $ChecksumsEntry does not match '$name'."
                }
                if ([int64]$evidenceAsset.size -ne [int64](Get-Item -LiteralPath $downloaded).Length) {
                    throw "Published size mismatch for '$name'."
                }
                if ($evidenceAsset.PSObject.Properties['digest'] -and $evidenceAsset.digest) {
                    $recordedHash = ([string]$evidenceAsset.digest) -replace '^sha256:', ''
                    if ($recordedHash -ine $downloadHash) {
                        throw "GitHub digest mismatch for '$name'."
                    }
                }
            }
            if (-not $checksumRecords.ContainsKey($ZipName) -or $checksumRecords[$ZipName] -ine $zipHash) {
                throw "Published $ChecksumsEntry does not match '$ZipName'."
            }
            if ($requireV0127) {
                $claimedHostedFiles = @{
                    $PythonSbomEntry = $claimedHostedEvidence.PythonSbomPath
                    $NpmSbomEntry = $claimedHostedEvidence.NpmSbomPath
                    $ImageApiSbomEntry = $claimedHostedEvidence.ImageApiSbomPath
                    $ImageWorkerSbomEntry = $claimedHostedEvidence.ImageWorkerSbomPath
                    $ImageFrontendSbomEntry = $claimedHostedEvidence.ImageFrontendSbomPath
                    $MigrationEntry = $claimedHostedEvidence.MigrationPath
                    $EvidenceEntry = $claimedHostedEvidence.EvidencePath
                    $WindowsEvidenceEntry = $bundle.EvidencePath
                }
                foreach ($name in $claimedHostedFiles.Keys) {
                    $workflowHash = (Get-FileHash -LiteralPath $claimedHostedFiles[$name] -Algorithm SHA256).Hash
                    $publishedHash = (Get-FileHash -LiteralPath $publishedFiles[$name] -Algorithm SHA256).Hash
                    if ($workflowHash -ine $publishedHash) {
                        throw "Published '$name' is not byte-identical to the claimed workflow artifact payload."
                    }
                }
            }

            $hostedEvidence = Get-Content -LiteralPath $publishedFiles[$EvidenceEntry] -Raw |
                ConvertFrom-Json
            if ([string]$hostedEvidence.release_version -cne $Version -or
                [string]$hostedEvidence.source_commit -ine $tagSha) {
                throw "Published hosted evidence does not match $Version at $tagSha."
            }
            if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27' -and
                ([string]$hostedEvidence.schema_version -cne '1.1' -or
                 [string]$hostedEvidence.evidence_kind -cne 'hosted' -or
                 [string]$hostedEvidence.product_version -cne $Version -or
                 [string]$hostedEvidence.workflow.name -cne $ReleaseGateWorkflowName -or
                 [string]$hostedEvidence.workflow.event -cne 'workflow_dispatch' -or
                 [string]$hostedEvidence.workflow.repository -cne $RepoSlug -or
                 [string]$hostedEvidence.workflow.artifact_name -cne $hostedArtifactName -or
                 [long]$hostedEvidence.workflow.run_id -ne $verifiedHostedRun.Id -or
                 [int]$hostedEvidence.workflow.run_attempt -ne $verifiedHostedRun.RunAttempt -or
                 [string]$hostedEvidence.workflow.run_url -cne $verifiedHostedRun.HtmlUrl)) {
                throw "Published hosted evidence lacks required v0.1.27 workflow/ProductVersion metadata."
            }
            foreach ($gate in @('python', 'frontend', 'hosted_compose', 'backup_rollback')) {
                $property = $hostedEvidence.gates.PSObject.Properties[$gate]
                if ($null -eq $property -or [string]$property.Value -cne 'passed') {
                    throw "Published hosted evidence does not record $gate=passed."
                }
            }
            foreach ($name in @(
                $PythonSbomEntry, $NpmSbomEntry,
                $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
                $MigrationEntry
            )) {
                $record = @($hostedEvidence.files | Where-Object { $_.name -eq $name })
                $actualHash = (Get-FileHash -LiteralPath $publishedFiles[$name] -Algorithm SHA256).Hash
                if ($record.Count -ne 1 -or [string]$record[0].sha256 -ine $actualHash) {
                    throw "Published hosted evidence does not match '$name'."
                }
            }
            $windowsEvidence = Get-Content -LiteralPath $publishedFiles[$WindowsEvidenceEntry] -Raw |
                ConvertFrom-Json
            if ([string]$windowsEvidence.release_version -cne $Version -or
                [string]$windowsEvidence.source_commit -ine $tagSha -or
                [string]$windowsEvidence.gates.windows_build -cne 'passed') {
                throw "Published Windows evidence does not match $Version at $tagSha."
            }
            if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27' -and
                ([string]$windowsEvidence.schema_version -cne '1.1' -or
                 [string]$windowsEvidence.evidence_kind -cne 'windows' -or
                 [string]$windowsEvidence.product_version -cne $Version -or
                 [string]$windowsEvidence.workflow.name -cne $WorkflowName -or
                 [string]$windowsEvidence.workflow.event -cne 'workflow_dispatch' -or
                 [string]$windowsEvidence.workflow.repository -cne $RepoSlug -or
                 [string]$windowsEvidence.workflow.artifact_name -cne $ArtifactName -or
                 [long]$windowsEvidence.workflow.run_id -ne $verifiedRun.Id -or
                 [int]$windowsEvidence.workflow.run_attempt -ne $verifiedRun.RunAttempt -or
                 [string]$windowsEvidence.workflow.run_url -cne $verifiedRun.HtmlUrl)) {
                throw "Published Windows evidence lacks required v0.1.27 workflow/ProductVersion metadata."
            }
        }

        Write-Host ""
        Write-Host "===================== VERIFY SUMMARY ====================="
        Write-Host "  version        : $Version"
        Write-Host "  release        : $($view.url)"
        Write-Host "  tag commit     : $tagSha"
        if ($null -ne $verifiedRun) {
            Write-Host "  verified run   : $($verifiedRun.Id)"
        }
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
    $run.HeadSha = $run.HeadSha.ToLowerInvariant()
    $mainShaBefore = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
    if ($mainShaBefore -ine $run.HeadSha) {
        throw "Remote main is $mainShaBefore, but workflow run $($run.Id) built $($run.HeadSha). Dispatch a fresh bundle from current main."
    }
    if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27') {
        foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
            $localSha = Resolve-LocalCommitSha -Reference $localReference
            if ($localSha -ine $run.HeadSha) {
                throw "Local reference $localReference is $localSha, not release SHA $($run.HeadSha)."
            }
        }
        Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $run.HeadSha
    }
    $shortSha = if ($run.HeadSha.Length -ge 7) { $run.HeadSha.Substring(0, 7) } else { $run.HeadSha }
    Write-Host "    run id     : $($run.Id)"
    Write-Host "    head sha   : $($run.HeadSha) (short $shortSha)"

    # 3. Find the artifact on that run.
    $art = Get-ArtifactInfo -RepoSlug $RepoSlug -RunId $run.Id
    Write-Host "    artifact   : $($art.name) (id $($art.id), $($art.size_in_bytes) bytes)"

    # 4. Download the artifact archive into a fresh short staging dir.
    $stage = New-StageDir -Path $stage -Version $Version
    $zipPath = Join-Path $stage $ZipName
    Write-Host ""
    Write-Host "Downloading artifact archive..."
    Get-ArtifactArchive -ArchiveUrl $art.archive_download_url -OutFile $zipPath
    Assert-ArtifactArchive -Artifact $art -ArchivePath $zipPath `
        -RequireMetadata:$requireV0127

    # 5. Verify the archive before it can reach Releases.
    Write-Host ""
    Write-Host "Verifying bundle zip..."
    $bundle  = Test-BundleZip -ZipPath $zipPath -Version $Version -CommitSha $run.HeadSha `
        -StageDir $stage -WorkflowRunId $run.Id -WorkflowRunAttempt $run.RunAttempt `
        -WorkflowArtifactName $ArtifactName -Repository $RepoSlug
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    $zipLen  = (Get-Item -LiteralPath $zipPath).Length
    $releasePayloads = @($zipPath)
    if ($bundle.EvidencePath) {
        if (-not $PSBoundParameters.ContainsKey('ReleaseGateRunId')) {
            throw "Publishing $Version requires -ReleaseGateRunId from the exact-SHA '$ReleaseGateWorkflowName' workflow."
        }
        $hostedRun = Get-ReleaseGateRunInfo `
            -RepoSlug $RepoSlug -RunId $ReleaseGateRunId -ExpectedSha $run.HeadSha
        $hostedArtifactName = "$Version-release-evidence-$($run.HeadSha)"
        $hostedArtifact = Get-ArtifactInfo `
            -RepoSlug $RepoSlug -RunId $hostedRun.Id -Name $hostedArtifactName
        $hostedArchive = Join-Path $stage 'hosted-release-evidence.zip'
        $hostedRoot = Join-Path $stage 'hosted-release-evidence'
        New-Item -ItemType Directory -Path $hostedRoot -Force | Out-Null
        Get-ArtifactArchive -ArchiveUrl $hostedArtifact.archive_download_url -OutFile $hostedArchive
        Assert-ArtifactArchive -Artifact $hostedArtifact -ArchivePath $hostedArchive `
            -RequireMetadata:$requireV0127
        [System.IO.Compression.ZipFile]::ExtractToDirectory($hostedArchive, $hostedRoot)
        $hostedEvidence = Test-HostedEvidence `
            -Root $hostedRoot -Version $Version -CommitSha $run.HeadSha `
            -WorkflowRunId $hostedRun.Id -WorkflowRunAttempt $hostedRun.RunAttempt `
            -WorkflowArtifactName $hostedArtifactName -Repository $RepoSlug

        $windowsEvidencePath = Join-Path $stage $WindowsEvidenceEntry
        Copy-Item -LiteralPath $bundle.EvidencePath -Destination $windowsEvidencePath
        $releasePayloads += @(
            $hostedEvidence.PythonSbomPath,
            $hostedEvidence.NpmSbomPath,
            $hostedEvidence.ImageApiSbomPath,
            $hostedEvidence.ImageWorkerSbomPath,
            $hostedEvidence.ImageFrontendSbomPath,
            $hostedEvidence.MigrationPath,
            $hostedEvidence.EvidencePath,
            $windowsEvidencePath
        )
        Write-ReleaseChecksums -Path $bundle.ChecksumsPath -Files $releasePayloads
        $releasePayloads += $bundle.ChecksumsPath
    }
    $payloadNames = @($releasePayloads | ForEach-Object { [IO.Path]::GetFileName($_) })
    if (@($payloadNames | Select-Object -Unique).Count -ne $payloadNames.Count) {
        throw "Release payload filenames are not unique."
    }
    Write-Host "    exe SHA-256 : $($bundle.ExeSha256)"
    Write-Host "    zip SHA-256 : $zipHash"

    # 6. Resolve notes tokens.
    Write-Host ""
    Write-Host "Resolving release notes tokens..."
    $notes = Get-Content -LiteralPath $NotesFile -Raw
    # .Replace (literal), not -replace (regex) - the {{...}} braces are literal.
    $notes = $notes.Replace('{{EXE_SHA256}}', $bundle.ExeSha256)
    $notes = $notes.Replace('{{ZIP_SHA256}}', $zipHash)
    $notes = $notes.Replace('{{COMMIT}}', $run.HeadSha)
    foreach ($unresolvedToken in @('{{COMMIT}}', '{{EXE_SHA256}}', '{{ZIP_SHA256}}')) {
        if ($notes.Contains($unresolvedToken)) {
            throw "Release notes still contain unresolved token $unresolvedToken."
        }
    }
    $resolvedNotes = Join-Path $stage 'release-notes-resolved.md'
    [IO.File]::WriteAllText(
        $resolvedNotes,
        $notes,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host "    wrote $resolvedNotes"

    # 7. Create a non-public draft. Every release-side byte is downloaded and
    #    verified while the release is still a draft; publication is the final
    #    mutation only after all identities have been rechecked.
    Write-Host ""
    Write-Host "Creating non-public draft release $Version..."
    $releaseArgs = @(
        'release', 'create', $Version,
        '--repo', $RepoSlug,
        '--verify-tag',
        '--draft',
        '--target', $run.HeadSha,
        '--title', $Title,
        '--notes-file', $resolvedNotes
    )
    $releaseArgs += $releasePayloads
    Invoke-Gh $releaseArgs "gh release create $Version" | ForEach-Object { Write-Host "    $_" }
    $draftCreated = $true

    # 8. Download every draft asset through the authenticated release-asset API
    #    and compare it byte-for-byte with its already-validated workflow input.
    Write-Host ""
    Write-Host "Verifying every draft asset before publication..."
    $viewRaw = Invoke-Gh @(
        'release', 'view', $Version,
        '--repo', $RepoSlug,
        '--json', 'assets,body,targetCommitish,url,isDraft'
    ) "gh release view $Version"
    $view = ($viewRaw -join "`n") | ConvertFrom-Json
    if ($view.isDraft -ne $true) {
        throw "Release $Version became public before verification completed."
    }
    if (@($view.assets).Count -ne $payloadNames.Count) {
        throw "Draft release contains an unexpected number of assets."
    }
    $draftVerifyDir = Join-Path $stage 'draft-asset-verification'
    New-Item -ItemType Directory -Path $draftVerifyDir -Force | Out-Null
    foreach ($localFile in $releasePayloads) {
        $name = [IO.Path]::GetFileName($localFile)
        $draftAsset = Get-UniqueReleaseAsset -Assets @($view.assets) -Name $name
        Assert-ReleaseAssetMatchesFile `
            -Asset $draftAsset -LocalFile $localFile -DownloadDirectory $draftVerifyDir | Out-Null
    }
    Assert-ReleaseBodyDigests `
        -Body ([string]$view.body) `
        -ExeSha256 $bundle.ExeSha256 `
        -ZipSha256 $zipHash `
        -CommitSha $run.HeadSha `
        -ExpectedBody $notes

    # Re-fetch every mutable identity immediately before publication. The
    # workflow artifacts themselves are immutable; matching IDs prove the bytes
    # validated above are still the unique named artifacts on those exact runs.
    $recheckedRun = Get-RunInfo -RepoSlug $RepoSlug -RunId $run.Id -AutoLocate $false
    if ($recheckedRun.HeadSha -ine $run.HeadSha -or
        $recheckedRun.RunAttempt -ne $run.RunAttempt -or
        $recheckedRun.HtmlUrl -cne $run.HtmlUrl) {
        throw "Windows workflow identity changed during draft verification."
    }
    $recheckedArtifact = Get-ArtifactInfo -RepoSlug $RepoSlug -RunId $run.Id -Name $ArtifactName
    if ([long]$recheckedArtifact.id -ne [long]$art.id -or
        [int64]$recheckedArtifact.size_in_bytes -ne [int64]$art.size_in_bytes -or
        [string]$recheckedArtifact.digest -cne [string]$art.digest) {
        throw "Windows workflow artifact identity changed during draft verification."
    }
    if ($bundle.EvidencePath) {
        $recheckedHostedRun = Get-ReleaseGateRunInfo `
            -RepoSlug $RepoSlug -RunId $hostedRun.Id -ExpectedSha $run.HeadSha
        if ($recheckedHostedRun.RunAttempt -ne $hostedRun.RunAttempt -or
            $recheckedHostedRun.HtmlUrl -cne $hostedRun.HtmlUrl) {
            throw "Hosted workflow identity changed during draft verification."
        }
        $recheckedHostedArtifact = Get-ArtifactInfo `
            -RepoSlug $RepoSlug -RunId $hostedRun.Id -Name $hostedArtifactName
        if ([long]$recheckedHostedArtifact.id -ne [long]$hostedArtifact.id -or
            [int64]$recheckedHostedArtifact.size_in_bytes -ne [int64]$hostedArtifact.size_in_bytes -or
            [string]$recheckedHostedArtifact.digest -cne [string]$hostedArtifact.digest) {
            throw "Hosted workflow artifact identity changed during draft verification."
        }
    }
    $tagSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference $Version
    if ($tagSha -ine $run.HeadSha) {
        throw "Draft verification mismatch: tag $Version resolves to $tagSha, not workflow commit $($run.HeadSha)."
    }
    $mainShaAfter = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
    if ($mainShaAfter -ine $run.HeadSha) {
        throw "Draft verification mismatch: remote main is $mainShaAfter, not $($run.HeadSha)."
    }
    foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
        $localSha = Resolve-LocalCommitSha -Reference $localReference
        if ($localSha -ine $run.HeadSha) {
            throw "Draft verification mismatch: $localReference is $localSha, not $($run.HeadSha)."
        }
    }
    Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $run.HeadSha

    $finalViewRaw = Invoke-Gh @(
        'release', 'view', $Version,
        '--repo', $RepoSlug,
        '--json', 'assets,body,targetCommitish,url,isDraft'
    ) "final read of draft release $Version"
    $finalView = ($finalViewRaw -join "`n") | ConvertFrom-Json
    if ($finalView.isDraft -ne $true -or @($finalView.assets).Count -ne $payloadNames.Count) {
        throw "Draft release state or asset count changed before publication."
    }
    Assert-ReleaseBodyDigests `
        -Body ([string]$finalView.body) `
        -ExeSha256 $bundle.ExeSha256 `
        -ZipSha256 $zipHash `
        -CommitSha $run.HeadSha `
        -ExpectedBody $notes
    $finalDraftVerifyDir = Join-Path $stage 'final-draft-asset-verification'
    New-Item -ItemType Directory -Path $finalDraftVerifyDir -Force | Out-Null
    foreach ($localFile in $releasePayloads) {
        $name = [IO.Path]::GetFileName($localFile)
        $finalAsset = Get-UniqueReleaseAsset -Assets @($finalView.assets) -Name $name
        Assert-ReleaseAssetMatchesFile `
            -Asset $finalAsset -LocalFile $localFile -DownloadDirectory $finalDraftVerifyDir | Out-Null
    }
    $view = $finalView

    # Publication is the last mutation. The read-only verification immediately
    # after it proves that GitHub actually published the draft without changing
    # its body or asset identities.
    Invoke-Gh @('release', 'edit', $Version, '--repo', $RepoSlug, '--draft=false') `
        "publish verified draft $Version" | ForEach-Object { Write-Host "    $_" }
    $publishCommandSucceeded = $true
    $publishedViewRaw = Invoke-Gh @(
        'release', 'view', $Version,
        '--repo', $RepoSlug,
        '--json', 'assets,body,targetCommitish,url,isDraft'
    ) "verify published release $Version"
    $publishedView = ($publishedViewRaw -join "`n") | ConvertFrom-Json
    if ($publishedView.isDraft -ne $false -or
        @($publishedView.assets).Count -ne $payloadNames.Count) {
        throw "PUBLICATION VERIFICATION FAILED: release is still draft or its asset count changed."
    }
    Assert-ReleaseBodyDigests `
        -Body ([string]$publishedView.body) `
        -ExeSha256 $bundle.ExeSha256 `
        -ZipSha256 $zipHash `
        -CommitSha $run.HeadSha `
        -ExpectedBody $notes
    foreach ($name in $payloadNames) {
        $draftAsset = Get-UniqueReleaseAsset -Assets @($finalView.assets) -Name $name
        $publishedAsset = Get-UniqueReleaseAsset -Assets @($publishedView.assets) -Name $name
        if ([long]$publishedAsset.id -ne [long]$draftAsset.id -or
            [int64]$publishedAsset.size -ne [int64]$draftAsset.size -or
            [string]$publishedAsset.digest -cne [string]$draftAsset.digest) {
            throw "PUBLICATION VERIFICATION FAILED: asset identity changed for '$name'."
        }
    }
    $publishedTagSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference $Version
    $publishedMainSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
    if ($publishedTagSha -ine $run.HeadSha -or $publishedMainSha -ine $run.HeadSha) {
        throw "PUBLICATION VERIFICATION FAILED: remote main/tag no longer match the workflow SHA."
    }
    foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
        $localSha = Resolve-LocalCommitSha -Reference $localReference
        if ($localSha -ine $run.HeadSha) {
            throw "PUBLICATION VERIFICATION FAILED: $localReference no longer matches the workflow SHA."
        }
    }
    Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $run.HeadSha
    $publishedRun = Get-RunInfo -RepoSlug $RepoSlug -RunId $run.Id -AutoLocate $false
    if ($publishedRun.HeadSha -ine $run.HeadSha -or
        $publishedRun.RunAttempt -ne $run.RunAttempt -or
        $publishedRun.HtmlUrl -cne $run.HtmlUrl) {
        throw "PUBLICATION VERIFICATION FAILED: Windows workflow identity changed."
    }
    $publishedArtifact = Get-ArtifactInfo -RepoSlug $RepoSlug -RunId $run.Id -Name $ArtifactName
    if ([long]$publishedArtifact.id -ne [long]$art.id -or
        [int64]$publishedArtifact.size_in_bytes -ne [int64]$art.size_in_bytes -or
        [string]$publishedArtifact.digest -cne [string]$art.digest) {
        throw "PUBLICATION VERIFICATION FAILED: Windows artifact identity changed."
    }
    if ($bundle.EvidencePath) {
        $publishedHostedRun = Get-ReleaseGateRunInfo `
            -RepoSlug $RepoSlug -RunId $hostedRun.Id -ExpectedSha $run.HeadSha
        if ($publishedHostedRun.RunAttempt -ne $hostedRun.RunAttempt -or
            $publishedHostedRun.HtmlUrl -cne $hostedRun.HtmlUrl) {
            throw "PUBLICATION VERIFICATION FAILED: hosted workflow identity changed."
        }
        $publishedHostedArtifact = Get-ArtifactInfo `
            -RepoSlug $RepoSlug -RunId $hostedRun.Id -Name $hostedArtifactName
        if ([long]$publishedHostedArtifact.id -ne [long]$hostedArtifact.id -or
            [int64]$publishedHostedArtifact.size_in_bytes -ne [int64]$hostedArtifact.size_in_bytes -or
            [string]$publishedHostedArtifact.digest -cne [string]$hostedArtifact.digest) {
            throw "PUBLICATION VERIFICATION FAILED: hosted artifact identity changed."
        }
    }
    $view = $publishedView
    $draftPublished = $true

    # 9. Summary + cleanup.
    Write-Host ""
    Write-Host "===================== RELEASE SUMMARY ===================="
    Write-Host "  version        : $Version"
    Write-Host "  run id         : $($run.Id)"
    Write-Host "  commit         : $shortSha"
    Write-Host "  tag commit     : $tagSha"
    Write-Host "  exe SHA-256    : $($bundle.ExeSha256)"
    Write-Host "  zip SHA-256    : $zipHash"
    Write-Host "  asset          : $ZipName ($zipLen bytes)"
    Write-Host "  release URL    : $($view.url)"
    Write-Host "=========================================================="

    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "PUBLISH OK."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($publishCommandSucceeded -and -not $draftPublished) {
        Write-Host "CRITICAL: GitHub accepted publication, but final public-state verification failed. Inspect or withdraw $Version immediately." -ForegroundColor Red
    }
    elseif ($draftCreated -and -not $draftPublished) {
        Write-Host "Draft $Version remains non-public for inspection or deletion." -ForegroundColor Yellow
    }
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        # Leave the staging dir for forensics on failure (partial download,
        # extracted files, resolved notes).
        Write-Host "Staging dir left for forensics: $stage" -ForegroundColor Yellow
    }
    exit 1
}
