# Message to Pete — v0.1.14 (ready to send)

**Written 2026-07-16, after the v0.1.11–13 wrap-up message went out.** The
v0.1.14 Release is published with the portable zip attached and marked latest,
so this is true as written — paste it into Teams or email as-is.

Context for the next session: v0.1.14 is a same-day follow-up to v0.1.13. An
audit sweep found three placeholder leftovers that survived the v0.1.13 purge
(import-template example site name, pre-run sample issues on validation pages,
dead sample rows in the bundle) plus one flaky Windows CI test. No engine or
report behaviour changed. Full detail in `CHANGELOG.md` `[0.1.14]`.

## The message

```text
Hi Pete,

Quick one on top of this morning's builds - there's a v0.1.14 up now, and
it's the one to use:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.14
(Smart_Commissioning_App_Windows_Portable.zip, about 32MB)

After v0.1.13 went out I ran a sweep for leftover demo content and found
three stragglers, so this build clears them:

- The import templates you download had "ElectraCom / Block B Plantroom" in
  the example row - now "Example Site / Plant Room", so a template can't be
  mistaken for a real site register
- The validation pages showed four made-up sample issues (ISS-1042 and
  friends) before you'd run anything - now they just say run a validation to
  see findings
- Some never-displayed sample rows are deleted from the app bundle outright

Nothing about scanning, validation or reports changes. If you've already got
v0.1.13, swap the folder for this one - your saved config and results stay
put, they live outside the exe folder now. Any reports you generate for the
ITP pack, do them on this build so the report and the templates in the
evidence line up.

If your machines allowlist by hash, the new exe needs approving again, same
drill as before:
SmartCommissioningApp.exe SHA-256:
92176E5EDC3991B855ACA113C6BA021F4FDA3875C9FFBF6A8461F0BCE3FCD2DA

Monday is unchanged - same runbook, same prep.

Cheers,
Raj
```

## Related docs

- `docs/pete-message-2026-07-16.md` — the v0.1.11–13 wrap-up message (sent).
- `docs/pete-followups-2026-07-16.md` — his open questions (unchanged by this).
- `docs/lab-day-2026-07-20-runbook.md` — Monday's BACnet lab session.
