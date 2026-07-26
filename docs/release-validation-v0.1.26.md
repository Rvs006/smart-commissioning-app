# v0.1.26 release validation record

Every row is blocking unless it says otherwise. Record the workflow URL and the
40-character release SHA beside each result. Evidence from a different commit
does not count.

I discard any green box that names another SHA.

## Source and migration

- [ ] Local `main`, `origin/main`, tag, PR merge commit, and workflow SHA match.
- [ ] All required GitHub checks belong to that SHA.
- [ ] SQLite migration, idempotent backfill, count comparison, and restore pass.
- [ ] Postgres migration, idempotent backfill, count comparison, and restore pass.
- [ ] Legacy report conflicts are classified without re-signing.
- [ ] Plaintext secret extraction is verified; the pre-migration backup is kept.

## Code and browser

- [ ] Ruff, full core/backend/worker tests, and mypy information run complete.
- [ ] Frontend lint, typecheck, Vitest, and production build pass.
- [ ] Headless Chromium race suite passes with zero console errors.
- [ ] Route switch, SSE identity, cached issues, final topics, report intent,
  session isolation, reordered selection, and failed-run restoration pass.

## Hosted Compose

- [ ] `docker compose config --quiet` passes with the release environment.
- [ ] API has read/write encrypted secrets, artifacts, and report signing.
- [ ] Worker has read-only encrypted secrets and no report-signing mount.
- [ ] Worker readiness proves Redis, schema head, secret-key read, task import,
  and `bacpypes3` import.
- [ ] Production uses queue mode and disables inline fallback.
- [ ] Deterministic UDMI runs in the worker; hosted mTLS resolves shared secrets.
- [ ] BACnet import/startup succeeds in the hosted worker image.

## Portable Windows

- [ ] The workflow checks out and proves the exact release SHA.
- [ ] Fresh build stamps v0.1.26 in README and EXE ProductVersion.
- [ ] Double-click launch passes from a clean extracted directory.
- [ ] SQLite, secrets, artifacts, and signing data land only below
  `%LOCALAPPDATA%\SmartCommissioning`; no Redis service is required.
- [ ] Every route, dropdown, dialog, run, report, download, navigation path, and
  error state is clicked and checked.

## Evidence and publication

- [ ] Python and npm CycloneDX files pass structural and runtime-license checks.
- [ ] API, worker, and frontend image SBOMs are generated for the built images.
- [ ] `SHA256SUMS.txt` verifies every payload asset; the manifest itself is the
  sole intentional exception to avoid a self-referential digest.
- [ ] Backup and rollback smoke passes.
- [ ] Release notes, migration guide, SBOMs, checksum manifest, and portable zip
  are attached to the release.
- [ ] Published asset digests, sizes, tag SHA, workflow SHA, and `origin/main`
  still match after upload.

Use `scripts/check_v0126_release_contracts.py`,
`scripts/validate_release_evidence.py`, and
`scripts/release-portable.ps1 -VerifyExisting` for the machine checks. The
publish command also requires `-ReleaseGateRunId` from the successful hosted
workflow at the same SHA. Keep this record unfinished while any box is blank.

## Provisional working-tree audit (not release evidence)

This record is intentionally separate from the checkboxes above. It describes
a dirty working tree based on `c3091c29d90d7da80c74c0a499fb369184fea0de`
and must be discarded when the signed release SHA exists.

- Python: Ruff and the v0.1.26 contract checker pass. Backend passes 522 tests
  with 1 skip, core passes 571 with 4 skips, and worker passes 14.
- Mypy information run: worker is clean; backend reports 156 existing errors
  and core reports 50. CI keeps these three checks informational.
- Frontend: ESLint, TypeScript, production build, and all 401 Vitest checks pass.
- Browser: 13 desktop, tablet, and mobile routes fit their viewport; 21
  application dropdowns, dialogs, dry-run and real loopback run actions, report
  creation, repeated download, navigation, theme, and a validation error were
  exercised. The console has zero warnings or errors. Active text passes the
  contrast sweep in both themes; disabled controls remain intentionally muted.
- Report download sample: 2,956 bytes with SHA-256
  `4df0d10c31f42506185039ad5b064225c2dd41711ba86af8f3b72c87ef42c502`
  on both HTTP downloads.
- SQLite hostile-data migration and backup/rollback smoke pass. The rollback
  record is `build/rollback-smoke-v0126/backup-rollback-smoke.json`.
- Compose interpolation and structural validation pass with disposable contract
  values. A hosted queue smoke passed earlier in the implementation cycle, but
  Docker access cannot rebuild the final working tree and the result is not
  exact-commit evidence.
- Provisional CycloneDX output parses as schema 1.5: Python has 49 components
  with allowlisted installed licences; npm has 4 production components, all MIT.
- Exact Windows packaging is pending. This machine has PowerShell 5.1, no
  PowerShell 7 command, and no PyInstaller installation. The release workflow
  supplies that clean build environment after a release commit is pushed.
- GitHub CLI authentication is valid. A dedicated SSH signing identity is
  configured and verified locally before publication. No unsigned commit, tag,
  or release is permitted.
