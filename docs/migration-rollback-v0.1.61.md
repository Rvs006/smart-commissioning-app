# v0.1.61 migration and rollback

v0.1.61 retains the Alembic head `a6b7c8d9e0f1` and adds no database
migration. It changes how the Reports list reads stored reports (display
fields only, through `page_report_summaries`; full verification stays on open,
download, export and verify), relabels a completed capture whose non-register
topic store filled, and changes release identity. Stored report rows, their
seals and evidence contracts are read as before and are not rewritten. The
retained Sync v2 immutable-evidence head `a7b8c9d0e1f2`, `sync_credentials`,
and `sync_delivery_state` are unchanged. IP, BACnet, MQTT, UDMI, report,
evidence, authorization, and Nmap policy data are unchanged.

Before upgrading, finish or stop active runs and back up the database,
evidence, reports, encrypted configuration, and exact artifact hashes. Deploy
API, worker, and frontend from the same v0.1.61 release, then confirm health,
the visible version, one native scanner run, worker heartbeat,
one report download, and the evidence manifest.

To roll back to v0.1.60, stop v0.1.61 after active work ends and restore the
recorded v0.1.60 EXE or immutable image digests. No Alembic downgrade is
required because both releases use `a6b7c8d9e0f1`. Mixed-version operation is
temporary recovery work, not an accepted steady state. Do not run mixed
v0.1.60 and v0.1.61 API or worker processes.

A rollback to v0.1.27 or earlier requires the documented downgrade to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
