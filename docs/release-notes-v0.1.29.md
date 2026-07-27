# v0.1.29: large MQTT registers keep capturing

This release removes the fixed 500-message ceiling that could stop a shared
register capture before every expected topic had a chance to publish. A
554-asset register with 2,216 concrete validation filters now receives an
automatic capacity of at least 2,216 unless an operator supplies a positive
limit.

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
