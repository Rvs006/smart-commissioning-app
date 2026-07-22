# Message to field engineer + integration engineer — v0.1.18 (ready to send)

**Written 2026-07-20, ahead of the tag.** Send AFTER the v0.1.18 Release is
published with the portable zip attached and marked latest — check the link
resolves first.

Context for the next session: v0.1.18 is the same-day turnaround of the
2026-07-20 live field review (PR #88). Codex reviewed the PR; both its
findings were verified and fixed. The exe SHA-256 lives in the Release notes,
not here.

## The message (simplified for a non-technical read)

```text
Hi both,

Same-day build with everything from this morning's session - v0.1.18:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.18
Same zip as always.

The big ones:

1. Stop button. Every tool now has "Stop run" - it keeps whatever data
   was collected and you can still generate a report from it. No more
   refreshing the page to escape a run.
2. Runs now happen in the background. Click Execute and the page comes
   back immediately with a progress bar and an elapsed timer. You can
   navigate away and come back - the running job picks up where the
   monitor left off. Closing the app kills the run (it'll show as
   "interrupted" next start - that's expected).
3. Run time: leave it blank and it now genuinely runs until every
   asset/topic has reported or you hit Stop. The old hidden ~2 minute
   cap is gone. (There's still a 48-hour safety limit so nothing runs
   forever by accident.)
4. Config export with keys - what you asked for. There's a separate
   "Export with secrets" button (the normal export stays masked). It
   includes the MQTT password and the actual cert/key/CA contents, so
   you can name it per site, hand the file to each other, import it on
   another laptop and it just connects. Plain text for now as agreed -
   encryption later.
5. Root Topic is gone from the Configuration page, as decided. The
   topic filter lives on the MQTT discovery page now; leave it blank
   to capture everything.
6. The three-file templates section is gone too - the download
   templates in Register Import cover it.
7. Results: grouped by asset now (click an asset to expand its
   metadata/state/pointset rows), filters for asset type and
   online/offline ("show me all the EMs that are offline"), and the
   payload compare sits side by side, scrolls as one, colours the
   JSON, and highlights the exact rows the checker flagged.

integration engineer - your scrollable windows and the "register already imported"
note are in there too.

field engineer - still curious what your 10-minute capture showed. If the tool
said "not published" for things Joe definitely published, grab the log
bundle after a run on this build and send it over - it captures the
full story now.

And the question that won't die: does the lab have a BBMD, or is the
whole estate flat? It decides what I build next on the BACnet side.

Cheers,
Product team
```

## Related docs

- `docs/field-message-v0.1.17.md` — the previous build's message.
- `CHANGELOG.md` `[0.1.18]` — full detail.
