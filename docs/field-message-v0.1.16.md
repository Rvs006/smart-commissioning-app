# Message to field engineer — v0.1.16 (ready to send)

**Written 2026-07-17, ahead of the tag.** Send AFTER the v0.1.16 Release is
published with the portable zip attached and marked latest — check the link
resolves first.

Context for the next session: v0.1.16 is the fix bundle from the 2026-07-17
live session on field engineer's flat (no-BBMD) network. His Wireshark capture proved
the scan worked on the wire while every real run 500'd and froze at
"running" — raw bacpypes3 values failing JSON persistence, plus a run-wrapper
double-fault that skipped the terminal status. His log bundle came back empty
(500 tracebacks never reached `app.log`), which is also fixed. His run
history shows five runs stuck at "running"; the new startup sweep reclaims
them, and the message below warns him so it does not look like new breakage.
Full detail in `CHANGELOG.md` `[0.1.16]`.

The exe SHA-256 lives in the Release notes, NOT here (same reasoning as
v0.1.14/15: the hash is only known after the bundle builds).

## The message (simplified for a non-technical read, flat-network steps)

```text
Hi field engineer,

New build with the fixes from our call - v0.1.16:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.16
Grab Smart_Commissioning_App_Windows_Portable.zip, same as before.

Your Wireshark capture told the whole story. The scan itself worked -
the broadcast went out, both devices answered, and the app read their
points - but SAVING the results is what blew up. That's why you got
the 500 error and runs stuck at "Running" forever. Both are fixed:
real scan values save properly now, and nothing can leave a run stuck
at "Running" any more - if something goes wrong it ends as a proper
failure with a plain-English reason.

Your log bundle also came back empty - errors never reached the log
file. Fixed too: next time anything misbehaves, that same "Download
log bundle" button will actually contain the answer.

One heads-up: the first time you start the new build, your five old
stuck "Running" runs will flip to failed with a note saying they were
interrupted. That's the cleanup working, not new breakage.

Scanning your network is simpler than my last message said (that one
assumed a BBMD, which you don't have - ignore its steps 2 and 5):

1. Configuration page: Source Interface = your wired adapter (not
   Auto), then Save Configuration.
2. Leave Foreign Device and the BBMD fields alone. They're only for
   sites with a BBMD. If the dry-run plan says "local broadcast only",
   that is CORRECT for your network - it does not mean the save failed.
3. Tick the authorisation box and Run. You should see both your
   devices with their points this time.

One question for Monday: does the lab network have a BBMD in it, or is
it flat like yours? If there's no BBMD I'll rework the session notes
before Monday so they don't send anyone in circles.

Cheers,
Product team
```

## Related docs

- `docs/field-message-v0.1.15.md` — the previous build's message (sent; carries
  a correction note about its BBMD steps).
- `docs/protocol-conformance.md` §3 — the recorded BBMD-optional requirement.
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session (rewrite
  pending field engineer's BBMD answer).
