# v0.1.22: database-outage-safe worker recovery

This patch removes the last known false-failure race in the worker-heartbeat
recovery added for v0.1.21.

## What changed

- A stale worker heartbeat now enters a two-minute confirmation window before
  the run becomes terminal.
- Any fresh worker lifecycle, result-summary, or issue write cancels the pending
  stale decision. A live scan is therefore not failed just because the API wins
  the first database lock after an outage.
- A worker that remains silent throughout the confirmation window is still
  marked `failed` with an explicit incomplete-result message. The run is never
  reported as successful without evidence.
- The regression suite covers startup recovery, normal polling, a late live
  heartbeat, and a late progress/result write without using real-time sleeps.

No database migration or settings reset is required.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it, and run
`SmartCommissioningApp.exe`. Existing settings and run history remain under
`%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The portable bundle is built and boot-tested by the Windows Portable Bundle
workflow before publication. Start on site with one supervised BACnet scan,
followed by the ten-minute MQTT/UDMI capture.
