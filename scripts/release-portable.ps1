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
    Successful "<version> Release Gates" workflow run that built and tested the
    hosted images. v0.1.28 also binds immutable GHCR digests and their SBOMs.
    Its 40-character head SHA must equal the Windows run and release tag.

.PARAMETER NotesFile
    Markdown release-notes template (required for publishing). Commit, Windows
    hashes, and (for v0.1.28) immutable container image tokens are substituted.

.PARAMETER Title
    Release title. Defaults to $Version.

.PARAMETER RepoSlug
    owner/repo. Defaults to Rvs006/smart-commissioning-app.

.PARAMETER VerifyExisting
    Skip publishing. Instead download all required already-published assets for
    -Version and re-verify their digests, evidence, SBOMs, and contained exe
    hash. A harmless, read-only way to exercise this script end to end.

.PARAMETER PrepareOnly
    Perform the publish-path artifact, provenance, notes, and checksum checks,
    then print the final publish command without creating or publishing a
    GitHub Release. This is the required pre-authorization path.

.PARAMETER Historical
    Publish or verify a Windows-only rebuild from the historical workflow. The
    original tag SHA remains the release identity, while the workflow may apply
    a documented release-blocker compatibility patch. Historical releases do
    not claim hosted image evidence or signed-tag verification.

.EXAMPLE
    # Publish v0.1.28 from matching Windows and hosted release-gate runs:
    powershell -NoProfile -File scripts\release-portable.ps1 -Version v0.1.28 -RunId 123456789 -ReleaseGateRunId 123456790 -NotesFile docs\release-notes-v0.1.28.md

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

    [Parameter(ParameterSetName = 'Publish')]
    [switch]$PrepareOnly,

    [string]$Title,

    [string]$RepoSlug = 'Rvs006/smart-commissioning-app',

    [Parameter(Mandatory, ParameterSetName = 'Verify')]
    [switch]$VerifyExisting,

    [Parameter(ParameterSetName = 'Publish')]
    [Parameter(ParameterSetName = 'Verify')]
    [switch]$Historical
)

$ErrorActionPreference = 'Stop'

# Some GitHub/Azure endpoints refuse pre-TLS1.2; PS 5.1 does not always enable it
# by default. -bor so we add Tls12 without dropping whatever is already enabled.
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# --- constants tied to the workflow + build.ps1 output ---
$WorkflowName = if ($Historical) { 'Historical Windows Portable Bundle' } else { 'Windows Portable Bundle' }
$WorkflowPath = if ($Historical) { '.github/workflows/historical-windows-portable.yml' } else { '.github/workflows/windows-portable.yml' }
$ArtifactName = 'SmartCommissioningApp-windows-portable'        # upload-artifact `name:`
$ReleaseGateWorkflowName = "$Version Release Gates"
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
$DockerImageEvidenceEntry = 'docker-image-evidence.json'
$SyncWireEntry = 'SYNC_V2_WIRE_FORMAT.md'
$SyncCredentialEntry = 'SYNC_V2_CREDENTIAL_SCOPE.md'
$SyncOperationsEntry = 'SYNC_V2_OPERATIONS.md'
$DockerDeploymentEntry = 'DOCKER_DEPLOYMENT_ROLLBACK.md'
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
    if ($run.name -ne $WorkflowName) {
        throw "Run $RunId is '$($run.name)', not the $WorkflowName workflow."
    }
    if ($Historical) {
        if ($run.path -cne '.github/workflows/historical-windows-portable.yml') {
            throw "Run $RunId uses '$($run.path)', not the historical Windows workflow path."
        }
    } elseif ($run.path -cne '.github/workflows/windows-portable.yml') {
        throw "Run $RunId uses '$($run.path)', not the canonical Windows workflow path."
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
    if ($run.name -ne $ReleaseGateWorkflowName -or $run.path -cne '.github/workflows/release-gates.yml') {
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

function Assert-CleanReleaseRepository {
    $root = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace([string]$root)) {
        throw 'release-portable.ps1 must run inside the release Git repository.'
    }
    $root = [IO.Path]::GetFullPath(([string]$root).Trim())
    $expectedScript = [IO.Path]::GetFullPath((Join-Path $root 'scripts\release-portable.ps1'))
    $actualScript = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PSCommandPath).Path)
    if (-not $actualScript.Equals($expectedScript, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The running publisher is '$actualScript', not the tracked repository publisher '$expectedScript'."
    }
    $status = @(& git status --porcelain=v1 --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not prove the release repository worktree is clean.'
    }
    if ($status.Count -ne 0) {
        throw 'Release publication and verification require a clean worktree, including no untracked files.'
    }
    return $root
}

function Assert-ReleaseCheckoutAtCommit {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$CommitSha
    )
    $headSha = Resolve-LocalCommitSha -Reference 'HEAD'
    if ($headSha -ine $CommitSha) {
        throw "The publisher checkout HEAD is $headSha, not exact release SHA $CommitSha."
    }
    $relative = 'scripts/release-portable.ps1'
    $actualPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PSCommandPath).Path)
    $expectedPath = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $relative))
    if (-not $actualPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The running publisher is not the canonical tracked '$relative'."
    }
    $commitBlob = (& git rev-parse "$CommitSha`:$relative" 2>$null)
    $workingBlob = (& git hash-object "--path=$relative" -- $actualPath 2>$null)
    if ($LASTEXITCODE -ne 0 -or $commitBlob -notmatch '^[0-9a-fA-F]{40}$' -or
        $workingBlob -notmatch '^[0-9a-fA-F]{40}$' -or
        $workingBlob.Trim() -ine $commitBlob.Trim()) {
        throw "The running publisher is not byte-equivalent to '$relative' at $CommitSha."
    }
}

function Assert-TrackedReleaseNotes {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha
    )
    $relative = "docs/release-notes-$Version.md"
    $expectedPath = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $relative))
    $actualPath = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
    if (-not $actualPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "NotesFile must be the canonical tracked release notes '$expectedPath'."
    }
    $commitBlob = (& git rev-parse "$CommitSha`:$relative" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $commitBlob -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Release commit $CommitSha has no tracked release-notes blob at '$relative'."
    }
    # --path applies normal clean filters before hashing, so a clean Windows
    # checkout with CRLF still compares to the reviewed Git blob.
    $workingBlob = (& git hash-object "--path=$relative" -- $actualPath 2>$null)
    if ($LASTEXITCODE -ne 0 -or $workingBlob -notmatch '^[0-9a-fA-F]{40}$' -or
        $workingBlob.Trim() -ine $commitBlob.Trim()) {
        throw "NotesFile is not byte-equivalent to '$relative' at exact release SHA $CommitSha."
    }
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
        [Parameter(Mandatory)][string]$ExpectedSha,
        [switch]$AllowUnsigned
    )

    $localTagObject = (& git rev-parse "refs/tags/$Version^{tag}" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $localTagObject -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Local tag $Version is missing or lightweight; create a signed annotated tag first."
    }
    if ($AllowUnsigned) {
        $localTarget = (& git rev-parse "$Version^{commit}").Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $localTarget -ine $ExpectedSha) {
            throw "Local historical tag $Version targets $localTarget, not exact release SHA $ExpectedSha."
        }
        $refRaw = Invoke-Gh @('api', "repos/$RepoSlug/git/ref/tags/$Version") `
            "resolve GitHub tag ref '$Version'"
        $ref = ($refRaw -join "`n") | ConvertFrom-Json
        if ([string]$ref.object.type -cne 'tag') {
            throw "GitHub historical tag $Version is lightweight; an annotated tag is required."
        }
        if ([string]$ref.object.sha -ine $localTagObject.Trim()) {
            throw "GitHub tag object '$($ref.object.sha)' does not match local '$($localTagObject.Trim())'."
        }
        Write-Host '    annotated tag : accepted for historical rebuild (signature not present)'
        return
    }

    # A successful SSH signature check writes "Good git signature" to stderr.
    # Windows PowerShell 5.1 promotes native stderr records according to
    # $ErrorActionPreference, so the script-wide Stop setting would throw before
    # we could inspect git's successful exit code. Relax it only around this
    # native call, capture both streams, then restore the fail-closed preference.
    $savedVerificationPreference = $ErrorActionPreference
    $verificationOutput = @()
    $verificationExitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        $verificationOutput = @(& git verify-tag $Version 2>&1)
        $verificationExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedVerificationPreference
    }
    $verificationOutput | ForEach-Object { Write-Host "    $_" }
    if ($verificationExitCode -ne 0) {
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
        [AllowEmptyString()][string]$ExpectedBody,
        [string[]]$ExpectedImageReferences = @()
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
    foreach ($expectedReference in $ExpectedImageReferences) {
        if ($expectedReference -notmatch '^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$' -or
            $Body.IndexOf($expectedReference, [StringComparison]::Ordinal) -lt 0) {
            throw "Release notes do not contain the exact immutable image reference $expectedReference."
        }
    }
    if ($PSBoundParameters.ContainsKey('ExpectedBody')) {
        $actualText = $Body.Replace("`r`n", "`n").TrimEnd([char[]]"`r`n")
        $expectedText = $ExpectedBody.Replace("`r`n", "`n").TrimEnd([char[]]"`r`n")
        if ($actualText -cne $expectedText) {
            throw "Release notes body is not the exact resolved notes content."
        }
    }
}

function Get-TrackedReleaseNotesTemplate {
    param(
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha
    )
    $relative = "docs/release-notes-$Version.md"
    if ($Historical) {
        $workingPath = Join-Path (Get-Location) $relative
        if (-not (Test-Path -LiteralPath $workingPath)) {
            throw "Historical release notes are missing at '$workingPath'."
        }
        return Get-Content -LiteralPath $workingPath -Raw
    }
    $blob = (& git rev-parse "$CommitSha`:$relative" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $blob -notmatch '^[0-9a-fA-F]{40}$') {
        throw "Release commit $CommitSha has no reviewed release-notes blob at '$relative'."
    }
    # Read the blob through an explicitly UTF-8 native-process stream. Windows
    # PowerShell 5.1 otherwise decodes native stdout through the console code
    # page, which can corrupt reviewed curly quotes or non-ASCII punctuation.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git.exe'
    $startInfo.Arguments = "cat-file blob $($blob.Trim())"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = New-Object Text.UTF8Encoding($false, $true)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Could not start git to read reviewed release notes."
        }
        $template = $process.StandardOutput.ReadToEnd()
        $process.StandardError.ReadToEnd() | Out-Null
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Could not read reviewed release notes '$relative' from $CommitSha."
        }
    }
    finally {
        $process.Dispose()
    }
    return $template
}

function Resolve-ReleaseNotesBody {
    param(
        [Parameter(Mandatory)][string]$Template,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha,
        [Parameter(Mandatory)][string]$ExeSha256,
        [Parameter(Mandatory)][string]$ZipSha256,
        $DockerImageEvidence
    )
    $replacements = @(
        [pscustomobject]@{ Token = '{{COMMIT}}'; Value = $CommitSha },
        [pscustomobject]@{ Token = '{{EXE_SHA256}}'; Value = $ExeSha256 },
        [pscustomobject]@{ Token = '{{ZIP_SHA256}}'; Value = $ZipSha256 }
    )
    $imageReferences = @()
    if (-not $Historical -and [version]($Version.TrimStart('v')) -ge [version]'0.1.28') {
        if ($null -eq $DockerImageEvidence) {
            throw "$Version release notes require verified Docker image evidence."
        }
        foreach ($role in @('api', 'worker', 'frontend')) {
            $property = $DockerImageEvidence.images.PSObject.Properties[$role]
            if ($null -eq $property) {
                throw "Docker image evidence has no '$role' record for release notes."
            }
            $image = $property.Value
            $upperRole = $role.ToUpperInvariant()
            $replacements += @(
                [pscustomobject]@{ Token = "{{$upperRole`_IMAGE}}"; Value = [string]$image.name },
                [pscustomobject]@{ Token = "{{$upperRole`_IMAGE_DIGEST}}"; Value = [string]$image.digest }
            )
            $imageReferences += "$([string]$image.name)@$([string]$image.digest)"
        }
    }

    $resolved = $Template
    foreach ($replacement in $replacements) {
        if (-not $Template.Contains([string]$replacement.Token)) {
            throw "Release notes template is missing required token $($replacement.Token)."
        }
        $resolved = $resolved.Replace(
            [string]$replacement.Token,
            [string]$replacement.Value
        )
        if ($resolved.Contains([string]$replacement.Token)) {
            throw "Release notes still contain unresolved token $($replacement.Token)."
        }
    }
    if ($resolved -match '\{\{[A-Z0-9_]+\}\}') {
        throw "Release notes still contain an unknown unresolved token '$($Matches[0])'."
    }
    return [pscustomobject]@{
        Body = $resolved
        ImageReferences = @($imageReferences)
    }
}

function Test-DockerImageEvidence {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$CommitSha,
        [Parameter(Mandatory)][string]$Repository
    )
    $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $rootKeys = @($document.PSObject.Properties.Name | Sort-Object) -join ','
    if ($rootKeys -cne 'images,registry,release_version,schema_version,source_commit') {
        throw 'Docker image evidence root contains missing or unknown fields.'
    }
    if ([string]$document.schema_version -cne '1.0' -or
        [string]$document.release_version -cne $Version -or
        [string]$document.source_commit -ine $CommitSha -or
        [string]$document.registry -cne 'ghcr.io') {
        throw "Docker image evidence does not identify $Version at $CommitSha in GHCR."
    }
    $imageRoleKeys = @($document.images.PSObject.Properties.Name | Sort-Object) -join ','
    if ($imageRoleKeys -cne 'api,frontend,worker') {
        throw 'Docker image evidence must contain exactly api, worker, and frontend.'
    }
    $seenImageNames = @{}
    foreach ($role in @('api', 'worker', 'frontend')) {
        $entryProperty = $document.images.PSObject.Properties[$role]
        if ($null -eq $entryProperty) {
            throw "Docker image evidence is missing '$role'."
        }
        $entry = $entryProperty.Value
        $entryKeys = @($entry.PSObject.Properties.Name | Sort-Object) -join ','
        if ($entryKeys -cne 'digest,immutable_reference,labels,name') {
            throw "Docker image evidence '$role' contains missing or unknown fields."
        }
        $name = [string]$entry.name
        $digest = [string]$entry.digest
        $expectedName = "ghcr.io/$($Repository.ToLowerInvariant())-$role"
        if ($name -notmatch '^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+$' -or $name.Contains('@') -or
            $digest -notmatch '^sha256:[0-9a-f]{64}$' -or
            [string]$entry.immutable_reference -cne "$name@$digest") {
            throw "Docker image evidence has an invalid immutable reference for '$role'."
        }
        if ($name -cne $expectedName -or $seenImageNames.ContainsKey($name)) {
            throw "Docker image evidence does not bind '$role' to its unique canonical image name '$expectedName'."
        }
        $seenImageNames[$name] = $true
        $labelKeys = @($entry.labels.PSObject.Properties.Name | Sort-Object) -join ','
        if ($labelKeys -cne 'org.opencontainers.image.revision,org.opencontainers.image.source,org.opencontainers.image.version') {
            throw "Docker image '$role' labels contain missing or unknown fields."
        }
        if ([string]$entry.labels.'org.opencontainers.image.version' -cne $Version -or
            [string]$entry.labels.'org.opencontainers.image.revision' -ine $CommitSha -or
            [string]$entry.labels.'org.opencontainers.image.source' -cne "https://github.com/$Repository") {
            throw "Docker image '$role' lacks exact version, revision, or source OCI labels."
        }
    }
    return $document
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

function Assert-ReleaseViewUnchanged {
    param(
        [Parameter(Mandatory)]$Initial,
        [Parameter(Mandatory)]$Current,
        [Parameter(Mandatory)][string[]]$ExpectedAssetNames
    )
    if ([string]$Current.body -cne [string]$Initial.body -or
        [string]$Current.url -cne [string]$Initial.url -or
        [string]$Current.targetCommitish -cne [string]$Initial.targetCommitish -or
        [bool]$Current.isDraft -ne [bool]$Initial.isDraft) {
        throw 'Release body or release identity changed during verification.'
    }
    if (@($Initial.assets).Count -ne $ExpectedAssetNames.Count -or
        @($Current.assets).Count -ne $ExpectedAssetNames.Count) {
        throw 'Release asset count changed during verification.'
    }
    foreach ($name in $ExpectedAssetNames) {
        $before = Get-UniqueReleaseAsset -Assets @($Initial.assets) -Name $name
        $after = Get-UniqueReleaseAsset -Assets @($Current.assets) -Name $name
        if ([string]::IsNullOrWhiteSpace([string]$before.id) -or
            [string]::IsNullOrWhiteSpace([string]$after.id) -or
            [string]$after.id -cne [string]$before.id -or
            [int64]$after.size -ne [int64]$before.size -or
            [string]$after.digest -cne [string]$before.digest) {
            throw "Release asset identity changed during verification for '$name'."
        }
    }
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

        # Reject archive names that extract ambiguously or outside the target.
        # Windows treats names case-insensitively, so even byte-distinct ZIP
        # entries such as App.exe and app.exe are a collision.
        $seenEntryNames = @{}
        foreach ($entry in $entries) {
            $entryName = [string]$entry.FullName
            if ([string]::IsNullOrWhiteSpace($entryName) -or
                $entryName.StartsWith('/') -or
                $entryName.Contains(':') -or
                $entryName.Contains('//')) {
                throw "Bundle zip contains an unsafe entry name ('$entryName')."
            }
            $segments = @($entryName.Split('/'))
            $pathSegments = @($segments | Where-Object { $_ -cne '' })
            if (@($pathSegments | Where-Object { $_ -ceq '.' -or $_ -ceq '..' }).Count -gt 0) {
                throw "Bundle zip contains a dot-segment entry name ('$entryName')."
            }
            $entryKey = $entryName.ToLowerInvariant()
            if ($seenEntryNames.ContainsKey($entryKey)) {
                throw "Bundle zip contains duplicate or case-colliding entry names ('$entryName')."
            }
            $seenEntryNames[$entryKey] = $true
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
                    throw "Windows evidence lacks the strict schema, kind, or ProductVersion metadata."
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
            throw "Hosted evidence lacks the strict schema, kind, or ProductVersion metadata."
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

    $requireV0128Evidence = ([version]($Version.TrimStart('v')) -ge [version]'0.1.28')
    $requiredNames = @(
        $PythonSbomEntry, $NpmSbomEntry,
        $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
        $MigrationEntry
    )
    if ($requireV0128Evidence) {
        $requiredNames += @(
            $DockerImageEvidenceEntry,
            $SyncWireEntry, $SyncCredentialEntry, $SyncOperationsEntry,
            $DockerDeploymentEntry
        )
    }
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
    $dockerImageEvidence = $null
    if ($requireV0128Evidence) {
        $dockerImageEvidence = Test-DockerImageEvidence `
            -Path $paths[$DockerImageEvidenceEntry] -Version $Version `
            -CommitSha $CommitSha -Repository $Repository
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
        DockerImageEvidencePath = $(if ($requireV0128Evidence) { $paths[$DockerImageEvidenceEntry] } else { $null })
        SyncWirePath = $(if ($requireV0128Evidence) { $paths[$SyncWireEntry] } else { $null })
        SyncCredentialPath = $(if ($requireV0128Evidence) { $paths[$SyncCredentialEntry] } else { $null })
        SyncOperationsPath = $(if ($requireV0128Evidence) { $paths[$SyncOperationsEntry] } else { $null })
        DockerDeploymentPath = $(if ($requireV0128Evidence) { $paths[$DockerDeploymentEntry] } else { $null })
        DockerImageEvidence = $dockerImageEvidence
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
    Write-Host "mode     : $(if ($VerifyExisting) { 'VERIFY (read-only)' } elseif ($PrepareOnly) { 'PREPARE (no release mutation)' } else { 'PUBLISH' })"

    $requireV0127 = ([version]($Version.TrimStart('v')) -ge [version]'0.1.27')
    $requireV0128 = ([version]($Version.TrimStart('v')) -ge [version]'0.1.28')
    $repositoryRoot = $null
    if ($requireV0127 -and -not $Historical) {
        $repositoryRoot = Assert-CleanReleaseRepository
    }
    if ($requireV0127 -and -not $Historical -and
        (-not $PSBoundParameters.ContainsKey('RunId') -or $RunId -le 0 -or
         -not $PSBoundParameters.ContainsKey('ReleaseGateRunId') -or $ReleaseGateRunId -le 0)) {
        throw "$Version requires both -RunId and -ReleaseGateRunId; automatic workflow selection is forbidden for publish and verification."
    }
    if ($Historical -and (-not $PSBoundParameters.ContainsKey('RunId') -or $RunId -le 0)) {
        throw "$Version historical publication requires the exact historical workflow -RunId."
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
            # Historical releases remain independently verifiable after main
            # advances. The immutable tag, claimed workflow runs, evidence, and
            # public bytes must still agree; current main is intentionally not
            # part of a read-only historical verification.
            Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $tagSha -AllowUnsigned:$Historical
        }
        $verifiedRun = $null
        $verifiedHostedRun = $null
        $claimedWindowsArchive = $null
        $claimedHostedEvidence = $null
        $publishedDockerEvidence = $null
        $expectedAssetNames = @($ZipName)
        if ($PSBoundParameters.ContainsKey('RunId')) {
            $verifiedRun = Get-RunInfo -RepoSlug $RepoSlug -RunId $RunId -AutoLocate $false
            if (-not $Historical -and $tagSha -ine $verifiedRun.HeadSha) {
                throw "Release tag $Version resolves to $tagSha, but workflow run $RunId built $($verifiedRun.HeadSha)."
            }
        }

        if ($requireV0127 -and -not $Historical) {
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
        if ($requireV0127 -and -not $Historical) {
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
        if ($bundle.EvidencePath -and -not $Historical) {
            if (-not $requireV0127 -and $PSBoundParameters.ContainsKey('ReleaseGateRunId')) {
                Get-ReleaseGateRunInfo `
                    -RepoSlug $RepoSlug -RunId $ReleaseGateRunId -ExpectedSha $tagSha | Out-Null
            }
            $payloadNames = @(
                $ZipName, $PythonSbomEntry, $NpmSbomEntry,
                $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
                $MigrationEntry, $EvidenceEntry, $WindowsEvidenceEntry
            )
            if ($requireV0128) {
                $payloadNames += @(
                    $DockerImageEvidenceEntry,
                    $SyncWireEntry, $SyncCredentialEntry, $SyncOperationsEntry,
                    $DockerDeploymentEntry
                )
            }
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
                if ($requireV0128) {
                    $claimedHostedFiles[$DockerImageEvidenceEntry] = $claimedHostedEvidence.DockerImageEvidencePath
                    $claimedHostedFiles[$SyncWireEntry] = $claimedHostedEvidence.SyncWirePath
                    $claimedHostedFiles[$SyncCredentialEntry] = $claimedHostedEvidence.SyncCredentialPath
                    $claimedHostedFiles[$SyncOperationsEntry] = $claimedHostedEvidence.SyncOperationsPath
                    $claimedHostedFiles[$DockerDeploymentEntry] = $claimedHostedEvidence.DockerDeploymentPath
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
                throw "Published hosted evidence lacks required workflow/ProductVersion metadata."
            }
            foreach ($gate in @('python', 'frontend', 'hosted_compose', 'backup_rollback')) {
                $property = $hostedEvidence.gates.PSObject.Properties[$gate]
                if ($null -eq $property -or [string]$property.Value -cne 'passed') {
                    throw "Published hosted evidence does not record $gate=passed."
                }
            }
            $hostedFileNames = @(
                $PythonSbomEntry, $NpmSbomEntry,
                $ImageApiSbomEntry, $ImageWorkerSbomEntry, $ImageFrontendSbomEntry,
                $MigrationEntry
            )
            if ($requireV0128) {
                $hostedFileNames += @(
                    $DockerImageEvidenceEntry, $SyncWireEntry, $SyncCredentialEntry,
                    $SyncOperationsEntry, $DockerDeploymentEntry
                )
            }
            foreach ($name in $hostedFileNames) {
                $record = @($hostedEvidence.files | Where-Object { $_.name -eq $name })
                $actualHash = (Get-FileHash -LiteralPath $publishedFiles[$name] -Algorithm SHA256).Hash
                if ($record.Count -ne 1 -or [string]$record[0].sha256 -ine $actualHash) {
                    throw "Published hosted evidence does not match '$name'."
                }
            }
            if ($requireV0128) {
                $publishedDockerEvidence = Test-DockerImageEvidence `
                    -Path $publishedFiles[$DockerImageEvidenceEntry] `
                    -Version $Version -CommitSha $tagSha -Repository $RepoSlug
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
                throw "Published Windows evidence lacks required workflow/ProductVersion metadata."
            }
        }

        $resolvedVerificationNotes = $null
        if ($requireV0127) {
            if (-not $bundle.EvidencePath -and -not $Historical) {
                throw "$Version has no retained Windows evidence for exact release-body verification."
            }
            $reviewedTemplate = Get-TrackedReleaseNotesTemplate `
                -Version $Version -CommitSha $tagSha
            $resolvedVerificationNotes = Resolve-ReleaseNotesBody `
                -Template $reviewedTemplate `
                -Version $Version `
                -CommitSha $tagSha `
                -ExeSha256 $bundle.ExeSha256 `
                -ZipSha256 $zipHash `
                -DockerImageEvidence $publishedDockerEvidence
            Assert-ReleaseBodyDigests `
                -Body ([string]$view.body) `
                -ExeSha256 $bundle.ExeSha256 `
                -ZipSha256 $zipHash `
                -CommitSha $tagSha `
                -ExpectedBody ([string]$resolvedVerificationNotes.Body) `
                -ExpectedImageReferences @($resolvedVerificationNotes.ImageReferences)
        }

        # Re-fetch mutable release state after all downloads and validation.
        # A concurrent body edit or asset replacement must not be reported as a
        # successful verification of the stale snapshot read at the start.
        $finalVerifyRaw = Invoke-Gh @(
            'release', 'view', $Version,
            '--repo', $RepoSlug,
            '--json', 'assets,body,url,targetCommitish,isDraft'
        ) "final read-only verification of release $Version"
        $finalVerifyView = ($finalVerifyRaw -join "`n") | ConvertFrom-Json
        Assert-ReleaseViewUnchanged `
            -Initial $view -Current $finalVerifyView `
            -ExpectedAssetNames $expectedAssetNames
        if ($requireV0127) {
            Assert-ReleaseBodyDigests `
                -Body ([string]$finalVerifyView.body) `
                -ExeSha256 $bundle.ExeSha256 `
                -ZipSha256 $zipHash `
                -CommitSha $tagSha `
                -ExpectedBody ([string]$resolvedVerificationNotes.Body) `
                -ExpectedImageReferences @($resolvedVerificationNotes.ImageReferences)
        }
        $view = $finalVerifyView

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
    $releaseSha = if ($Historical) {
        Resolve-CommitSha -RepoSlug $RepoSlug -Reference $Version
    } else {
        $run.HeadSha
    }
    if (-not $Historical) {
        $mainShaBefore = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
        if ($mainShaBefore -ine $run.HeadSha) {
            throw "Remote main is $mainShaBefore, but workflow run $($run.Id) built $($run.HeadSha). Dispatch a fresh bundle from current main."
        }
    }
    if ([version]($Version.TrimStart('v')) -ge [version]'0.1.27' -and -not $Historical) {
        foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
            $localSha = Resolve-LocalCommitSha -Reference $localReference
            if ($localSha -ine $run.HeadSha) {
                throw "Local reference $localReference is $localSha, not release SHA $($run.HeadSha)."
            }
        }
        Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $run.HeadSha
        Assert-ReleaseCheckoutAtCommit `
            -RepositoryRoot $repositoryRoot -CommitSha $run.HeadSha
        Assert-TrackedReleaseNotes `
            -RepositoryRoot $repositoryRoot -Path $NotesFile `
            -Version $Version -CommitSha $run.HeadSha
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
    $bundle  = Test-BundleZip -ZipPath $zipPath -Version $Version -CommitSha $releaseSha `
        -StageDir $stage -WorkflowRunId $run.Id -WorkflowRunAttempt $run.RunAttempt `
        -WorkflowArtifactName $ArtifactName -Repository $RepoSlug
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    $zipLen  = (Get-Item -LiteralPath $zipPath).Length
    $releasePayloads = @($zipPath)
    if ($bundle.EvidencePath -and -not $Historical) {
        if (-not $PSBoundParameters.ContainsKey('ReleaseGateRunId')) {
            throw "Publishing $Version requires -ReleaseGateRunId from the exact-SHA '$ReleaseGateWorkflowName' workflow."
        }
        $hostedRun = Get-ReleaseGateRunInfo `
            -RepoSlug $RepoSlug -RunId $ReleaseGateRunId -ExpectedSha $releaseSha
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
        if ($requireV0128) {
            $releasePayloads += @(
                $hostedEvidence.DockerImageEvidencePath,
                $hostedEvidence.SyncWirePath,
                $hostedEvidence.SyncCredentialPath,
                $hostedEvidence.SyncOperationsPath,
                $hostedEvidence.DockerDeploymentPath
            )
        }
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
    $notesTemplate = Get-Content -LiteralPath $NotesFile -Raw
    $resolvedNotesResult = Resolve-ReleaseNotesBody `
        -Template $notesTemplate `
        -Version $Version `
        -CommitSha $releaseSha `
        -ExeSha256 $bundle.ExeSha256 `
        -ZipSha256 $zipHash `
        -DockerImageEvidence $(if ($requireV0128) { $hostedEvidence.DockerImageEvidence } else { $null })
    $notes = [string]$resolvedNotesResult.Body
    $expectedImageReferences = @($resolvedNotesResult.ImageReferences)
    $resolvedNotes = Join-Path $stage 'release-notes-resolved.md'
    [IO.File]::WriteAllText(
        $resolvedNotes,
        $notes,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host "    wrote $resolvedNotes"

    if ($PrepareOnly) {
        $publishNotesPath = [IO.Path]::GetFullPath($NotesFile)
        $publishCommand = @(
            'powershell -NoProfile -File scripts/release-portable.ps1',
            "-Version $Version",
            "-RunId $($run.Id)",
            "-ReleaseGateRunId $(if ($hostedRun) { $hostedRun.Id } else { '<RELEASE_GATE_RUN_ID>' })",
            "-NotesFile `"$publishNotesPath`""
        ) -join ' '
        Write-Host ""
        Write-Host "PREPARE ONLY OK - no GitHub release was created or published." -ForegroundColor Green
        Write-Host "Verified source commit: $releaseSha"
        Write-Host "Verified portable ZIP SHA-256: $zipHash"
        Write-Host "Final publish command after explicit authorization:"
        Write-Host "  $publishCommand"
        Remove-Item -LiteralPath $stage -Recurse -Force
        exit 0
    }

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
        '--target', $releaseSha,
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
        -CommitSha $releaseSha `
        -ExpectedBody $notes `
        -ExpectedImageReferences $expectedImageReferences

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
    if ($bundle.EvidencePath -and -not $Historical) {
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
    if ($tagSha -ine $releaseSha) {
        throw "Draft verification mismatch: tag $Version resolves to $tagSha, not release source $releaseSha."
    }
    if (-not $Historical) {
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
    }
    Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $releaseSha -AllowUnsigned:$Historical

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
        -CommitSha $releaseSha `
        -ExpectedBody $notes `
        -ExpectedImageReferences $expectedImageReferences
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
        -CommitSha $releaseSha `
        -ExpectedBody $notes `
        -ExpectedImageReferences $expectedImageReferences
    foreach ($name in $payloadNames) {
        $draftAsset = Get-UniqueReleaseAsset -Assets @($finalView.assets) -Name $name
        $publishedAsset = Get-UniqueReleaseAsset -Assets @($publishedView.assets) -Name $name
        # `gh release view --json assets` exposes GraphQL node IDs such as
        # RA_kwDOS4_ANM4dQnKb. They are stable identity strings, not the numeric
        # REST asset IDs embedded in apiUrl. A numeric cast would reject a valid
        # publication before the remaining public-state checks can run.
        $publishedAssetId = [string]$publishedAsset.id
        $draftAssetId = [string]$draftAsset.id
        if ([string]::IsNullOrWhiteSpace($publishedAssetId) -or
            [string]::IsNullOrWhiteSpace($draftAssetId) -or
            $publishedAssetId -cne $draftAssetId -or
            [int64]$publishedAsset.size -ne [int64]$draftAsset.size -or
            [string]$publishedAsset.digest -cne [string]$draftAsset.digest) {
            throw "PUBLICATION VERIFICATION FAILED: asset identity changed for '$name'."
        }
    }
    $publishedTagSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference $Version
    if ($publishedTagSha -ine $releaseSha) {
        throw "PUBLICATION VERIFICATION FAILED: remote tag no longer matches the release source SHA."
    }
    if (-not $Historical) {
        $publishedMainSha = Resolve-CommitSha -RepoSlug $RepoSlug -Reference 'main'
        if ($publishedMainSha -ine $run.HeadSha) {
            throw "PUBLICATION VERIFICATION FAILED: remote main no longer matches the workflow SHA."
        }
        foreach ($localReference in @('refs/heads/main', 'refs/remotes/origin/main')) {
            $localSha = Resolve-LocalCommitSha -Reference $localReference
            if ($localSha -ine $run.HeadSha) {
                throw "PUBLICATION VERIFICATION FAILED: $localReference no longer matches the workflow SHA."
            }
        }
    }
    Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $releaseSha -AllowUnsigned:$Historical
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
    if ($bundle.EvidencePath -and -not $Historical) {
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
