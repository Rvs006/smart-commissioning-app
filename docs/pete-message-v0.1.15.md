# Message to Pete — v0.1.15 (ready to send)

**Written 2026-07-17, ahead of the tag.** Send AFTER the v0.1.15 Release is
published with the portable zip attached and marked latest — check the link
resolves first.

Context for the next session: v0.1.15 is a single fix ahead of Monday's lab
session (2026-07-20). The "Source Interface not present / is down" errors at
run creation, and the Configuration page's missing-adapter hint, all advised
falling back to "Auto (OS default route)" — advice a BACnet scan cannot
follow, because a real Who-Is must bind one specific adapter. All three now
give the engine-neutral version: re-select a current adapter; Auto also works
for IP and MQTT runs, while a BACnet scan requires a specific adapter. No
engine or report behaviour changed. Full detail in `CHANGELOG.md` `[0.1.15]`.

The exe SHA-256 lives in the Release notes, NOT here, on purpose (same
reasoning as v0.1.14: the hash is only known after the bundle builds, and a
repo file pinning it would always trail the tag it describes).

Pete's environment (2026-07-16): personal laptop and the MSI server, no
ThreatLocker/allowlisting — SmartScreen note applies to him, not the IT
hash-approval drill.

## The message (simplified for a non-technical read)

```text
Hi Pete,

Small update before Monday - v0.1.15. Use this one:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.15
Download: Smart_Commissioning_App_Windows_Portable.zip (about 32MB)

One fix in it: if the app tells you your network adapter has gone
missing or is down, the error now gives advice that actually works for
BACnet. The old wording suggested switching to "Auto" - fine for IP and
MQTT scans, but a BACnet scan always needs a real adapter picked on the
Configuration page. The messages now say exactly that, so nobody gets
sent in a circle on Monday.

Everything else is unchanged from v0.1.14.

Cheers,
Raj
```

## Related docs

- `docs/pete-message-v0.1.14.md` — the previous build's message (sent).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
