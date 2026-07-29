# v0.1.31 - Asset-topic observation evidence

v0.1.31 is a release candidate for reconciling registered assets with the
topics actually seen during a bounded MQTT validation run. It keeps the
independent raw capture and the application evidence side by side so that a
missing observation can be classified from evidence, rather than guessed from
the final percentage alone.

## What changed

- Asset-topic discovery provides bounded, topic-only evidence for messages
  associated with registered assets and keeps it distinct from unexpected
  publishers.
- The diagnostic is opt-in. It uses the bounded register scope by default;
  an all-topic capture requires explicit operator confirmation and retains no
  payload bodies.
- Observation diagnostics retain the subscribed scope, captured topics, and
  broker outcome needed to distinguish a missing broker message from a
  register-matching or post-capture classification problem.
- The release candidate carries a field checklist for reconciling every asset
  seen by the independent collector against the application result.
- Versioned release, Docker, migration, and evidence-validator contracts now
  require the same v0.1.31 identity across the Windows bundle and hosted stack.

## Validation boundary

The release does not turn an incomplete capture into a successful observation.
For a site acceptance run, compare the app export and append-only MQTT capture
over the same approved scope and time window. Each difference needs one of three
evidence-backed outcomes: no raw message was received, a raw message was outside
the approved scope, or the application failed to associate an in-scope message
with its registered asset.

Asset-topic discovery is forensic evidence only. An asset ID found on another
topic does not make a required UDMI payload observed or valid.

Use the two-hour scale run for volume and a separate run that covers the longest
expected payload cadence. Keep the source register, raw capture, application
export, and report artifacts with their hashes.

## Windows portable download

Download Smart_Commissioning_App_Windows_Portable.zip, extract it into a new
empty directory, and run SmartCommissioningApp.exe. Existing settings and run
history remain under %LOCALAPPDATA%\SmartCommissioning.

- Source commit: {{COMMIT}}
- EXE SHA-256: {{EXE_SHA256}}
- ZIP SHA-256: {{ZIP_SHA256}}

The final portable bundle must be built and boot-tested by the Windows Portable
Bundle workflow from the exact merged release commit.

## Hosted images

Deploy the immutable API, worker, and frontend digest references recorded in
the release evidence. All three must identify the same v0.1.31 commit.

- API: {{API_IMAGE}}@{{API_IMAGE_DIGEST}}
- Worker: {{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}
- Frontend: {{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}

v0.1.31 adds no database migration. Alembic head remains a7b8c9d0e1f2.
Follow docs/migration-rollback-v0.1.31.md and
docs/docker-deployment-rollback-v0.1.31.md for upgrade and rollback checks.
