# v0.1.29 release validation record

Every unchecked row blocks publication. A green build is necessary, and field
acceptance still needs both the unfiltered two-hour scale run and the
longest-cadence run in `v0.1.29-field-acceptance-checklist.md`.

## Identity and provenance

- [ ] The source commit is merged to `main` and the working tree is clean.
- [ ] Core, API, worker, frontend, run-context, and report-renderer versions are
  exactly `0.1.29`.
- [ ] The Windows workflow is dispatched with `v0.1.29` and the exact 40-character
  `main` commit.
- [ ] `README_FIRST.txt`, EXE ProductVersion, frontend `.app-version`, API health,
  and Windows release evidence all identify v0.1.29.
- [ ] The Windows evidence commit equals the release commit and every required
  gate is `passed`.
- [ ] The annotated `v0.1.29` tag verifies and points to that same commit.

## Automated gates

- [ ] Core, backend, and worker unit suites pass.
- [ ] Ruff passes for the Python tree and release scripts.
- [ ] Frontend lint, typecheck, unit tests, and Node 24 production build pass.
- [ ] Windows portable build, boot, heartbeat, cancellation, report provenance,
  byte-equality, path-with-spaces, and log checks pass.
- [ ] Hosted queued and inline execution, recovery, Sync v2, mTLS, frontend,
  browser-console, backup, rollback, and cleanup checks pass.
- [ ] Windows and hosted evidence validators pass for v0.1.29.

## Field evidence

- [ ] The unchanged 554-row register imports as 554 expected assets.
- [ ] Automatic retained-topic capacity is at least 2,216 and is not fixed at
  500.
- [ ] The two-hour run continues after message 500 and reconciles every asset.
- [ ] The app result is compared with the payload grabber by asset, system,
  payload type, expected topic, and actual topic.
- [ ] Wrong-topic assets are observed, listed once, and retain content validation.
- [ ] PDF, DOCX, XLSX, and ZIP come from one frozen scope and title.
- [ ] XLSX and ZIP contain the annotated frozen register and wrong-topic detail.
- [ ] The longer run covers the slowest expected state, metadata, or pointset
  cadence.
- [ ] A release-machine review confirms DOCX pagination and report readability.

## Publication

- [ ] `scripts/release-portable.ps1` verifies the matching Windows and hosted
  workflow artifacts before creating the release.
- [ ] Release notes contain the exact commit and published SHA-256 values.
- [ ] Published assets are byte-identical to the accepted workflow artifacts.
- [ ] `-VerifyExisting` passes after publication.
