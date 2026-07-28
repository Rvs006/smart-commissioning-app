# v0.1.30 - Live MQTT capture keeps draining

v0.1.30 removes expensive provisional validation work from the MQTT socket
reader. Large register runs can keep receiving state, metadata, and pointset
traffic while progress summaries are prepared and stored separately.

## What changed

- Live progress snapshots are coalesced on one background writer. Only the
  freshest pending view is retained, and work backs off according to its actual
  cost.
- Exact expected topics route directly to their registered asset. Wrong-topic
  and unexpected-publisher aggregation runs off the socket reader rather than
  rescanning a growing capture for every message.
- Snapshot state is frozen coherently before a provisional report is built. A
  stalled progress write has a firm shutdown deadline and cannot replace the
  terminal result after it completes.
- Hosted Postgres connections bound connection, lock, statement, TCP, and idle
  transaction waits. The app can finish a run even when a progress transaction
  stops responding.
- Generate All keeps its four individual reports and, after all four succeed,
  offers one combined ZIP containing the PDF, DOCX, XLSX, and JSON evidence
  pack.
- `scripts/capture_mqtt_evidence.py` provides an append-only independent MQTT
  timeline. It retains exact topic paths and receive times, refuses to overwrite
  evidence, and accepts a password only from process-scoped configuration or a
  hidden prompt.

## Validation boundary

The capture-path defect is covered by a 462-message large-register regression,
including blocked progress storage and late-write protection. This does not
waive publisher conformance findings. Metadata schema errors, wrong topic paths,
silent assets, empty payloads, and unexpected publishers remain visible.

A fresh site run is still required. Use the same approved broker scope for the
app and the append-only collector, then reconcile state, metadata, and pointset
separately. The two-hour run proves scale; a second run must cover the slowest
expected cadence.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it into a new
empty directory, and run `SmartCommissioningApp.exe`. Existing settings and run
history remain under `%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The final portable bundle must be built and boot-tested by the Windows Portable
Bundle workflow from the exact merged release commit.

## Hosted images

Deploy the immutable API, worker, and frontend digest references recorded in
the release evidence. All three must identify the same v0.1.30 commit.

- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

v0.1.30 adds no database migration. Alembic head remains `a7b8c9d0e1f2`.
Follow `docs/migration-rollback-v0.1.30.md` and
`docs/docker-deployment-rollback-v0.1.30.md` for upgrade and rollback checks.
