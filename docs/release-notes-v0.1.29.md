# v0.1.29 - Large MQTT registers keep capturing

A 554-asset register expands to 2,216 concrete validation filters. v0.1.29 gives
that shared capture at least 2,216 retained-message slots unless the operator
sets a positive limit, removing the old fixed ceiling of 500.

## What changed

- Shared MQTT capture uses the larger of 500 or the number of concrete
  validation filters when no positive `max_messages` value is supplied.
  Explicit positive limits remain unchanged.
- Registered assets found on a wrong MQTT topic are matched by one exact,
  case-sensitive topic segment inside a bounded parent scope. The app never adds
  a bare `#` subscription for this check and does not guess when an identity is
  ambiguous.
- Wrong-topic assets remain observed, keep their payload content-validation
  results, and appear with expected and actual topic evidence in the app and
  generated reports.
- A payload that never arrived reads `Not received`. An absent pointset no
  longer creates one synthetic finding for every expected point, while a
  received empty object is still validated.
- XLSX and ZIP outputs include an annotated copy of the exact frozen input
  register. The XLSX adds `Wrong Topic Assets` and `Annotated Input Register`;
  the ZIP adds `wrong_topic_assets.json` and `annotated_input_register.json`.
  All original register columns stay first, followed by observation and
  topic-review columns.
- Engineers can delete one report or a validated batch after explicit
  confirmation. Source runs and shared content-addressed evidence remain
  intact.
- Generate All creates PDF, DOCX, XLSX, and ZIP reports from one frozen title
  and scope. Partial failures name the formats that did not complete.
- Payloads with eight or more findings provide a keyboard-focusable jump to the
  matching expected-versus-observed control. Reduced-motion preferences are
  respected.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it into a new
empty directory, and run `SmartCommissioningApp.exe`. Existing settings and run
history remain under `%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The portable bundle must be built and boot-tested by the Windows Portable Bundle
workflow before publication. Field acceptance still requires an unfiltered
554-asset two-hour run and a second run covering the longest expected payload
cadence, preferably 24 hours when daily state or metadata is expected.

## Hosted images

The hosted gate rebuilds API, worker, and frontend images from the same release
commit. Deploy the immutable digest references recorded in the release evidence:

- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

v0.1.29 adds no database migration. Hosted deployments stay on Alembic head
`a7b8c9d0e1f2`; follow `docs/migration-rollback-v0.1.29.md` and
`docs/docker-deployment-rollback-v0.1.29.md` for upgrade and rollback checks.
