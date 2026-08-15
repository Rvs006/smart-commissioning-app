# v0.1.48 migration and rollback

v0.1.48 retains the Alembic head `a6b7c8d9e0f1`; it adds no database migration.
It changes only the operator UI path that creates an existing sealed-preview
authorization. The retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`,
`sync_credentials`, and `sync_delivery_state` are unchanged. IP, BACnet, MQTT,
UDMI, report, evidence, and Nmap policy data are unchanged.

Before upgrade, finish or stop active runs and back up the database, evidence,
reports, encrypted configuration, and exact artifact hashes. Deploy API,
worker, and frontend from the same v0.1.48 release, then confirm health,
visible version, worker heartbeat, one report download, and the evidence
manifest.

To roll back to v0.1.47, stop v0.1.48 after active work ends and restore the
recorded v0.1.47 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. A rollback to v0.1.46 or
earlier requires the documented downgrade to `f6a7b8c9d0e1` after exporting
Sync v2 receipts and artifacts. Mixed-version operation is temporary recovery
work, not an accepted steady state. Do not run mixed v0.1.47 and v0.1.48 API or
worker processes.
