# v0.1.60 migration and rollback

v0.1.60 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It moves pure reads onto the query-only session, adds the BACnet
object-list indexed read, measures the UDMI unexpected-device count past the
secondary payload cap, and changes release identity. A saved configuration that
still carries the removed BBMD toggle key loads unchanged. The
retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`, `sync_credentials`,
and `sync_delivery_state` are unchanged. IP, BACnet, MQTT, UDMI, report,
evidence, authorization, and Nmap policy data are unchanged.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.60 release, then confirm health,
the visible version, one native scanner run, worker heartbeat,
one report download, and the evidence manifest.

To roll back to v0.1.59, stop v0.1.60 after active work ends and restore the
recorded v0.1.59 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. Mixed-version operation is
temporary recovery work, not an accepted steady state. Do not run mixed
v0.1.59 and v0.1.60 API or worker processes.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
