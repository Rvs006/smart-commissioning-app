# v0.1.25 - The Workbench keeps the view you chose

Three filtered devices could become the full imported register while the report
title dialog was open. v0.1.25 freezes the selected rows when report generation
starts and applies the 24 July field review to the Workbench.

## What changed

- Clicking an asset selects it and refreshes the Inspector immediately. View
  Issues uses that same path and focuses the Inspector. The extra Result detail
  popup added a click without adding evidence, so it has been removed.
- The Inspector issue viewport is roughly twice as tall on desktop and keeps a
  viewport-safe cap on smaller windows.
- Asset, Payload, and Fault metrics sit in three distinct tonal groups with
  centered headings, labels, and values. The compact title, compliance, and
  Issues header is restored without bringing back the old Blocking wording.
- A report captures the exact active filter scope when Generate report is
  clicked. Later changes made while the title dialog is open cannot alter that
  snapshot. Empty filtered views remain empty rather than falling back to every
  imported asset.
- A populated Unexpected Devices list now wins over a stale numeric zero in both
  the Workbench and report model. Compact historical snapshots that never stored
  the list still use their saved count.
- A custom report title is visible in Generated Reports and is carried into the
  downloaded filename. Unsafe path characters are removed, length is bounded,
  and the report run ID keeps duplicate titles unique.
- Expected payload timestamps are identified as schema-template values created
  when the expected view is built. Freshness uses the observed payload timestamp
  against broker receipt or capture time, including after a long run. The
  expected side keeps its own template value and never borrows device data.
- Missing or malformed MQTT broker settings now report
  `broker_not_configured`. DNS resolver failures report
  `dns_resolution_failed`. Existing TLS, authentication, timeout, subscription,
  and connection-refused classifications keep their prior order and wording.

No database migration or settings reset is required.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it, and run
`SmartCommissioningApp.exe`. Existing settings and run history remain under
`%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The Windows Portable Bundle workflow builds and boot-tests the executable before
publication.
