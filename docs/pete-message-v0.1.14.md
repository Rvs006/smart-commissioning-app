# Message to Pete — v0.1.14 (ready to send)

**Written 2026-07-16, after the v0.1.11–13 wrap-up message went out.** The
v0.1.14 Release is published with the portable zip attached and marked latest,
so this is true as written — paste it into Teams or email as-is.

Context for the next session: v0.1.14 is a same-day follow-up to v0.1.13. An
audit sweep found three placeholder leftovers that survived the v0.1.13 purge
(import-template example site name, pre-run sample issues on validation pages,
dead sample rows in the bundle) plus one flaky Windows CI test. No engine or
report behaviour changed. Full detail in `CHANGELOG.md` `[0.1.14]`.

The exe SHA-256 lives in the Release notes, NOT here, on purpose: the hash is
only known after the bundle builds, so pinning it in a repo file would always
leave main one commit ahead of the tag it describes. Release notes can carry
it without a commit.

Pete's environment (2026-07-16): he tests on his **personal laptop**, and the
MSI server has **no ThreatLocker/allowlisting** — so the hash-approval drill
does not apply to his machines (it is only the dev laptop that is locked
down). The message therefore carries the SmartScreen note, not the IT one.

## The message (simplified for a non-technical read)

```text
Hi Pete,

One more build on top of this morning's - v0.1.14. Use this one:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.14
Download: Smart_Commissioning_App_Windows_Portable.zip (about 32MB)

It clears out the last of the demo content that shouldn't have been there:

- The example row in the import templates named a made-up site - it now
  says "Example Site / Plant Room" so it can't be confused with a real one
- The validation pages used to show a few made-up example issues before
  you'd run anything - now they stay empty until you run a validation
- Some leftover demo data inside the app is gone completely

Nothing about how scans, checks or reports work has changed. Already on
v0.1.13? Just replace the app folder with this one - your settings and
results are kept. And any reports for the ITP pack, generate them on this
build.

For the full picture, everything from this week's builds is in this one
download:

- Version number on the front page, Electracom logo and look throughout
- Menus named properly: IP Discovery, BACnet Discovery, MQTT Discovery
- Reports show up in the Reports tab the moment they're generated, with
  Electracom headers and footers and a list of what was actually found
- Every page remembers its last run when you navigate away and back
- Register imports explain exactly which rows were rejected and why, and
  Excel-saved files import without fuss
- BACnet scans reach devices behind your BBMD (foreign device registration)
- IP scan keeps the quiet hosts - "no response on scanned ports" instead of
  disappearing
- MQTT capture over hours or days, checked against your register (green
  matched, red unexpected), with a topic inspector
- UDMI red/amber/green results, one silent device no longer fails the whole
  run, and a schema template download for the non-published sets
- Certificates pill tells the truth, and logs can be saved to file or
  uploaded so nobody needs remote access to fetch them

Windows may warn when you first run it because the app isn't signed - pick
"More info" then "Run anyway", same as the last builds.

Monday is unchanged - same plan, same prep.

Cheers,
Raj
```

## Related docs

- `docs/pete-message-2026-07-16.md` — the v0.1.11–13 wrap-up message (sent).
- `docs/pete-followups-2026-07-16.md` — his open questions (unchanged by this).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
