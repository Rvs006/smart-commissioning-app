# Message to Pete — v0.1.11–v0.1.13 wrap-up (ready to send)

**Written 2026-07-16.** Companion to `docs/release-publishing-handoff.md`.
Self-contained: the message below is final (three drafts iterated down to this
one) and needs no prior conversation to use.

## When and how to send

- **Send AFTER the three GitHub Releases are published** (the job described in
  `docs/release-publishing-handoff.md`) — the message tells Pete the builds are
  up, which is only true once the Releases page shows v0.1.11/12/13.
- Paste as plain text into Teams or email, and add the v0.1.13 Release URL so
  Pete can grab the build directly.
- The message assumes Pete already has **the follow-up questions**
  (`docs/pete-followups-2026-07-16.md`) and **the lab-day runbook**
  (`docs/lab-day-2026-07-20-runbook.md`) — "the runbook" it mentions is that
  file. If those haven't gone to him yet, send them alongside.

## The message

```text
Hi Pete,

Ran your walkthrough notes against what's now in. The early ones (version on
the front page, the logo, import telling you why a row got rejected, the menu
naming) went in with v0.1.11, and the BACnet scan is v0.1.12, Monday's build.
This is the rest of your list, all in the latest build:

- Reports with Electracom headers and footers for the ITP/witnessing pack — in
- A proper handover report per silo that lists what was actually found, not
  just run info — in
- IP scan showing every register entry, including the ones that didn't
  answer — in
- MQTT comparing the whole-broker scan against your template, green for a
  match, red for anything foreign — in
- MQTT able to run over hours or days, same as the UDMI capture — in
- The MQTT inspector actually digging into a topic you click: payload,
  retained flag, QoS, when we saw it — in
- UDMI not binning the whole run over one silent device; it finishes and shows
  that one red as offline — in
- Red/amber/green on the UDMI results, exactly the way you described it — in
- Schema-template download on the validation page for the non-published
  sets — in
- Certs pill reading correctly after you load keys, instead of stuck on
  "not configured" — in
- Logging to a local file or up to a URL, so no AnyDesk just to pull logs off
  site — in
- Placeholder junk gone: Block B Plantroom, the sample dashboard, the fake
  "last backup: success"
- Scrolling stuck inside those bound windows — fixed

Two I've left for you to decide: the amber rule is strict, so a minor-notes
pass shows amber rather than green (say the word and I flip it), and the MQTT
QoS column reads 0 until you okay me raising the subscribe QoS on your live
broker.

One still on you: the general nmap-style scanner. Which ports it checks is the
heart of it, so send me your list and I'll build it round that.

And the catch worth repeating: saved settings don't update themselves, so BBMD
and Foreign Device get set by hand on your kit for the BACnet side. Step one
in the runbook.

Cheers,
Raj
```

## Related docs

- `docs/release-publishing-handoff.md` — publish the Releases first.
- `docs/pete-followups-2026-07-16.md` — the pre-Monday questions (decisions
  shipped with defaults that want Pete's confirmation).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
- `docs/handoff-v0.1.13-remaining-punchlist.md` — what was deferred and why.
