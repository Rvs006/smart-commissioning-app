# v0.1.62 migration and rollback

v0.1.62 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It changes how the frontend recovers from a lazy page chunk that
fails to load (one reload, then a reload panel), serves `index.html` with
`Cache-Control: no-cache` and pins the `.js` and `.css` media types, makes the
portable launcher warn when port 8000 is already taken, and changes release
identity. No stored row, seal, evidence contract or report is touched. The
retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`, `sync_credentials`,
and `sync_delivery_state` are unchanged. IP, BACnet, MQTT, UDMI, report,
evidence, authorization, and Nmap policy data are unchanged.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.62 release, then confirm health,
the visible version, one native scanner run, worker heartbeat,
one report download, and the evidence manifest.

To roll back to v0.1.61, stop v0.1.62 after active work ends and restore the
recorded v0.1.61 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. Mixed-version operation is
temporary recovery work, not an accepted steady state. Do not run mixed
v0.1.61 and v0.1.62 API or worker processes.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
