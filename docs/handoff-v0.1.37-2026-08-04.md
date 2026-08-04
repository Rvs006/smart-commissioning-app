# v0.1.37 implementation and field handoff

**Prepared:** 2026-08-04

v0.1.37 carries the application-side fixes from the field acceptance plan. The
application gate is testable in this repository. The field gate remains open
until the private 752-asset full-register run and the longest cadence run pass
against the frozen manifest in `docs/v0.1.37-field-acceptance-checklist.md`.

## Baseline carried forward

The v0.1.36 report job succeeded as an export operation, not as field
acceptance. Its diagnostic baseline was 752 expected assets, 470 observed,
282 unobserved, 17 registered wrong-topic candidates, 27 unexpected publishers,
0 successfully validated assets, and 2,051 blocking findings. These counts are
historical context only. A changed count cannot prove a fix.

## Application changes

- State-shaped payloads found on metadata topics now produce one grouped
  `payload_routing` diagnostic with expected topic, raw topic provenance, and
  key evidence.
- Topic association remains exact and case-sensitive, with expected,
  alternate, and no-match outcomes kept separate.
- Missing evidence is not converted into zero, false, or an observation.
- Report listing has an explicit bounded page and the Reports table uses a
  bounded scroll region.
- Release evidence and field artifacts are hash- and commit-bound through the
  v0.1.37 manifest and release validators.

## Ownership boundary

Publisher topic roots, timestamps, metadata content, point names and units,
cadence, device mappings, and broker behavior are publisher/field work. The
application reports those findings and does not loosen its register or schema
contract to hide them.

Do not commit broker commands, credentials, real site or device identifiers,
personnel details, or commercial detail. Publication is a separate action that
requires explicit user authorization after both field gates pass.
