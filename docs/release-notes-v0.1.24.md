# v0.1.24: UDMI results and reports now share one scope

This release applies the July field review to validation, filtering, and every
generated report format.

The supplied evidence included a 460-page, 108-asset report and a separate
72-page, 22-asset report. Comparing those totals as a before-and-after pair would
be misleading. This release makes the selected run and rows explicit instead.

## What changed

- A report now records the exact expected asset and payload rows visible when
  the operator submits it. Text, verdict, topic, system, observation, category,
  and combined filters all update the screen metrics and report scope. The
  server snapshots the derived report model at submission, so a later source
  record change cannot alter a download from that report run.
- Unexpected MQTT publishers inside the bounded register topic scope have their
  own numeric metric and filter. They remain outside expected, observed,
  compliance, Fault Matrix, and validation-detail totals. Their wildcard
  traffic also uses a separate retained-topic budget, so a chatty unregistered
  publisher cannot crowd expected payloads out of the capture.
- Payloads With Issues now counts received expected payloads only. A separate
  Not Received value keeps silence visible without mixing it into received
  evidence.
- UDMI required-field checks continue when `version` is absent and the imported
  register supplies the supported expected version.
- Unit checks use one pinned Google Digital Buildings vocabulary. Valid
  `parts_per_billion` and `ppb` values are accepted, while ppm and ppb remain
  different units.
- Unit findings identify the exact `units` field, so the payload comparison no
  longer marks the parent point name for a unit-only issue.
- RFC 3339 offsets such as `Z`, `+00:00`, and `+01:00` remain valid. One focused
  issue now identifies mixed lexical timestamp styles inside the same payload.
- Register-backed metadata checks report a missing `system.location` container
  separately when the row expects a site or room.
- Project and Site come from the imported register. The generated report run ID
  replaces the former source-run list in human-readable report metadata.
- Metric Definitions now appears before Executive Summary. PDF, DOCX, and XLSX
  tables wrap full finding text, grow with their content, repeat headers where
  needed, and use visible row and column separators.
- Human-readable UDMI tables no longer show Source Run, Severity, or Evidence
  URI. Machine-readable audit output keeps the provenance needed for
  verification.
- Release publication targets the successful bundle workflow's exact commit.
  It rejects PR-built artifacts and stops if the tag, remote main, release-body
  hashes, or workflow commit differ.
- The results page uses three aligned Asset, Payload, and Fault groups. The
  Inspector is tighter, Evidence Outputs is removed, and operator copy says
  Issues instead of Blocking.

No database migration or settings reset is required.

## Compatibility

- The JSON evidence envelope remains schema 1.0. Its nested
  `validation_summary_v1` advances to 1.1 so Not Received payloads and unexpected
  publisher measurements have explicit fields. Stored nested 1.0 summaries remain
  readable. Report generation recomputes Payloads With Issues from complete
  retained rows; compact summaries without those rows are capped at Received.
- Report jobs created on v0.1.24 freeze their evidence at creation. Current
  contract runs store the derived report model, while pre-contract sources store
  the redacted records needed by the legacy renderer. Report jobs that already
  existed before the upgrade are not retroactively snapshotted and keep the
  legacy rebuild-from-source fallback. Create a new report after the upgrade when
  later downloads must use a fixed snapshot.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it, and run
`SmartCommissioningApp.exe`. Existing settings and run history remain under
`%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The Windows Portable Bundle workflow builds and boot-tests the executable before
publication.
