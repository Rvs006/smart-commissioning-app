# Message to Pete — v0.1.11–v0.1.13 wrap-up (ready to send)

**Written 2026-07-16, updated same day after the Releases went live.** All three
GitHub Releases are published with portable zips attached (v0.1.13 = latest), so
this message is true as written — paste it into Teams or email as-is.

The follow-up questions (`docs/pete-followups-2026-07-16.md`) and the lab-day
runbook (`docs/lab-day-2026-07-20-runbook.md`) should reach Pete alongside this
if he doesn't have them yet — "the runbook" below means that file.

## The message

```text
Hi Pete,

All three updates are now up on GitHub as proper releases. Grab v0.1.13 only,
here:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.13
(Smart_Commissioning_App_Windows_Portable.zip, about 32MB)

No need to touch v0.1.11 or v0.1.12. Each release contains everything before
it, so 13 has the lot; the other two are just there for the record of what
changed when.

What that one zip gets you:

From 0.1.11: reports actually showing up on the Reports tab, imports telling
you which rows got rejected and why, the logo, the version number on the front
page, tidier menus.

From 0.1.12: the BACnet fix for Monday. The scan can now register with your
BBMD as a foreign device and reach devices past the local subnet, which is
what the third-party browser was doing and we weren't.

New in 0.1.13: Electracom headers and footers on reports, and reports that
list the actual devices, points and topics found. IP scan keeps the quiet
hosts instead of dropping them. MQTT checks the broker against your register
(green matched, red foreign), runs for hours or days, and the inspector shows
payload, retained, QoS and received time. UDMI finishes the run when a device
is silent and shows it red, with the red/amber/green as you described. Plus
honest config pills, log upload, and the placeholder junk gone.

Only reminder: BBMD and Foreign Device still get set by hand on your kit
before Monday. Step one in the runbook.

Cheers,
Raj
```

## Related docs

- `docs/release-publishing-handoff.md` — the publish job (executed 2026-07-16).
- `docs/pete-followups-2026-07-16.md` — the pre-Monday questions (decisions
  shipped with defaults that want Pete's confirmation).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
- `docs/handoff-v0.1.13-remaining-punchlist.md` — what was deferred and why.
