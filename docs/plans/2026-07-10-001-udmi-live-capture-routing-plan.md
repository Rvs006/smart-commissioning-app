---
title: "Complete live MQTT/UDMI capture and validation"
status: completed
created: 2026-07-10
---

# Problem frame

field engineer's discovery run proves the MSI client can connect, subscribe, and receive MQTT payloads. His UDMI run still returns almost immediately with `not captured`, even when a 120-second window is entered. The original SUBACK/retained-PUBLISH ordering defect is fixed and covered by CI; this plan addresses the remaining end-to-end gap: ensuring the validation request carries the intended register topics, the live capture subscribes to exactly those topics, received payloads are routed to the correct asset, and the result exposes enough evidence to diagnose any real broker mismatch.

## Scope and success criteria

- A register-driven three-asset run sends the requested positive timeout to the backend and waits until all expected asset payload groups arrive or the timeout expires.
- A broker payload on `state`, `metadata`, `events/pointset`, or legacy `event/pointset` is associated with the correct asset and payload type.
- The run never reports `not captured` when a matching valid JSON payload was received.
- When no match exists, the UI shows subscribed filters, received topics, per-message timestamps, and the reason for rejection.
- Existing discovery, pasted-payload validation, authorization, and security behavior remains unchanged.

## Decisions

1. Treat the register as the source of truth for expected asset roots and payload types; do not infer an asset from payload content.
2. Keep capture completion event-driven, but enforce the user-selected timeout as an upper bound.
3. Preserve raw MQTT evidence (topic, retained flag, receipt time, JSON parse result) through persistence and the result API.
4. Keep operational run success separate from validation pass/fail: a completed capture may contain validation issues.

## Implementation units

### U1 — Trace the live request and capture contract

Files: `frontend/src/features/workflow/ModulePage.tsx`, `backend/app/api/routes/validation.py`, `core/smart_commissioning_core/udmi_validation.py`.

Confirm the exact JSON sent for a register-driven run, including `use_register`, `capture_seconds`, broker settings, and each generated asset entry. Add a temporary structured diagnostic field (safe values only) to the run result: normalized timeout, subscribed filters, expected asset roots, and captured topic list.

Tests: frontend request test asserts `capture_seconds: 120`; backend route test asserts three imported rows produce the expected state/metadata/pointset filters; core test asserts the capture callback receives the same filters and timeout.

### U2 — Make topic normalization and asset routing deterministic

Files: `backend/app/api/routes/validation.py`, `core/smart_commissioning_core/udmi_validation.py`.

Centralize normalization of trailing slashes, wildcard roots, singular/plural event aliases, and payload-type restrictions. Build one immutable per-asset capture specification and route each received message by exact filter match plus the owning specification. Select the newest receipt per payload slot without allowing one asset's wildcard to claim another asset's message.

Tests: three assets with overlapping prefixes; explicit state-only rows; blank payload type whole-asset rows; `event/pointset` and `events/pointset` aliases; newer/older duplicate messages; malformed and scalar payloads.

### U3 — Verify timeout and completion semantics

Files: `core/smart_commissioning_core/udmi_validation.py`, `core/smart_commissioning_core/mqtt_transport.py`.

Ensure positive timeout values are passed unchanged, blank means cancel/message-cap bounded capture in worker execution, and completion requires one valid JSON object for every expected asset/payload group. Do not end a run merely because a SUBACK or retained message arrived. Keep QoS1/QoS2 handshake completion and retained-message metadata intact.

Tests: delayed payload at 1, 30, and 119 seconds; no payload until timeout; retained payload followed by fresh payload; early PUBLISH before SUBACK; QoS1 and QoS2; cancellation; message-cap exhaustion.

### U4 — Improve evidence and failure diagnostics

Files: `core/smart_commissioning_core/udmi_validation.py`, `backend/app/api/routes/validation.py`, `frontend/src/features/workflow/ModulePage.tsx`.

Expose normalized subscriptions, captured topics, per-asset receipt times, retained status, and a bounded mismatch summary in the result detail. Distinguish `broker connected/no matching topic`, `malformed payload`, `timed out`, and `asset routing mismatch`. Never include passwords, URLs with credentials, or raw broker exceptions.

Tests: each diagnostic status produces the expected safe result fields; credential-bearing exceptions are redacted; UI renders the mismatch without crashing and preserves the expected-vs-observed payload view.

### U5 — MSI deployment and field reproduction

Files: `.github/workflows/windows-portable.yml`, `packaging/windows_portable/build.ps1`, `docs/phase5-onsite-validation.md`.

Add a portable smoke assertion for the live-validation request echo and diagnostic fields. Produce a runbook for field engineer containing the exact CSV topic convention, timeout entry, broker log markers, and the minimum artifacts to collect: imported row, normalized filters, app result JSON, and broker log interval.

Tests: Windows portable build/smoke; local fake-broker three-asset scenario; manual MSI validation against field engineer's broker with Wireshark timestamps correlated to app receipt timestamps.

## Sequencing

U1 → U2 → U3 → U4 → U5. U1 must first establish whether the deployed request differs from local tests; U2/U3 fix deterministic behavior; U4 makes any remaining site-specific mismatch actionable; U5 validates the artifact and real broker.

## Risks and mitigations

- TLS prevents Wireshark payload inspection: correlate broker logs, app diagnostics, and packet timing rather than requiring decrypted captures.
- Retained values may be old: retain receipt time/flag and validate pointset freshness separately.
- Similar asset roots may overlap: reject ambiguous blank-payload rows at import and use exact normalized roots.
- A live site may publish only one payload type: report the missing group explicitly instead of treating the whole run as an unexplained failure.

## Deferred

Physical Niagara/device publisher defects, broker ACL changes, and payload-schema corrections remain site-side follow-up work once the application proves it received and routed the messages correctly.
