# v0.1.42 release validation

Date: 2026-08-13
Status: release automation required; field acceptance open

Every unchecked row blocks publication as field-accepted.

| Gate | Release evidence | State |
|---|---|---|
| Version and contract identity | Package, runtime, frontend lock, and release contract checks | Local automated gate |
| Protocol regressions | IP-register fallback, BACnet Preview/port/live-data, MQTT terminal Results, and version identity tests | Automated release gate required |
| Portable executable | Windows portable build, ProductVersion, health, report, and smoke evidence | Automated release gate required |
| Immutable container images | Hosted release gates, SBOMs, image digests, and evidence pack | Automated release gate required |
| Field validation | Approved IP register, BACnet segment, broker, and independent collector evidence | **UNPROVEN** |

For a field MQTT capture, the completed finite-window outcome is
`window_completed=true` with `termination_reason=window_elapsed`. Cancellation,
broker interruption, primary-cap exit, and missing outcome proof are incomplete.

Before acceptance, record the approved register hash/import, BACnet port and
interface, exact run and evidence-set IDs, capture start/end and requested
duration, client/technical artifact hashes, portable/image hashes, independent
collector evidence, and owner sign-off. A synthetic broker gate does not prove a
live field window.
