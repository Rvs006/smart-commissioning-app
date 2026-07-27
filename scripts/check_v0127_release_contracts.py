#!/usr/bin/env python3
"""Static checks for the v0.1.27 candidate and exact-SHA publication paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION = "v0.1.27"


def _contains(text: str, value: str, description: str, failures: list[str]) -> None:
    if value not in text:
        failures.append(description)


def _ordered(
    text: str,
    values: tuple[str, ...],
    description: str,
    failures: list[str],
) -> None:
    cursor = -1
    for value in values:
        cursor = text.find(value, cursor + 1)
        if cursor < 0:
            failures.append(description)
            return


def _check_action_pins(
    workflow: str,
    workflow_name: str,
    failures: list[str],
) -> None:
    expected = {
        "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
        "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
        "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "anchore/sbom-action": "e22c389904149dbc22b58101806040fa8d37a610",
    }
    found = 0
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match is None:
            continue
        action = match.group(1)
        if action.startswith("./"):
            continue
        found += 1
        if "@" not in action:
            failures.append(
                f"{workflow_name}:{line_number} action has no immutable ref: {action}"
            )
            continue
        name, reference = action.rsplit("@", 1)
        if re.fullmatch(r"[0-9a-f]{40}", reference) is None:
            failures.append(
                f"{workflow_name}:{line_number} action is not pinned to a full commit SHA"
            )
        elif name in expected and expected[name] != reference:
            failures.append(
                f"{workflow_name}:{line_number} action does not use the reviewed commit"
            )
    if found == 0:
        failures.append(f"{workflow_name} contains no checked third-party action pins")


def check(repo: Path) -> list[str]:
    failures: list[str] = []
    windows = (repo / ".github" / "workflows" / "windows-portable.yml").read_text(
        encoding="utf-8"
    )
    hosted = (repo / ".github" / "workflows" / "release-gates.yml").read_text(
        encoding="utf-8"
    )
    publisher = (repo / "scripts" / "release-portable.ps1").read_text(encoding="utf-8")
    generator = (repo / "scripts" / "generate_release_evidence.py").read_text(
        encoding="utf-8"
    )
    validator = (repo / "scripts" / "validate_v0127_release_evidence.py").read_text(
        encoding="utf-8"
    )
    evidence_tests = (repo / "scripts" / "test_v0127_release_evidence.py").read_text(
        encoding="utf-8"
    )
    portable_smoke_tests = (
        repo / "scripts" / "test_windows_portable_release_smoke.py"
    ).read_text(encoding="utf-8")
    portable_smoke = (repo / "scripts" / "smoke_windows_portable_release.py").read_text(
        encoding="utf-8"
    )

    # The publisher deliberately retains the 0.1.26 evidence-compatibility
    # threshold so read-only verification of that historical release stays strict.
    current_publisher = publisher.replace("[version]'0.1.26'", "")
    for name, text in (
        ("Windows workflow", windows),
        ("hosted workflow", hosted),
        ("publisher", current_publisher),
        ("evidence generator", generator),
    ):
        if "v0.1.26" in text:
            failures.append(f"{name} still contains a v0.1.26 release hardcode")
        _contains(text, VERSION, f"{name} does not identify {VERSION}", failures)

    for text, name in ((windows, "Windows workflow"), (hosted, "hosted workflow")):
        _contains(text, "pull_request:", f"{name} has no PR-head candidate event", failures)
        _contains(
            text,
            "github.event.pull_request.head.sha",
            f"{name} does not bind candidate checks to the PR head SHA",
            failures,
        )
        _check_action_pins(text, name, failures)
        _contains(
            text,
            "git rev-parse origin/main",
            f"{name} does not retain exact-main post-merge protection",
            failures,
        )

    for value, description in (
        ("api/v1/ready", "Windows acceptance omits readiness"),
        ("RUN_LEASE_SECONDS", "Windows acceptance does not shorten the live lease"),
        ("RUN_HEARTBEAT_SECONDS", "Windows acceptance does not shorten heartbeat timing"),
        ("smoke_windows_portable_release.py", "Windows acceptance helper is not run"),
        ("windows-acceptance.json", "Windows acceptance evidence is not retained"),
        ("ProductVersion", "Windows workflow does not prove ProductVersion"),
        ("Portable acceptance path with spaces", "Windows workflow omits path-with-spaces"),
        ("(args.lease_seconds * 2.0) + 10.0", "heartbeat capture does not span two lease windows"),
        ("original_lease_expiry", "heartbeat assertion is not anchored to the database lease"),
        ("sqlite3.connect", "Windows acceptance does not inspect the isolated SQLite lease"),
        ("portable SQLite did not record two heartbeat renewals", "Windows acceptance does not prove multiple renewals"),
        ("lease_expires_at", "Windows acceptance does not prove expiry advancement"),
        ("boundary_heartbeat_age_seconds", "Windows acceptance does not prove boundary freshness"),
        ("continuous_sample_count", "Windows acceptance does not continuously sample heartbeats"),
        (
            "initial SQLite heartbeat renewal had a cadence gap",
            "initial heartbeat proof does not reject early cadence gaps",
        ),
        (
            "original_lease_expiry + timedelta(seconds=boundary_grace_seconds)",
            "continuous monitor does not own the original lease boundary checkpoint",
        ),
        ("last_running_heartbeat_at", "Windows acceptance does not retain a pre-terminal heartbeat"),
        (
            "terminal_from_last_running_heartbeat_seconds",
            "Windows acceptance does not prove terminal freshness from a live heartbeat",
        ),
        ('b\'<div id="root"></div>\'', "Windows acceptance does not verify the app root"),
        ("frontend\\dist\\.app-version", "Windows workflow does not verify the frontend build stamp"),
        ("--gate sqlite_lease_configuration=passed", "SQLite lease proof is not retained as a gate"),
        ("--test portable_app_root=passed", "app-root proof is not retained as a test"),
        ("--test portable_frontend_build_stamp=passed", "frontend stamp proof is not retained"),
        ("--stdout-log $out --stderr-log $err", "Windows acceptance does not scan redirected logs"),
        ("severe JSON log record", "Windows acceptance does not reject ERROR/CRITICAL JSON logs"),
        ("Exception in thread", "Windows acceptance does not reject thread exceptions"),
        ("redirected {label} contains forbidden marker", "redirected logs are not scanned for secrets"),
        (
            '_lease_snapshot(database_path, run_id)["cancel_requested"]',
            "Stop proof trusts the public response instead of private persisted state",
        ),
        (
            "test_windows_portable_release_smoke.py",
            "portable smoke regression tests are not run",
        ),
    ):
        _contains(
            windows + hosted + portable_smoke + portable_smoke_tests,
            value,
            description,
            failures,
        )

    if 'terminal_lease["heartbeat_at"]' in portable_smoke:
        failures.append("terminal proof trusts heartbeat_at rewritten by finalization")
    monitor_call = portable_smoke.find("_monitor_running_heartbeats_until_terminal(", 500)
    boundary_sleep = portable_smoke.find("while datetime.now(UTC) < boundary")
    if boundary_sleep >= 0 or monitor_call < 0:
        failures.append("heartbeat monitoring still has an unobserved pre-boundary sleep")

    for value, description in (
        ("Assert-SignedAnnotatedTag", "publisher does not enforce a signed annotated tag"),
        ("git verify-tag", "publisher does not verify the tag locally"),
        ("verification.verified", "publisher does not require GitHub tag verification"),
        ("workflow_dispatch", "publisher does not retain release-workflow event protection"),
        ("remote main", "publisher does not retain exact-main protection"),
        ("$matches.Count -ne 1", "publisher accepts duplicate named Actions artifacts"),
        ("Assert-ArtifactArchive", "publisher does not verify Actions artifact archives"),
        ("size_in_bytes", "publisher does not compare Actions artifact archive size"),
        ("Actions artifact", "publisher does not compare Actions artifact digest"),
        ("RunAttempt", "publisher does not bind workflow run attempts"),
        ("HtmlUrl", "publisher does not bind exact workflow run URLs"),
        (
            "Published Windows zip is not byte-identical",
            "VerifyExisting does not compare the public zip with the Windows workflow artifact",
        ),
        (
            "claimedHostedFiles",
            "VerifyExisting does not compare published evidence with the hosted workflow artifact",
        ),
        (
            "requires both -RunId and -ReleaseGateRunId",
            "v0.1.27 does not require both claimed workflow runs",
        ),
        (
            "automatic workflow selection is forbidden for publish and verification",
            "v0.1.27 still permits automatic workflow selection",
        ),
        ("[switch]$RequireMetadata", "publisher cannot require Actions artifact metadata"),
        ("has no positive size_in_bytes metadata", "publisher accepts missing artifact size"),
        ("has no SHA-256 digest metadata", "publisher accepts missing artifact digest"),
        ("non-api.github.com URL", "publisher can attach a token to an untrusted artifact URL"),
        ("$apiUri.DnsSafeHost -cne 'api.github.com'", "publisher does not bind artifact URL host"),
        ("$apiUri.Scheme -cne 'https'", "publisher does not require HTTPS artifact URLs"),
        ("$publishCommandSucceeded = $true", "publisher does not track the publish mutation"),
        ("verify published release", "publisher does not re-fetch after publication"),
        ("$publishedView.isDraft -ne $false", "publisher does not prove the draft was published"),
        ("asset identity changed", "publisher does not compare post-publication asset identities"),
        ("PUBLICATION VERIFICATION FAILED", "publisher does not loudly report publication mismatch"),
        ("$publishedMainSha", "post-public verification does not re-resolve remote main"),
        ("$publishedTagSha", "post-public verification does not re-resolve the tag"),
        ("$publishedRun", "post-public verification does not re-fetch the Windows workflow"),
        ("$publishedHostedRun", "post-public verification does not re-fetch hosted workflow"),
        ("$publishedArtifact", "post-public verification does not re-fetch Windows artifact"),
        ("$publishedHostedArtifact", "post-public verification does not re-fetch hosted artifact"),
        ("expected direct TEMP child", "publisher does not constrain recursive stage deletion"),
        ("ReparsePoint", "publisher can recursively delete a reparse-point stage"),
        ("$FrontendVersionEntry", "publisher does not require the frontend build stamp"),
        ("Frontend build stamp", "publisher does not compare the frontend build stamp"),
        ("still contain unresolved token", "publisher accepts unresolved release-note tokens"),
        (
            "[IO.File]::WriteAllText(",
            "publisher does not write resolved release notes as explicit UTF-8",
        ),
        (
            "New-Object Text.UTF8Encoding($false)",
            "publisher can add a PowerShell 5.1 UTF-8 BOM to release notes",
        ),
        ("exact verified 40-character commit", "publisher body checks omit the release commit"),
        ("-CommitSha $run.HeadSha", "draft/public body checks are not bound to the commit"),
    ):
        _contains(publisher, value, description, failures)
    if re.search(
        r"Set-Content\s+-LiteralPath\s+\$resolvedNotes\b",
        publisher,
        flags=re.IGNORECASE,
    ):
        failures.append("publisher writes resolved release notes with a PS5.1 UTF-8 BOM")

    for value, description in (
        ('"schema_version": "1.1"', "evidence schema was not enriched"),
        ('"product_version"', "evidence omits ProductVersion metadata"),
        ('"workflow"', "evidence omits workflow/run metadata"),
        ('"repository"', "evidence omits repository identity"),
        ('"artifact_name"', "evidence omits Actions artifact identity"),
        ('"run_attempt"', "evidence omits workflow attempt identity"),
        ('"tests"', "evidence omits test metadata"),
        ('"gates"', "evidence omits gate metadata"),
    ):
        _contains(generator, value, description, failures)

    for value, description in (
        ("files metadata is missing or empty", "validator accepts empty files metadata"),
        ("SHA256SUMS.txt is missing", "validator does not require SHA256SUMS.txt"),
        ("unsafe evidence file name", "validator does not reject unsafe basenames"),
        ("escapes search root through a symlink", "validator does not reject symlink escapes"),
        ("ambiguous across search roots", "validator accepts ambiguous payload matches"),
        ("REQUIRED_FILES", "validator trusts caller-supplied SBOM kinds"),
        ("required[name] == \"sbom\"", "validator does not infer SBOM validation by name"),
        (
            'f"https://github.com/{args.repository}/actions/runs/{args.workflow_run_id}"',
            "validator does not require the exact GitHub repository/run URL",
        ),
        ('"artifact_name": args.workflow_artifact_name', "validator does not bind artifact name"),
        ('"run_attempt": args.workflow_run_attempt', "validator does not bind run attempt"),
    ):
        _contains(validator, value, description, failures)

    for value, description in (
        ("test_rejects_empty_files_metadata", "tests omit empty-files rejection"),
        ("test_rejects_missing_checksum_manifest", "tests omit missing-manifest rejection"),
        ("test_rejects_traversal_and_absolute_names", "tests omit unsafe-name rejection"),
        ("test_rejects_symlink_escape", "tests omit symlink-escape rejection"),
        ("test_rejects_wrong_required_filename_or_kind", "tests omit SBOM name/kind rejection"),
        (
            "test_rejects_wrong_workflow_host_repo_run_attempt_and_artifact",
            "tests omit exact workflow/artifact identity rejection",
        ),
        ("test_rejects_null_product_version", "tests omit null ProductVersion rejection"),
        ("test_rejects_ambiguous_payload_across_roots", "tests omit ambiguous payload rejection"),
        ("test_rejects_duplicate_file_record", "tests omit duplicate record rejection"),
        (
            "test_generator_retains_v0126_schema_compatibility",
            "tests omit legacy v0.1.26 generator compatibility",
        ),
    ):
        _contains(evidence_tests, value, description, failures)

    _ordered(
        publisher,
        (
            "'release', 'create', $Version",
            "'--draft'",
            "Verifying every draft asset before publication",
            "Assert-ReleaseAssetMatchesFile `",
            "workflow identity changed during draft verification",
            "Assert-SignedAnnotatedTag -RepoSlug $RepoSlug -Version $Version -ExpectedSha $run.HeadSha",
            "final read of draft release",
            "final-draft-asset-verification",
            "'release', 'edit', $Version, '--repo', $RepoSlug, '--draft=false'",
            "verify published release",
            "$draftPublished = $true",
        ),
        "publisher does not create, verify, publish, then verify public state in order",
        failures,
    )
    _ordered(
        publisher,
        (
            "function Get-ArtifactArchive",
            "[Uri]::TryCreate($ArchiveUrl",
            "gh auth token",
        ),
        "publisher reads the GitHub token before validating the artifact URL",
        failures,
    )
    for value, description in (
        ("$draftCreated = $true", "publisher does not track draft creation"),
        ("$draftPublished = $true", "publisher does not track successful publication"),
        ("remains non-public", "publisher failure path does not identify a retained draft"),
        ("$view.isDraft -ne $true", "publisher does not prove verification occurs on a draft"),
        ("-ExpectedBody $notes", "publisher does not compare the exact draft release body"),
        (
            "-RequireMetadata:$requireV0127",
            "v0.1.27 artifact downloads do not fail closed on missing metadata",
        ),
    ):
        _contains(publisher, value, description, failures)

    for value, description in (
        ("--repository $env:GITHUB_REPOSITORY", "Windows evidence omits repository identity"),
        (
            "--workflow-artifact-name SmartCommissioningApp-windows-portable",
            "Windows evidence omits artifact identity",
        ),
        ("--workflow-run-attempt $env:GITHUB_RUN_ATTEMPT", "Windows validator omits run attempt"),
    ):
        _contains(windows, value, description, failures)
    for value, description in (
        ('--repository "$GITHUB_REPOSITORY"', "hosted evidence omits repository identity"),
        ('--workflow-artifact-name "$artifact_name"', "hosted evidence omits artifact identity"),
        ('--workflow-run-attempt "$GITHUB_RUN_ATTEMPT"', "hosted validator omits run attempt"),
    ):
        _contains(hosted, value, description, failures)

    expected_docs = (
        "docs/release-notes-v0.1.27.md",
        "docs/migration-rollback-v0.1.27.md",
        "docs/release-validation-v0.1.27.md",
        "docs/inline-heartbeat-v0.1.27.md",
    )
    for relative in expected_docs:
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.27 release document is missing: {relative}")
    if not re.search(r"name:\s+v0\.1\.27 Release Gates", hosted):
        failures.append("hosted workflow name and publisher release-gate name are not v0.1.27")
    return failures


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = check(repo)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.27 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
