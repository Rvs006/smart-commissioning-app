# Message to Pete — v0.1.15 (ready to send)

**Written 2026-07-17, ahead of the tag.** Send AFTER the v0.1.15 Release is
published with the portable zip attached and marked latest — check the link
resolves first.

> **CORRECTION (2026-07-17, after the live session):** steps 2 and 5 of the
> message below assume the target network sits behind a BBMD (the 2026-07-20
> lab premise). Pete's own test network is a flat single subnet with **no
> BBMD** — on such networks Foreign Device stays **Disabled**, the BBMD fields
> are left alone, and a dry-run plan saying "local broadcast only" is the
> CORRECT, healthy state — not a failed save. Following step 2 as written
> misdirected the 2026-07-17 session (Foreign Device enabled against a
> non-existent BBMD fails loudly, as designed). A BBMD is optional per site;
> the requirement is recorded in `docs/protocol-conformance.md` §3. The
> message text below is preserved exactly as sent.

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

## The message (simplified for a non-technical read, BACnet steps included)

```text
Hi Pete,

New build before Monday - v0.1.15:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.15
Grab Smart_Commissioning_App_Windows_Portable.zip, same as last time.

One fix in it. When the app can't find your network adapter it used to
tell you to switch to "Auto", which quietly breaks BACnet scans - they
need a real adapter picked. The error now tells you the right thing to
do instead, so nobody chases their tail on Monday.

For the BACnet discovery itself:

1. Configuration page first. Under Network Basics, set Source Interface
   to your wired adapter from the list (not Auto, not blank).
2. Same page, BACnet section: switch Foreign Device to Enabled, put in
   the BBMD's real IP (just the IP, no port), leave the port at 47808
   and TTL at 300. Ignore the separate "BBMD" toggle, it doesn't do
   anything anymore.
3. Hit Validate Snapshot, then Save Configuration. Nothing counts until
   you press Save.
4. Close Yabe or any other BACnet tool if one's open - they hog the port.
5. Tick "Dry run" on the BACnet Discovery step and run it once. If the
   plan says "using foreign-device registration via BBMD ..." you're
   set. If it says "local broadcast only", the save didn't take - back
   to step 1.
6. Untick Dry run, tick the authorisation box, and run it for real.
   Start with a couple of known devices before the full sweep.

The first real scan may pop a Windows firewall prompt - accept it, and
do that first run before the session starts, not during it.

Cheers,
Raj
```

## Related docs

- `docs/pete-message-v0.1.14.md` — the previous build's message (sent).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
