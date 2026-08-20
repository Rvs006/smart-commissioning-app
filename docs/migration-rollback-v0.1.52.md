# v0.1.52 migration and rollback

v0.1.52 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It is an additive scanner-sidecar integration and a release-identity
change only. The retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`,
`sync_credentials`, and `sync_delivery_state` are unchanged. IP, BACnet, MQTT,
UDMI, report, evidence, authorization, and Nmap policy data are unchanged.

The three standalone Node scanner apps are integrated as SCT-supervised
loopback sidecars (IP :3777, BACnet :3778, MQTT :3799) driven by Python adapter
engines through `run_engine`. The sidecars are gated behind
`SMART_COMMISSIONING_ENABLE_SIDECARS` and add no schema. MQTT scanning stays
read-only, with no publish or configuration writes. The portable bundle ships
the three `dist/bundle.js` scanner artifacts and one signed `node.exe`.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.52 release, then confirm health,
the visible version, Brief and Learning content, worker heartbeat, one report
download, and the evidence manifest. When sidecars are enabled, also confirm
that each scanner process binds only its loopback port and that a scanner run
records evidence through `run_engine`.

To roll back to v0.1.50, stop v0.1.52 after active work ends and redeploy the
prior portable EXE or immutable image digests. No Alembic downgrade is required
because both releases use `a6b7c8d9e0f1`, and no sidecar state persists to the
database. Rolling back removes the integrated scanner modules and restores the
prior operator-facing IP/BACnet/MQTT tabs. Mixed-version operation is temporary
recovery work, not an accepted steady state. Do not run mixed v0.1.50 and
v0.1.52 API or worker processes.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
