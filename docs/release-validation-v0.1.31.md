# v0.1.31 release validation record

Every unchecked row blocks publication. Field acceptance also requires the
unfiltered scale run and the longest-cadence run in
v0.1.31-field-acceptance-checklist.md.

## Identity and provenance

- [ ] The source commit is merged to main and the working tree is clean.
- [ ] Core, API, worker, frontend, run-context, and report-renderer versions
  are exactly 0.1.31.
- [ ] The Windows workflow is dispatched with v0.1.31 and the exact
  40-character main commit.
- [ ] README_FIRST.txt, EXE ProductVersion, frontend .app-version, API health,
  and release evidence all identify v0.1.31.
- [ ] The annotated v0.1.31 tag verifies and points to the same commit.

## Automated gates

- [ ] Core, backend, and worker unit suites pass.
- [ ] Ruff passes for the Python tree and release scripts.
- [ ] Frontend lint, typecheck, unit tests, and Node 24 production build pass.
- [ ] Windows portable build, boot, heartbeat, cancellation, report provenance,
  byte equality, path-with-spaces, and log checks pass.
- [ ] Hosted queued and inline execution, recovery, Sync v2, mTLS, frontend,
  browser-console, backup, rollback, and cleanup checks pass.
- [ ] Windows and hosted evidence validators pass for v0.1.31.

## Asset-topic observation gates

- [ ] The unfiltered run records the registered asset total, observed total,
  and every not-observed asset.
- [ ] Asset-topic discovery is opt-in, uses the bounded register scope unless
  an all-topic capture has recorded explicit approval, and retains no payload
  body.
- [ ] For each raw-capture asset that the application did not observe, retain
  the exact topic, receive time, subscription scope, and app classification.
- [ ] A raw message within the approved scope is either associated with its
  registered asset or recorded as a defect with reproducible evidence.
- [ ] Unexpected publishers remain visible and are excluded from expected
  compliance totals.
- [ ] No broker credential is present in source, command arguments, evidence,
  logs, or release artifacts.

## Reports and UI

- [ ] PDF, DOCX, XLSX, and the JSON evidence pack use one frozen title and
  scope.
- [ ] Individual report downloads return the exact stored files.
- [ ] A release-machine review confirms DOCX pagination and report readability.

## Publication

- [ ] scripts/release-portable.ps1 verifies matching Windows and hosted
  workflow artifacts before creating the release.
- [ ] Release notes contain the exact commit and published SHA-256 values.
- [ ] Published assets are byte-identical to accepted workflow artifacts.
- [ ] -VerifyExisting passes after publication.
