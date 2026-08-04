# v0.1.37 release validation record

Every unchecked row blocks publication. Field acceptance remains open until
Gate A and Gate B are complete in the v0.1.37 checklist.

## Identity and provenance

- [ ] Exact release commit and clean tracked worktree are recorded.
- [ ] Core, API, worker, frontend, run-context, and report-renderer versions
  are exactly `0.1.37`.
- [ ] The canonical register hash, import identity, approved scope, and both
  run IDs match the pre-run manifest.
- [ ] Application and independent evidence record one run identity, capture
  window, terminal status, and termination reason.
- [ ] Every report/export artifact has a recorded SHA-256 and matching frozen
  title, scope, register identity, and run identity.

## Automated application gate

- [ ] Core, backend, worker, and frontend unit suites pass.
- [ ] Frontend lint, typecheck, and build pass.
- [ ] Release contract and seeded-secret scans pass on sanitized artifacts.
- [ ] The bounded Reports API and UI regression tests pass.
- [ ] Indefinite capture, cancellation, resource backstop, and terminal-state
  tests pass.

## Field gates

- [ ] Gate A full-register scale run passes against the frozen 752-row register.
- [ ] Gate B longest cadence passes with
  `window_completed=true` and `termination_reason=window_elapsed`.
- [ ] No cap, cancellation, broker interruption, disk failure, truncation, or
  partial evidence is treated as a pass.
- [ ] Unexpected publishers remain outside registered-asset metrics.

## Publication boundary

- [ ] `scripts/release-portable.ps1 -PrepareOnly` verifies selected artifacts
  without creating or publishing a GitHub release.
- [ ] Publication approval is recorded before the publish command is run.
- [ ] `-VerifyExisting` passes after publication and the final package hash is
  added to the private acceptance manifest.
