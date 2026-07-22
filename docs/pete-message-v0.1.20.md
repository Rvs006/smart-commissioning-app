# Message to Pete + Dylan — v0.1.20 (ready to send)

**Written 2026-07-22, ahead of the tag.** Send AFTER the v0.1.20 Release is
published with the portable zip attached and marked latest — check the link
resolves first. This is the release that fixes everything from Monday's
on-site day.

## The message

```text
Hi both,

The build that fixes Monday - v0.1.20:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.20
Same zip as always.

The big one: the BACnet scan that hung for 16 minutes is fixed. Your
two Wireshark captures cracked it - they showed the tool did all its
network work in about 8 seconds and then just sat there, which told me
it was stuck, not slow. The cause was a genuine deadlock that triggered
whenever the register had a lot of addresses that didn't answer. Your
41-row register now finishes in seconds. And every network read now has
a time limit, so a device that aborts a big reply or goes quiet can no
longer freeze the whole run.

Also fixed from Monday:
- Stop actually stops now, even mid-device. It keeps what it collected.
- The progress bar shows real progress ("X of Y devices"), so a working
  scan looks different from a stuck one.
- MQTT capture no longer cuts itself short after a few seconds of quiet
  - I think that was behind the "loads didn't publish" you were
  sceptical about. Give it another run and see.
- One device sending a dodgy timestamp used to crash the whole UDMI
  validation. Fixed.
- Timestamp mismatches from a device stamping local time (BST) instead
  of UTC are now flagged as their own category, so they don't drown out
  real faults like a missing point. They still show - just clearly
  labelled as a clock issue, not a data issue.
- The issues panel now sits beside the payload table instead of below
  it, and clicking a row jumps you straight to that payload's issues.
  The View button tells you how many issues it holds.

Worth a fresh run of both a BACnet scan and an MQTT capture on this
build - it should feel like a different tool.

Two things I still need from you when you get a sec:
- Does the lab network have a BBMD, or is it flat?
- Dylan - any of the legacy projects on a UDMI version before 1.5.2?

Cheers,
Raj
```

## Related docs

- `docs/pete-message-v0.1.19.md` — the previous build's message.
- `CHANGELOG.md` `[0.1.20]` — full detail.
