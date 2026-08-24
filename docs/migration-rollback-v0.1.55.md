# v0.1.55 migration and rollback

v0.1.55 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It changes bundled frontend guidance and release identity only. The
retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`, `sync_credentials`,
and `sync_delivery_state` are unchanged. IP, BACnet, MQTT, UDMI, report,
evidence, authorization, and Nmap policy data are unchanged.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.55 release, then confirm health,
the visible version, Brief and Learning content, worker heartbeat, one report
download, and the evidence manifest.

To roll back to v0.1.54, stop v0.1.55 after active work ends and restore the
recorded v0.1.54 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. Mixed-version operation is
temporary recovery work, not an accepted steady state. Do not run mixed
v0.1.54 and v0.1.55 API or worker processes.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
