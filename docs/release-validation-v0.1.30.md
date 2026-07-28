# v0.1.30 release validation record

Every unchecked row blocks publication. Field acceptance also requires the
unfiltered scale run and the longest-cadence run in
`v0.1.30-field-acceptance-checklist.md`.

## Identity and provenance

- [ ] The source commit is merged to `main` and the working tree is clean.
- [ ] Core, API, worker, frontend, run-context, and report-renderer versions are
  exactly `0.1.30`.
- [ ] The Windows workflow is dispatched with `v0.1.30` and the exact
  40-character `main` commit.
- [ ] `README_FIRST.txt`, EXE ProductVersion, frontend `.app-version`, API
  health, and release evidence all identify v0.1.30.
- [ ] The annotated `v0.1.30` tag verifies and points to the same commit.

## Automated gates

- [ ] Core, backend, and worker unit suites pass.
- [ ] Ruff passes for the Python tree and release scripts.
- [ ] Frontend lint, typecheck, unit tests, and Node 24 production build pass.
- [ ] Windows portable build, boot, heartbeat, cancellation, report provenance,
  byte equality, path-with-spaces, and log checks pass.
- [ ] Hosted queued and inline execution, recovery, Sync v2, mTLS, frontend,
  browser-console, backup, rollback, and cleanup checks pass.
- [ ] Windows and hosted evidence validators pass for v0.1.30.

## MQTT capture gates

- [ ] A burst containing 462 distinct exact pointsets is retained without the
  progress store blocking the MQTT reader.
- [ ] Provisional asset and message counts describe one coherent snapshot.
- [ ] A stuck provisional write cannot block or overwrite terminal evidence.
- [ ] Wrong-topic and unexpected-publisher evidence remains classified and is
  not hidden from results.
- [ ] The independent collector retains repeated messages on one exact topic as
  separate JSONL records and refuses to overwrite an existing file.
- [ ] No broker credential is present in source, command arguments, evidence,
  logs, or release artifacts.

## Reports and UI

- [ ] Generate All produces PDF, DOCX, XLSX, and the JSON evidence pack from one
  frozen title and scope.
- [ ] A fully successful Generate All run offers one combined ZIP containing all
  four outputs.
- [ ] Individual report downloads still return the exact stored files.
- [ ] Failed or partial Generate All runs do not claim a complete bundle.
- [ ] A release-machine review confirms DOCX pagination and report readability.

## Publication

- [ ] `scripts/release-portable.ps1` verifies matching Windows and hosted
  workflow artifacts before creating the release.
- [ ] Release notes contain the exact commit and published SHA-256 values.
- [ ] Published assets are byte-identical to accepted workflow artifacts.
- [ ] `-VerifyExisting` passes after publication.
