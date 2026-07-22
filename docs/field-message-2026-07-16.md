# Message to field engineer — v0.1.11–v0.1.13 wrap-up (ready to send)

**Written 2026-07-16, updated same day after the Releases went live.** All three
GitHub Releases are published with portable zips attached (v0.1.13 = latest), so
this message is true as written — paste it into Teams or email as-is.

The follow-up questions (`docs/field-followups-2026-07-16.md`) and the lab-day
runbook (`docs/lab-day-2026-07-20-runbook.md`) should reach field engineer alongside this
if he doesn't have them yet — "the runbook" below means that file.

## The message (sent 2026-07-16)

```text
Hi field engineer,

Here's your walkthrough list against the new build. One download, v0.1.13,
has everything:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.13
(Smart_Commissioning_App_Windows_Portable.zip, about 32MB)

Going down your notes:

- Version number on the front page - in
- Electracom logo showing in the app - in
- Menus named properly (IP Discovery, BACnet Discovery, and so on) - in
- Reports appearing on the Reports tab as soon as they're generated - in
- Results staying put when you navigate away and back - in
- Register import telling you which rows got rejected and why, and re-picking
  a fixed file with the same name actually working - in
- BACnet scan reaching past the local subnet by registering with your BBMD as
  a foreign device - in (Monday's fix)
- Electracom headers and footers on reports for the ITP/witnessing pack - in
- Handover reports listing the actual devices, points and topics found, not
  just run info - in
- IP scan keeping the quiet hosts: every register entry shows, and a
  non-answer reads "no response on scanned ports" instead of disappearing - in
- MQTT scan checked against your register: green where a topic matches, red
  where something's publishing that isn't in your template - in
- MQTT capture running over hours or days, same as UDMI - in
- MQTT inspector doing its job: click a topic, get the payload, retained
  flag, QoS and received time - in
- UDMI finishing the run when one device is silent; that device shows red as
  offline instead of binning the lot - in
- Red/amber/green on UDMI results the way you described it - in
- Schema template download on the validation page for the non-published
  sets - in
- Certs pill reading true after you load keys, not stuck on
  "not configured" - in
- Logs to a local file or pushed up to a URL, so no AnyDesk just to pull them
  off site - in
- Placeholder junk removed: Block B Plantroom, the sample dashboard, the fake
  backup status - gone
- Scrolling stuck inside those bound windows - fixed

Two of those shipped with my best guess and want your call: amber is strict
right now (a pass with minor notes shows amber, not green; one line to flip
if you'd rather), and the QoS column reads 0 until you okay me raising the
subscribe QoS on your live broker.

Still on you: a port list for the nmap-style scanner, and BBMD plus Foreign
Device set by hand on your kit before Monday. Step one in the runbook.

Cheers,
Product team
```

## Related docs

- `docs/release-publishing-handoff.md` — the publish job (executed 2026-07-16).
- `docs/field-followups-2026-07-16.md` — the pre-Monday questions (decisions
  shipped with defaults that want field engineer's confirmation).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
- `docs/handoff-v0.1.13-remaining-punchlist.md` — what was deferred and why.
