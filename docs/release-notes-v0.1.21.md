# v0.1.21: field reliability gate

This patch closes the pre-ship findings found in the v0.1.20 site-readiness
review. The BACnet deadlock correction remains unchanged and is now backed by
direct tests of the production timeout adapter.

## What changed

- Worker runs carry a live heartbeat. A killed worker can no longer leave a run
  permanently stuck at `queued` or `running`; IP and BACnet worker timeouts are
  no longer mislabelled as live-capture failures.
- MQTT packet reads respect the full capture deadline, including a packet that
  starts at the edge of the window.
- UDMI treats a one-hour-old timestamp as ambiguous evidence and asks the
  operator to check both clock labelling and publish cadence.
- Offset-less timestamps remain visible as RFC 3339 issues and cannot win the
  `payload_last_seen` ordering by being treated as invented UTC.
- BACnet object-list and present-value timeout wrappers, worker IP/BACnet
  interruption paths, and public-repository hygiene now run in the merge gate.
- Public examples use demo identities only.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it, and run
`SmartCommissioningApp.exe`. Existing settings and run history remain under
`%LOCALAPPDATA%\SmartCommissioning`.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The portable bundle is built and boot-tested by the Windows Portable Bundle
workflow before it is attached here. Real BACnet and MQTT behavior still needs
the normal supervised check against the site's devices and broker.
