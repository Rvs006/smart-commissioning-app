# v0.1.36 - Evidence-led live monitoring and release integrity

v0.1.36 packages the report-list correction and embedded live-console evidence
work delivered after the previous candidate, then restores the versioned release
contracts needed to publish the exact Windows and hosted artifacts. It does not
claim field acceptance.

## What changed

- Report listing and deletion now create a database Session before entering its
  context manager, restoring the report-list API path without changing its
  single-query projection.
- The active-run console presents registered-asset observation evidence from
  the authoritative run summary and topic discovery: expected-topic observed,
  alternate-topic observed, and no matching topic observed remain distinct.
- The console's rolling asset view is based on real observed and expected asset
  counts. When the required evidence is unavailable, it says **Waiting for
  evidence** rather than showing a fabricated zero or trace.
- The independent per-second display pulse remains available to show that the
  UI is sampling run state. It is explicitly not a broker-message rate or proof
  that a broker message was received.
- The accessible run-summary and live-console elapsed labels are both retained;
  their test assertions now target their intended surfaces.
- Core, API, worker, frontend, report-renderer, portable, hosted, and evidence
  contracts now share the v0.1.36 identity required for an auditable release.

## Validation and field boundary

Publication requires the exact-SHA Windows portable build and hosted release
gates, immutable image evidence, and a signed annotated tag. The portable
offline smoke exercises local API and dry-run paths only. Neither it nor CI
proves live broker, publisher, device, or site observations.

Field acceptance remains open until the unfiltered scale run and the
longest-cadence run pass the [v0.1.36 field acceptance checklist](v0.1.36-field-acceptance-checklist.md).
The display heartbeat must never be treated as broker evidence.

## Windows portable download

Download Smart_Commissioning_App_Windows_Portable.zip, extract it into a new
empty directory, and run SmartCommissioningApp.exe. Existing settings and run
history remain under %LOCALAPPDATA%\SmartCommissioning.

- Source commit: {{COMMIT}}
- EXE SHA-256: {{EXE_SHA256}}
- ZIP SHA-256: {{ZIP_SHA256}}

The final portable bundle is built and boot-tested by the Windows Portable
Bundle workflow from the exact merged release commit.

## Hosted images

Deploy only the immutable API, worker, and frontend digest references recorded
in the release evidence. All three identify the same v0.1.36 commit.

- API: {{API_IMAGE}}@{{API_IMAGE_DIGEST}}
- Worker: {{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}
- Frontend: {{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}

v0.1.36 adds no database migration. Alembic head remains a7b8c9d0e1f2. Follow
[the migration and rollback guide](migration-rollback-v0.1.36.md) and
[the Docker deployment and rollback guide](docker-deployment-rollback-v0.1.36.md)
for upgrade and rollback checks.
