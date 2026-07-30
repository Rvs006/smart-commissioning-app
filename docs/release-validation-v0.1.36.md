# v0.1.36 release validation record

Every unchecked row blocks publication. Field acceptance remains open and
requires the unfiltered scale run and the longest-cadence run in
v0.1.36-field-acceptance-checklist.md.

## Identity and provenance

- [ ] The source commit is merged to main and the working tree is clean.
- [ ] Core, API, worker, frontend, run-context, and report-renderer versions
  are exactly 0.1.36.
- [ ] The Windows workflow is dispatched with v0.1.36 and the exact
  40-character main commit.
- [ ] README_FIRST.txt, EXE ProductVersion, frontend .app-version, API health,
  and release evidence all identify v0.1.36.
- [ ] The signed annotated v0.1.36 tag verifies and points to the same commit.

## Automated gates

- [ ] Core, backend, and worker unit suites pass.
- [ ] Ruff passes for the Python tree and release scripts.
- [ ] Frontend lint, typecheck, unit tests, and Node 24 production build pass.
- [ ] Windows portable build, boot, heartbeat, cancellation, report provenance,
  byte equality, path-with-spaces, and log checks pass.
- [ ] Hosted queued and inline execution, recovery, Sync v2, mTLS, frontend,
  browser-console, backup, rollback, and cleanup checks pass.
- [ ] Windows and hosted evidence validators pass for v0.1.36.

## Live-console evidence gates

- [ ] Expected-topic, alternate-topic, and no-matching-topic outcomes are read
  only from run-summary and topic-discovery evidence and remain distinct.
- [ ] Unavailable evidence displays Waiting for evidence rather than a zero
  count or fabricated trace.
- [ ] The UI/run-state heartbeat is labelled as sampling and never presented as
  a broker-message rate or observation.
- [ ] The accessible run-summary and console elapsed fields remain independently
  testable.

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
