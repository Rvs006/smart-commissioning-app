# v0.1.23: complete UDMI validation evidence and reporting

This release turns retained UDMI validation data into an operator-readable
result, a portable raw record, and a consistent set of commissioning reports.

## What changed

- The UDMI results screen now shows compact asset, payload, fault, and
  per-system metrics alongside a full expected-payload table and inspector.
- System, observation, and topic-contains filters make large retained runs
  practical to review. Topic matching is partial and case-insensitive.
- Raw validation evidence can be downloaded as deterministic, versioned JSON.
  The export records its limitations: it contains the latest retained payload
  per expected payload type, rather than a complete MQTT message stream.
- A report-title prompt supports site-specific headings without hard-coded site
  data. PDF, DOCX, XLSX, and ZIP reports share the same summary, asset schedule,
  fault matrix, detailed findings, definitions, and source timestamps.
- Reports use `Observed` and `Not observed`. They do not infer network state from
  the absence of a payload.
- Failed or cancelled source runs remain available as partial evidence and are
  labelled incomplete. Running runs cannot be exported or used as report
  sources.
- Spreadsheet output treats source text as data, preventing formula execution
  from register, issue, or report-title values.

The expected reporting interval grammar for `60S`, `10M`, and `COV,48H` remains
outside this release. That needs a separate capture-duration and event-driven
validation design.

No database migration or settings reset is required.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it, and run
`SmartCommissioningApp.exe`. Existing settings and run history remain under
`%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The portable bundle is built and boot-tested by the Windows Portable Bundle
workflow before publication.
