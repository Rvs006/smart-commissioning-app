# v0.1.41 release validation

Date: 2026-08-13
Status: application release gate passed; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
|---|---|---|
| Version and contract identity | Package, runtime, frontend lock, and release contract checks | Local automated gate |
| Discovery authority and evidence | Core, backend, worker, frontend, migration, and live PostgreSQL gates | Local and disposable-container gates |
| Finite MQTT capture | Local real-broker synthetic 780-row blank-register case | Local synthetic gate |
| Long field window | Approved register, independent collector, and longest cadence | **UNPROVEN** |
| Field acceptance | Private operator, broker, machine, and evidence references | **UNPROVEN** |

The only completed finite-window outcome is
`window_completed=true` with `termination_reason=window_elapsed`. Cancellation,
broker interruption, primary-cap exit, and missing outcome proof are incomplete.
Diagnostic truncation is retained as incomplete diagnostic evidence while the
finite capture continues.

Before acceptance, record the approved register hash/import, exact run and
evidence-set IDs, capture start/end and requested duration, client/technical
artifact hashes, portable/image hashes, independent collector evidence, and
owner sign-off. The short broker gate does not prove a 2-hour or 10-hour field
window.
