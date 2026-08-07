# v0.1.40 migration and rollback

v0.1.40 has no database migration. The Alembic head remains
`a7b8c9d0e1f2`; `sync_credentials` and `sync_delivery_state` are unchanged.

Before upgrade, finish or stop active runs, back up the database, evidence,
reports, encrypted configuration, and record exact artifact hashes. Deploy API,
worker, and frontend together, then check health, version, worker heartbeat,
one report download, and evidence manifest.

To roll back, stop v0.1.40 after active work ends and restore the recorded
compatible v0.1.39 artifact or immutable image digests. Leave the database at
`a7b8c9d0e1f2` and do not delete artifacts or secret volumes. Mixed-version
operation is temporary recovery work, not an accepted steady state. A rollback
to v0.1.27 or earlier requires the documented `downgrade` operation to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts.
