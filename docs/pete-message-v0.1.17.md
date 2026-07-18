# Message to Pete — v0.1.17 (ready to send)

**Written 2026-07-17, ahead of the tag.** Send AFTER the v0.1.17 Release is
published with the portable zip attached and marked latest — check the link
resolves first. Assumes the v0.1.16 message has already been sent (this one
does not repeat the BACnet fixes or the stuck-runs cleanup note).

Context for the next session: v0.1.17 is the walkthrough punch list from
Pete's Document3 (7 implemented; 4 parked for discussion by his own request).
Root causes worth remembering: the password was never lost (write-only API,
lying Show affordance) and Root Topic was overridden by the run form's own
silent `#`. The Monday runbook is rewritten two-path (flat no-BBMD = primary).
Full detail in `CHANGELOG.md` `[0.1.17]`.

## The message (simplified for a non-technical read)

```text
Hi Pete,

Second build today - v0.1.17. This one is your walkthrough list:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.17
Same zip as always. Install over the top of v0.1.16 or fresh, either
is fine.

From your doc:

1. Root Topic now really filters MQTT discovery. The run screen was
   quietly sending "#" behind your back, which overrode the saved
   topic. Leave the run's topic filter blank now and it uses your
   saved Root Topic; type # only if you truly want everything.
2. The password mystery solved: your password WAS saved all along.
   The app never displays saved passwords (on purpose), but the Show
   button pretended it could and just showed the masking dots. The
   field now says "Saved - hidden" so it's honest. To change it,
   type a new one and Save.
3. Results tables scroll in their own box now, with filters (type
   part of a topic or asset name) and a "Showing N of M rows" count.
4. The import card tells you when a register is already on file -
   file name, rows accepted, and when - so no more guessing whether
   to upload again.
5. Expected and observed payloads sit side by side now, and anything
   present on only one side is highlighted.
6. Your phase2/phas2 one: a misnamed point now shows as ONE fault
   naming both spellings, instead of two separate faults.
7. Empty values now say "empty", and a payload that never arrived no
   longer reads "Pass with notes".

Your other four (exporting configs with the keys in, merging the
three templates, the run-time wording, and fetching newer UDMI
versions) are parked for a proper chat - nothing changed there on
purpose.

I've also rewritten Monday's runbook so the no-BBMD path is the main
path. Which brings me back to the question: does Monday's network
have a BBMD in it, or is it flat like yours?

Cheers,
Raj
```

## Related docs

- `docs/pete-message-v0.1.16.md` — the earlier build's message (BACnet fixes).
- `docs/lab-day-2026-07-20-runbook.md` — rewritten two-path runbook.
- `docs/pete-mqtt-udmi-punchlist` context lives in session memory, not repo
  docs (private walkthrough material).
