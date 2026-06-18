<#
.SYNOPSIS
    Build the Smart Commissioning App Windows portable bundle (Option A).

.DESCRIPTION
    Turnkey rebuild of the directory-style portable bundle documented in
    docs/portable-bundle-rebuild.md section 4 "Option A". The launcher
    (run_smart_commissioning_app.py) is NOT a self-contained freeze: at startup
    configure_environment() puts sibling source dirs <root>/backend, <root>/core
    and <root>/frontend/dist on sys.path. So the build is:

        1. build the frontend  -> frontend/dist/
        2. PyInstaller freeze   -> dist/SmartCommissioningApp/ (exe + _internal)
        3. assemble bundle dir  -> exe + backend/ + core/ + frontend/dist/

    Shipping the core/ source tree beside the exe makes
    migrate.py::_SOURCE_TREE_ROOT resolve <root>/core/alembic.ini, so a fresh
    SQLite DB migrates to head on first launch (the regression the
    alembic-in-wheel change fixed). This is the same resolution path that already
    works in dev (core installed editable, origin = core/).

    After building, smoke the bundle offline (no broker / Postgres / Redis):
        <OutputDir>\SmartCommissioningApp.exe        # note the printed URL
        pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000

    NOT verified in the dev env (build-box / on-site only): the PyInstaller
    freeze itself, a double-click run of the resulting exe, and Windows
    SmartScreen / AV behaviour on first launch. See docs/portable-bundle-rebuild.md
    section 6.

.PARAMETER OutputDir
    Bundle output directory. Default: build\Smart_Commissioning_App_Windows_Portable
    under the repo root.

.PARAMETER Python
    Python launcher to use for PyInstaller. Default: "python" (build box needs 3.12
    with PyInstaller + all runtime deps installed; see the runbook section 1).

.PARAMETER SkipFrontend
    Reuse an existing frontend/dist instead of running npm ci && npm run build.
    Fails if frontend/dist/index.html is absent.

.PARAMETER SkipFreeze
    Reuse an existing dist/SmartCommissioningApp from a prior PyInstaller run
    instead of freezing again (assemble-only). Fails if it is absent.

.PARAMETER ExtraPyInstallerArgs
    Extra args appended to the PyInstaller invocation (e.g.
    --collect-submodules uvicorn) for the build box to tune hidden imports if a
    frozen run reports a missing module. The default invocation mirrors the
    runbook's documented command.

.PARAMETER Clean
    Remove an existing OutputDir before assembling.

.EXAMPLE
    pwsh packaging/windows_portable/build.ps1

.EXAMPLE
    pwsh packaging/windows_portable/build.ps1 -SkipFrontend -Clean

.EXAMPLE
    # If the frozen exe reports e.g. "No module named uvicorn.protocols.http":
    pwsh packaging/windows_portable/build.ps1 -ExtraPyInstallerArgs '--collect-submodules','uvicorn'
#>
[CmdletBinding()]
param(
    [string]$OutputDir,
    [string]$Python = "python",
    [switch]$SkipFrontend,
    [switch]$SkipFreeze,
    [string[]]$ExtraPyInstallerArgs = @(),
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- locate repo root (this script lives at <root>/packaging/windows_portable) ---
$ScriptDir = $PSScriptRoot
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$AppName   = "SmartCommissioningApp"

if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "build\Smart_Commissioning_App_Windows_Portable"
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$File,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory
    )
    if ($WorkingDirectory) { Push-Location $WorkingDirectory }
    try {
        Write-Host "    $File $($Arguments -join ' ')" -ForegroundColor DarkGray
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed (exit $LASTEXITCODE): $File $($Arguments -join ' ')"
        }
    }
    finally {
        if ($WorkingDirectory) { Pop-Location }
    }
}

# Recursively delete build caches the wheel's MANIFEST.in already prunes, so the
# copied core/ tree stays lean (no __pycache__, *.egg-info, local build/dist).
function Remove-PythonCaches([string]$Root) {
    Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist") -or $_.Name -like "*.egg-info" } |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -LiteralPath $Root -Recurse -Force -File -Include "*.pyc", "*.pyo" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

Write-Host "Smart Commissioning App - Windows portable bundle (Option A)" -ForegroundColor Green
Write-Host "  repo root : $RepoRoot"
Write-Host "  output    : $OutputDir"
Write-Host "  python    : $Python"

# --- preflight: required source dirs ---
$BackendSrc      = Join-Path $RepoRoot "backend"
$CoreSrc         = Join-Path $RepoRoot "core"
$FrontendDistSrc = Join-Path $RepoRoot "frontend\dist"
$LauncherScript  = Join-Path $ScriptDir "run_smart_commissioning_app.py"

foreach ($pair in @(
        @{ Path = $BackendSrc;      What = "backend source tree" },
        @{ Path = $CoreSrc;         What = "core source tree" },
        @{ Path = $LauncherScript;  What = "portable launcher" })) {
    if (-not (Test-Path -LiteralPath $pair.Path)) {
        throw "Missing $($pair.What): $($pair.Path)"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $CoreSrc "alembic.ini"))) {
    throw "core/alembic.ini missing - Option A relies on it for first-launch migration."
}

# --- 1. frontend ---
$FrontendDir = Join-Path $RepoRoot "frontend"
if ($SkipFrontend) {
    Write-Step "Frontend: SKIPPED (-SkipFrontend) - reusing existing dist"
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendDistSrc "index.html"))) {
        throw "frontend/dist/index.html missing but -SkipFrontend was set."
    }
} else {
    Write-Step "Frontend: npm ci && npm run build"
    Invoke-Native -File "npm" -Arguments @("ci")        -WorkingDirectory $FrontendDir
    Invoke-Native -File "npm" -Arguments @("run","build") -WorkingDirectory $FrontendDir
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendDistSrc "index.html"))) {
        throw "frontend build did not produce frontend/dist/index.html."
    }
}

# --- 2. PyInstaller freeze ---
$FreezeDist = Join-Path $RepoRoot "dist\$AppName"
if ($SkipFreeze) {
    Write-Step "PyInstaller: SKIPPED (-SkipFreeze) - reusing dist\$AppName"
    if (-not (Test-Path -LiteralPath $FreezeDist)) {
        throw "dist\$AppName missing but -SkipFreeze was set."
    }
} else {
    Write-Step "PyInstaller: freeze launcher -> dist\$AppName"
    $piArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", $AppName,
        "--console",
        "--distpath", (Join-Path $RepoRoot "dist"),
        "--workpath", (Join-Path $RepoRoot "build\pyinstaller"),
        "--specpath", (Join-Path $RepoRoot "build\pyinstaller")
    )
    $piArgs += $ExtraPyInstallerArgs
    $piArgs += $LauncherScript
    Invoke-Native -File $Python -Arguments $piArgs -WorkingDirectory $RepoRoot
    if (-not (Test-Path -LiteralPath $FreezeDist)) {
        throw "PyInstaller did not produce dist\$AppName (onedir build expected)."
    }
}

# --- 3. assemble bundle dir ---
Write-Step "Assemble bundle: $OutputDir"
if ($Clean -and (Test-Path -LiteralPath $OutputDir)) {
    Write-Host "    removing existing bundle (-Clean)"
    Remove-Item -LiteralPath $OutputDir -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

# 3a. frozen exe + _internal
Write-Host "    copy frozen exe + _internal"
Copy-Item -Path (Join-Path $FreezeDist "*") -Destination $OutputDir -Recurse -Force

# 3b. backend source tree
Write-Host "    copy backend\"
$BackendDst = Join-Path $OutputDir "backend"
if (Test-Path -LiteralPath $BackendDst) { Remove-Item -LiteralPath $BackendDst -Recurse -Force }
Copy-Item -Path $BackendSrc -Destination $BackendDst -Recurse -Force
Remove-PythonCaches $BackendDst

# 3c. core source tree (carries alembic.ini + alembic/versions/*.py for migration)
Write-Host "    copy core\ (+ alembic)"
$CoreDst = Join-Path $OutputDir "core"
if (Test-Path -LiteralPath $CoreDst) { Remove-Item -LiteralPath $CoreDst -Recurse -Force }
Copy-Item -Path $CoreSrc -Destination $CoreDst -Recurse -Force
Remove-PythonCaches $CoreDst
if (-not (Test-Path -LiteralPath (Join-Path $CoreDst "alembic.ini"))) {
    throw "BUG: bundle core\alembic.ini missing after copy - first launch would not migrate."
}

# 3d. built frontend
Write-Host "    copy frontend\dist\"
$FrontendDistDst = Join-Path $OutputDir "frontend\dist"
if (Test-Path -LiteralPath (Join-Path $OutputDir "frontend")) {
    Remove-Item -LiteralPath (Join-Path $OutputDir "frontend") -Recurse -Force
}
New-Item -ItemType Directory -Path (Join-Path $OutputDir "frontend") -Force | Out-Null
Copy-Item -Path $FrontendDistSrc -Destination $FrontendDistDst -Recurse -Force

# 3e. operator note (unsigned tester build)
$ReadmePath = Join-Path $OutputDir "README_FIRST.txt"
@"
Smart Commissioning App - Windows portable (tester build)
=========================================================

To run:
  1. Double-click SmartCommissioningApp.exe (or run it from a terminal).
  2. Keep the console window open. It prints the local URL (e.g.
     http://127.0.0.1:8000/) and opens your browser automatically.
  3. Press Ctrl+C in the console to stop the app.

This build is UNSIGNED. On first launch Windows SmartScreen / antivirus may
warn ("Windows protected your PC"). Choose More info -> Run anyway, or have
your administrator allow it.

Local-only profile: binds 127.0.0.1, SQLite under runtime\, jobs run inline,
no broker / Postgres / Redis / network required. State lives under runtime\
beside the exe (runs, secrets, logs, smart_commissioning.db). Crash logs, if
any, are written to runtime\logs\crash-*.log.
"@ | Set-Content -LiteralPath $ReadmePath -Encoding UTF8

# --- done ---
$BundleExe = Join-Path $OutputDir "$AppName.exe"
Write-Step "DONE"
Write-Host "  bundle : $OutputDir"
Write-Host "  exe    : $BundleExe"
Write-Host ""
Write-Host "Next - offline smoke (no broker):" -ForegroundColor Green
Write-Host "  1. `"$BundleExe`"                       # note the printed URL"
Write-Host "  2. pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000"
Write-Host ""
Write-Host "Expect: SMOKE PASSED N/N checks OK (exit 0). See"
Write-Host "docs/portable-bundle-rebuild.md sections 5-6 for the 6 expected PASS lines"
Write-Host "and what is build-box / on-site only."
