# v0.1.39 release validation

Date: 2026-08-07
Release status: Candidate with named blockers
Scope: Asset-register reconciliation, UDMI validation, portable evidence export

## Release decision

The candidate is not accepted for controlled field validation. The local
automated evidence supports the application changes, but the authoritative
register, payload applicability matrix, required cadence, paired collector
evidence, and manual packaged-run checks are still open.

Every unchecked row blocks publication of a field-acceptance claim.

## Gate A: evidence identity and completeness

| Requirement | Candidate state | Evidence / blocker |
|---|---|---|
| Approved register and asset count | Open | 752 and 780 are both present in the review record; no owner decision |
| Matching register hash and import ID | Open | Not supplied for the next candidate run |
| Matching application and independent collector evidence | Open | No paired v0.1.39 run available |
| Shared run and evidence-set identity | Implemented and automated-tested | Report formats use one deterministic evidence-set identity |
| Exact topic association | Implemented and automated-tested | Expected, wrong-topic, alternate, and no-match paths are covered |
| Unexpected publishers excluded from registered metrics | Implemented and automated-tested | Report model keeps unexpected assets separate |
| Complete report manifest | Candidate implementation | Technical ZIP includes raw-evidence manifest and finding index; selected-report bundles include a member hash manifest; field artifact hash manifest pending |
| Raw evidence retrieval | Candidate implementation and automated-tested | Clean-machine manual retrieval pending |
| Artifact hashes | Candidate implementation | Final field artifact hashes pending |

Gate A: **Open**

## Gate B: completed capture window

The only passing terminal state is:

```text
window_completed=true
termination_reason=window_elapsed
```

Cancellation, backstop expiry, message cap, broker interruption, disk failure,
and truncation are non-passing terminal states even when partial evidence is
retained. The v0.1.37 one-minute Stop scenario is a cancellation regression
case, not cadence evidence.

Gate B: **Open**

## Automated checks completed in the candidate

- Core UDMI validation fixtures, including applicability, timestamps, cadence,
  retained messages, topic routing, cancellation, and raw evidence.
- Backend UDMI report and export tests, including client versus technical
  products, shared evidence-set identity, cancellation labelling, and portable
  redacted raw evidence.
- Export schema parsing and validation tests.
- Core suite: `633 passed, 4 skipped` using `python -m unittest discover -s core/tests -p "test*.py"`.
- Backend suite: `612 passed, 3 skipped` using `python -m unittest discover -s backend/tests -p "test*.py"`.
- Frontend suite: `420 passed across 22 files` using `npm test -- --run`.
- Frontend typecheck, lint, and production build: passed.
- Portable launcher PowerShell parse and v0.1.39 identity contract: passed.

These are local automated checks only. The independent MQTT capture, clean-machine
retrieval, packaged-run screenshots, and release-owner sign-off remain open. No
local field run is claimed by this file.

## Required follow-up before acceptance

1. Approve the canonical register, hash, revision, import ID, and expected
   asset count.
2. Approve payload applicability and any fourth payload type.
3. Approve the longest required cadence, including the treatment of retained
   and daily payloads.
4. Run the portable candidate with an independent collector over the approved
   window.
5. Verify Gates A and B, then attach client and technical artifacts plus their
   hashes.
6. Complete the manual validation record and acceptance sign-off.

## Deferred scope

IP Discovery, BACnet Discovery, MQTT Discovery, BACnet-to-MQTT, EDQ / Device
Qualification, and whole-application production readiness are outside this
UDMI release decision.
