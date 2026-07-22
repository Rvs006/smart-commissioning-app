# Follow-up questions for field engineer — v0.1.13 build

These are decisions made while building the rest of your punch list. Each shipped
with a sensible default so nothing was blocked, but every one is a place your
answer would change or confirm what you see. Grouped by how much they matter.
None of them hold up the build — they are for the next walkthrough.

## Confirm, because I guessed a default you might not want

1. **UDMI RAG — "pass with notes" now shows amber.** You said amber = "online but
   not UDMI compliant". Read strictly, a device that passes with only *minor*
   notes drops from green to amber. I shipped it strict (your words), but if you'd
   rather minor-notes stay green, it's a one-line change. Also confirm the other
   half: a device that IS publishing but has *critical* issues also shows amber now
   — red is reserved purely for offline/not-publishing.

2. **MQTT "QoS" column reads 0 for everything.** You asked to see the QoS level.
   The column is there and honest, but discovery subscribes at QoS 0, so every
   delivery shows QoS 0 regardless of what the publisher used. To make it
   meaningful I'd have to raise the subscription to QoS 2 — which adds a handshake
   per message against your live broker. I did **not** do that without your call.
   Want it?

3. **"When it was published" can't be shown.** An MQTT 3.1.1 message carries no
   publish timestamp on the wire. The inspector shows "Received at (this tool's
   clock)" instead — and for a retained message that's the moment you subscribed,
   not the original publish. Flagging so it doesn't look like a missing-feature bug.

4. **MQTT wildcard register rows.** One register row like `site/b1/#` turns every
   topic under it green, including a rogue device publishing inside that namespace.
   Shipped as green with the covering wildcard shown per row. If you'd rather those
   read amber ("covered by wildcard, not individually listed"), that's a small add.
   Also: broker-internal `$SYS/#` topics currently flag red as "not in register" —
   keep, or exclude them?

## Report sign-offs (cheap to change now, byte-breaking later)

5. **Paper size** is A4 on the DOCX to match the PDF — confirm A4 not Letter.

6. **Is the "ELECTRACOM" text wordmark enough for ITP witnessing**, or does the
   pack need fixed footer fields — document/revision number, "uncontrolled when
   printed"? The run id is in the footer on every page; project/site are in the
   report body. Logo *image* (vs text) is a later phase.

7. **BACnet points inventory in reports.** A real lab run could produce thousands
   of point rows, and they render in the PDF/DOCX too — a very long handover doc.
   No cap (a cap hides data). If that's unwieldy, the fallback is points-to-XLSX
   only with a pointer paragraph in the PDF. What fleet sizes should I expect?

## Wire contracts I implemented as a documented guess

8. **Log upload.** I send a multipart POST (field `file`) with an optional
   `Authorization: Bearer <token>`. Tell me what actually receives it on your web
   space (POST vs PUT, Bearer vs basic vs none) and I'll match it — it's a
   one-function change.

## Things that are true and you should just know

9. **Your saved config won't pick up new defaults automatically.** Fabricated
   *statuses* now self-heal on load, but any new default *values* a machine should
   run with still need setting by hand on installs that already saved a snapshot.
   (Same caveat as the BACnet BBMD fields.)

10. **Historical runs stay as they were.** Old FAILED UDMI runs stay failed in
    history; only new runs get the succeed-with-silent-devices behaviour. Re-run
    rather than re-reading the old record.

11. **After upgrading, re-download any report** generated under the old version —
    it will read as hash-mismatched until you do (the bytes changed for branding).
    Old exported files keep valid signatures over their own bytes.

## Deferred to the next release (not dropped)

- **General nmap-style discovery pane.** Deferred because it needs *your* input:
  which ports belong in the curated scan list is the substance of the feature, and
  I won't guess it. Also what host-count ceiling is safe on your OT network. Send
  me a port list and it's a ~2.5d build.
- **Look-and-feel component extraction.** The part you actually feel — the
  scrolling-in-bound-windows fix — shipped. The remaining internal refactor moves
  no pixels and was deferred to avoid destabilising code under the dated BACnet fix.
