---
title: "Fix field engineer UDMI payload mapping and result presentation"
status: active
created: 2026-07-10
---

# Problem frame

field engineer's live broker test now captures all payload groups. The result screen still
reports registered points as absent even though the captured metadata visibly
contains them under `pointset.points`, and it compares a flattened internal
expectation (`manufacturer`, `udmi_version`) with full UDMI payload JSON. This
makes a correct capture look like a register mismatch.

The captured state payload also contains values that fail the vendored canonical
UDMI 1.5.2 schema (non-RFC-3339 timestamp and object values where the schema
expects scalar values). Those findings must remain honest; this change does not
silently relax canonical validation.

## Requirements

- Map `Make`, `Model`, and `Expected schema version` to the observed UDMI paths
  `system.hardware.make`, `system.hardware.model`, and top-level `version`.
- Compare expected point units against the captured metadata point definitions
  at `pointset.points.<point>.units` and avoid false missing-point findings.
- Render expected-versus-observed output using UDMI-shaped fields/paths rather
  than internal matcher keys.
- Report an expected identity or unit as missing when it is absent, not only
  when a non-empty observed value differs.
- Preserve strict canonical UDMI 1.5.2 structural validation and its existing
  diagnostics for non-canonical publisher fields.
- Add regression tests using field engineer-shaped payload fixtures.
- Add a local JSONCrack-style expandable payload tree for an explicitly selected
  captured payload; never send broker payloads, credentials, or configuration
  to a third party.

## Scope boundaries

- No changes to MQTT connection, subscription, capture timing, or wildcard
  routing: the screenshots prove those paths now work.
- No change to the imported-register `Payload type` contract: it remains blank,
  `state`, `metadata`, or `pointset`.
- No automatic acceptance or rewriting of non-canonical state payload fields.

## Technical decisions

1. Keep the register as the source of expected values, but express result views
   as path/value comparisons so operators can see the exact payload location.
2. Keep canonical schema validation independent from register matching. A field
   can match the register yet still be reported as a UDMI schema violation.
3. Use the captured payload shape in regression fixtures; screenshots alone are
   not a substitute for repeatable test evidence.
4. Build the small useful subset locally: an expandable JSON tree beside the
   selected payload. This avoids a new dependency and keeps live broker data on
   the MSI server; it is intentionally not a full graph-editor clone.

## Implementation units

### U1 — Characterise field engineer-shaped payload matching

Files: `core/tests/test_udmi_validation.py`, `backend/tests/test_v1_review_contracts.py`.

Add fixtures for a metadata payload containing `pointset.points.<name>.units`
and a state payload containing top-level `version` and `system.hardware` make/
model. Cover present, missing, and mismatched fields.

Verification: registered points shown in field engineer's screenshots match; a genuinely
missing point/unit and missing make/model/version each emit a precise issue.

### U2 — Correct shared validation lookup and missing-field handling

Files: `core/smart_commissioning_core/udmi_validation.py`,
`core/tests/test_udmi_validation.py`.

Trace the captured metadata object through the shared validator and correct the
point-definition lookup so it reads the actual payload shape. Extend identity
checks to distinguish missing expected values from mismatches, without changing
the canonical-schema validator.

Verification: valid metadata point units no longer produce missing-point
issues; mismatched and absent values remain failures; existing schema tests stay
green.

### U3 — Present register expectations as UDMI paths

Files: `core/smart_commissioning_core/udmi_validation.py`,
`frontend/src/features/workflow/ModulePage.tsx`,
`frontend/src/features/workflow/ModulePage.test.tsx`.

Replace the flattened internal expected JSON in the payload view with a compact
path/value comparison representation that names `version`,
`system.hardware.make`, `system.hardware.model`, and point-unit paths.

Verification: UI tests assert that operator-visible expected labels use UDMI
paths and never expose `manufacturer` or `udmi_version` as payload fields.

### U4 — Add a local JSONCrack-style payload inspector

Files: `frontend/src/features/workflow/ModulePage.tsx`,
`frontend/src/features/workflow/ModulePage.test.tsx`.

Place an expandable tree control beside captured MQTT/UDMI payload evidence. It
is available only when that payload is valid JSON and renders keys, arrays, and
primitive values in an engineer-readable tree without external network access.

Verification: valid nested payload expands and collapses correctly; absent or
malformed payload has no actionable control; configuration secrets are not
included.

### U5 — Release and field verification

Files: `docs/field-quickstart.md`, `CHANGELOG.md`.

Record the fixed result semantics and provide field engineer a short repeat test: import
the corrected register, run 120 seconds, confirm matched point/unit evidence,
and retain canonical-state warnings if the publisher still emits non-canonical
fields.

Verification: focused core/backend/frontend tests, complete repository gates,
Windows portable build/boot smoke, and a fresh release artifact.

## Sequence

U1 → U2 → U3/U4 → U5. U1 first locks the observed payload contract; U2 removes
the false negative; U3 makes the result explainable; U4 adds the requested
inspection path; U5 packages only a fully verified build.

## Risks and mitigations

- A screenshot may omit details of the stored payload: use a test fixture based
  on the exact nested shapes and retain raw evidence in failures.
- A graph-editor clone would add substantial surface area: ship the local tree
  inspector first and add graph layout only if field engineers still need it.
- Relaxing schema checks would hide publisher defects: do not normalize or
  suppress canonical-schema findings in this fix.
