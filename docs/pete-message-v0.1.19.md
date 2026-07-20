# Message to Pete + Dylan — v0.1.19 (ready to send)

**Written 2026-07-20, ahead of the tag.** Send AFTER the v0.1.19 Release is
published with the portable zip attached and marked latest — check the link
resolves first. Assumes the v0.1.18 message went out earlier today.

## The message

```text
Hi both,

Last two items from the V2 doc - v0.1.19:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.19
Same zip.

1. Empty values now get flagged properly. A point whose units (in
   metadata) or present value (in pointset) arrives blank - empty
   string, null, whitespace - raises its own issue naming the point,
   instead of slipping through silently or showing up as a confusing
   "should be numeric". Kept to units + present value as we agreed,
   so a legitimately-empty field elsewhere doesn't spam you. A real 0
   or false never counts as empty. The flagged point's row goes red
   in the side-by-side compare automatically.

2. Export selected fixed. Tick several reports and you get ONE zip
   with all of them (it used to quietly export just the last one you
   ticked). One ticked report still downloads directly.

On your typo test (fast2 vs phase2): the tool merges near-identical
names into one "probable misname" fault, but those two are just
different enough to fall below the similarity cutoff, which is why
you saw two faults. We can't push the cutoff higher without it
wrongly merging real siblings like phase1/phase2 - so it stays as
agreed, and smarter matching goes on the later list.

That's every implementable item from both rounds of today's doc done.
Still parked by choice: newer UDMI versions (pinned 1.5.2), export
encryption. Still waiting on: Pete's 10-minute capture verdict, the
BBMD question, and Dylan's check on legacy pre-1.5.2 projects.

Cheers,
Raj
```

## Related docs

- `docs/pete-message-v0.1.18.md` — earlier today's build.
- `CHANGELOG.md` `[0.1.19]` — full detail.
