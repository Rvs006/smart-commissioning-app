# Documentation index

This index separates current guidance from release records and historical
working notes. A filename alone is a poor lifecycle signal, especially when an
old handoff still says "ready to send."

Status meanings:

- **Current**: maintained guidance for the present codebase.
- **Candidate**: a build prepared for field validation but not yet published as a
  GitHub release.
- **Versioned**: accurate for the named release and retained for rollback or
  audit work.
- **Historical**: a completed plan, sent message, or dated review record.
- **Generated**: produced by tooling; edit the source or generator instead.

## Start here

| Need | Status | Document |
| --- | --- | --- |
| Product overview | Current | [What is this?](what-is-this.md) |
| Windows field use | Current | [Field quick-start](field-quickstart.md) |
| Nmap on a field laptop | Current | [One-click Nmap operator guide](nmap-one-click-operator-guide.md) |
| Five-minute stack check | Current | [Quickstart](quickstart.md) |
| Reviewer orientation | Current | [Review guide](review-guide.md) |
| Full repository entry point | Current | [Root README](../README.md) |

## Operations and assurance

| Area | Status | Documents |
| --- | --- | --- |
| Architecture | Current | [Production architecture](production-architecture.md) |
| Daily operation | Current | [Operations runbook](runbook.md) |
| Backup | Current | [Backup and restore](backup-restore.md) |
| Monitoring | Current | [Observability](observability.md) |
| Security | Current, with its review date recorded in the file | [Security posture](security-posture.md) |
| Protocol support | Current | [Protocol conformance](protocol-conformance.md) |
| Live testing | Current | [Phase 5 surface inventory](phase5-live-surface-inventory.md) and [on-site checklist](phase5-onsite-validation.md) |
| Pilot boundary | Current | [Team pilot deployment](team-pilot-deployment.md) |
| Windows | Current, with its review date recorded in the file | [Windows compatibility](windows-compatibility.md) |

## Deployment, migration, and synchronization

| Area | Status | Documents |
| --- | --- | --- |
| Internal portable candidate | v0.1.54 candidate | [Release notes](release-notes-v0.1.54.md), [validation](release-validation-v0.1.54.md), and [rollback](migration-rollback-v0.1.54.md) |
| Hosted Docker | v0.1.54 candidate | [Docker deployment and rollback](docker-deployment-rollback-v0.1.54.md) |
| Portable rebuild | Current | [Portable bundle rebuild](portable-bundle-rebuild.md) |
| MQTT identities and ACLs | Versioned for v0.1.26 | [MQTT client IDs and broker ACLs](mqtt-client-id-and-acl.md) |
| Inline ownership | Versioned for v0.1.27 | [Inline heartbeat](inline-heartbeat-v0.1.27.md) |
| Database migration | Versioned | [v0.1.26](migration-rollback-v0.1.26.md), [v0.1.27](migration-rollback-v0.1.27.md), [v0.1.28](migration-rollback-v0.1.28.md), [v0.1.29](migration-rollback-v0.1.29.md), [v0.1.30](migration-rollback-v0.1.30.md), [v0.1.31](migration-rollback-v0.1.31.md), [v0.1.36](migration-rollback-v0.1.36.md), [v0.1.37](migration-rollback-v0.1.37.md), [v0.1.38](migration-rollback-v0.1.38.md), [v0.1.39](migration-rollback-v0.1.39.md), [v0.1.40](migration-rollback-v0.1.40.md), [v0.1.41](migration-rollback-v0.1.41.md), and [v0.1.42](migration-rollback-v0.1.42.md) |
| Sync design | Current | [Architecture](sync-architecture.md), [wire format](sync-v2-wire-format.md), [credential scope](sync-v2-credential-scope.md), [operations](sync-v2-operations.md) |

## v0.1.54 candidate

v0.1.54 hardens the two release-gate tools that run in CI: the shared
release-evidence validator (malformed evidence JSON fails closed with a clear
message instead of a traceback) and the shared release secret scanner (catches
quoted-key and pretty-printed multi-line JSON credentials, with precise
field-name and label suppression). It is a release-tooling change on top of
v0.1.53 with no functional change to discovery, reporting, evidence, or the
Advanced scanner panels, and it adds no database migration.

- [Release notes](release-notes-v0.1.54.md)
- [Release validation record](release-validation-v0.1.54.md)
- [Migration and rollback](migration-rollback-v0.1.54.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.54.md)
- [Field acceptance checklist](v0.1.54-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.54-evidence-manifest.md)
- [Nmap operator guide](nmap-one-click-operator-guide.md)

## v0.1.53 release record

v0.1.53 is the latest published release while v0.1.54 remains a candidate. It
hardened the release-gate secret scanning and the portable-bundle provenance
checks and scrubbed a placeholder from the vendored MQTT discovery page, with no
functional change to discovery, reporting, evidence, or the Advanced scanner
panels.

- [Release notes](release-notes-v0.1.53.md)
- [Release validation record](release-validation-v0.1.53.md)
- [Migration and rollback](migration-rollback-v0.1.53.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.53.md)
- [Field acceptance checklist](v0.1.53-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.53-evidence-manifest.md)

## v0.1.52 release record

v0.1.52 brought the standalone IP, BACnet, and MQTT discovery tools into SCT as an
Advanced tab on each discovery module, added the sidecar-lane operator inputs,
and stabilized the MQTT live topic tree.

- [Release notes](release-notes-v0.1.52.md)
- [Release validation record](release-validation-v0.1.52.md)
- [Migration and rollback](migration-rollback-v0.1.52.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.52.md)
- [Field acceptance checklist](v0.1.52-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.52-evidence-manifest.md)

## v0.1.51 release record

v0.1.51 brought the standalone IP, BACnet, and MQTT discovery tools into SCT as
native modules and added the frictionless authorization mode.

- [Release notes](release-notes-v0.1.51.md)
- [Release validation record](release-validation-v0.1.51.md)
- [Migration and rollback](migration-rollback-v0.1.51.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.51.md)
- [Field acceptance checklist](v0.1.51-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.51-evidence-manifest.md)

## v0.1.50 release record

v0.1.50 updated the bundled Brief and Learning pages with the protected discovery
sequence, safe equivalent retries, and protocol and Nmap boundaries.

- [Release notes](release-notes-v0.1.50.md)
- [Release validation record](release-validation-v0.1.50.md)
- [Migration and rollback](migration-rollback-v0.1.50.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.50.md)
- [Field acceptance checklist](v0.1.50-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.50-evidence-manifest.md)

## v0.1.49 release record

v0.1.49 delivered run-isolated preview/live evidence and equivalent IP retries.

- [Release notes](release-notes-v0.1.49.md)
- [Release validation record](release-validation-v0.1.49.md)
- [Migration and rollback](migration-rollback-v0.1.49.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.49.md)
- [Field acceptance checklist](v0.1.49-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.49-evidence-manifest.md)

## v0.1.48 release record

v0.1.48 added in-app creation of sealed-preview authorizations. Its versioned
records remain available for rollback and audit work.

- [Release notes](release-notes-v0.1.48.md)
- [Release validation record](release-validation-v0.1.48.md)
- [Migration and rollback](migration-rollback-v0.1.48.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.48.md)
- [Field acceptance checklist](v0.1.48-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.48-evidence-manifest.md)

## v0.1.41 release record

v0.1.41 remains available for audit and rollback comparison.

- [Release notes](release-notes-v0.1.41.md)
- [Release validation record](release-validation-v0.1.41.md)
- [Migration and rollback](migration-rollback-v0.1.41.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.41.md)
- [Field acceptance checklist](v0.1.41-field-acceptance-checklist.md)
- [Evidence manifest](v0.1.41-evidence-manifest.md)

## v0.1.40 release record

These files are retained for rollback, field comparison, and audit work.

- [Release notes](release-notes-v0.1.40.md)
- [Release validation record](release-validation-v0.1.40.md)
- [Migration and rollback](migration-rollback-v0.1.40.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.40.md)
- [Field acceptance checklist](v0.1.40-field-acceptance-checklist.md)
- [Baseline comparison](v0.1.40-baseline-comparison.md)
- [Evidence manifest](v0.1.40-evidence-manifest.md)
- [Capture contract](v0.1.40-capture-contract.md)
- [UDMI regression postmortem](v0.1.40-udmi-regression-postmortem.md)
- [Meeting-note disposition](v0.1.40-meeting-note-disposition.md)

## v0.1.39 versioned release record

v0.1.39 remains available for audit and rollback comparison.

- [Release notes](release-notes-v0.1.39.md)
- [Release validation record](release-validation-v0.1.39.md)
- [Migration and rollback](migration-rollback-v0.1.39.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.39.md)
- [Field acceptance checklist](v0.1.39-field-acceptance-checklist.md)
- [Baseline comparison](v0.1.39-baseline-comparison.md)
- [Evidence manifest](v0.1.39-evidence-manifest.md)

## v0.1.38 release record

The original v0.1.38 release remains available for rollback and comparison.

- [Release notes](release-notes-v0.1.38.md)
- [Release validation record](release-validation-v0.1.38.md)
- [Migration and rollback](migration-rollback-v0.1.38.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.38.md)

## v0.1.30 candidate record

This prior candidate remains available for review and rollback traceability.

- [Release notes](release-notes-v0.1.30.md)
- [Release validation record](release-validation-v0.1.30.md)
- [Migration and rollback](migration-rollback-v0.1.30.md)
- [Docker deployment and rollback](docker-deployment-rollback-v0.1.30.md)
- [Field acceptance checklist](v0.1.30-field-acceptance-checklist.md)

## Release records

Release notes use the same readable spine: a specific title and opening, a
`What changed` section, release-specific compatibility or boundary notes where
needed, and Windows portable provenance.

- [v0.1.21](release-notes-v0.1.21.md)
- [v0.1.22](release-notes-v0.1.22.md)
- [v0.1.23](release-notes-v0.1.23.md)
- [v0.1.24](release-notes-v0.1.24.md)
- [v0.1.25](release-notes-v0.1.25.md)
- [v0.1.26](release-notes-v0.1.26.md)
- [v0.1.27](release-notes-v0.1.27.md)
- [v0.1.28](release-notes-v0.1.28.md)
- [v0.1.29](release-notes-v0.1.29.md)
- [v0.1.30 candidate](release-notes-v0.1.30.md)

Blocking release records are retained for
[v0.1.26](release-validation-v0.1.26.md),
[v0.1.27](release-validation-v0.1.27.md),
[v0.1.28](release-validation-v0.1.28.md),
[v0.1.29](release-validation-v0.1.29.md), and the
[v0.1.30 candidate](release-validation-v0.1.30.md),
[v0.1.31 candidate](release-validation-v0.1.31.md), and the
[v0.1.37 candidate](release-validation-v0.1.37.md).

## Historical field communication

These messages document what was sent or prepared for an older build. Keep them
for traceability; current field instructions start at
[field-quickstart.md](field-quickstart.md).

- [v0.1.11 to v0.1.13 wrap-up](field-message-2026-07-16.md)
- [v0.1.13 follow-up decisions](field-followups-2026-07-16.md)
- [v0.1.14](field-message-v0.1.14.md)
- [v0.1.15](field-message-v0.1.15.md)
- [v0.1.16](field-message-v0.1.16.md)
- [v0.1.17](field-message-v0.1.17.md)
- [v0.1.18](field-message-v0.1.18.md)
- [v0.1.19](field-message-v0.1.19.md)
- [v0.1.20](field-message-v0.1.20.md)
- [v0.1.21](field-message-v0.1.21.md)
- [v0.1.22](field-message-v0.1.22.md)
- [v0.1.17 lab-day runbook](lab-day-2026-07-20-runbook.md)

## Historical plans and handoffs

- [June engineer update](engineer-update-2026-06-18.md)
- [July field walkthrough handoff](handoff-2026-07-15-field-walkthrough.md)
- [v0.1.13 remaining-punch-list handoff](handoff-v0.1.13-remaining-punchlist.md)
- [v0.1.11 to v0.1.13 release-publishing handoff](release-publishing-handoff.md)
- [Original 24-comment verification record](review-comments-verification.md)
- [V1 review checklist](v1-review-checklist.md)
- [Completed live-capture routing plan](plans/2026-07-10-001-udmi-live-capture-routing-plan.md)
- [Completed field payload-mapping plan](plans/2026-07-10-002-fix-field-udmi-payload-mapping-plan.md)
- [Completed v0.1.24 UDMI review plan](plans/2026-07-23-001-v0.1.24-udmi-review-plan.md)
- [Implemented NIC-selection proposal](proposals/nic-interface-selection.md)
- [Superseded v0.1.27 Sync v2 plan](sync-v2-v0.1.27.md)
- [Implemented v0.1.28 Sync v2 plan](sync-v2-v0.1.28.md)

## Generated and provenance documents

- [SBOM policy and inventory notes](SBOM.md)
- [Generated Python dependency inventory](SBOM.generated.md)
- [Historical detailed specification](../deliverables/Smart_Commissioning_App_Detailed_Specification.md)
- [Pinned Digital Buildings Ontology provenance](../core/smart_commissioning_core/schemas/dbo/UPSTREAM.md)
- [Vendored UDMI schema provenance](../core/smart_commissioning_core/schemas/udmi/UPSTREAM.md)

## Contributor documents

- [Contributing guide](../CONTRIBUTING.md)
- [Coding-agent guidance](../AGENTS.md) and its identical [Claude copy](../CLAUDE.md)
- [API README](../backend/README.md)
- [Worker README](../worker/README.md)
- [Hosted deployment README](../infra/README.md)
- [Changelog](../CHANGELOG.md)
