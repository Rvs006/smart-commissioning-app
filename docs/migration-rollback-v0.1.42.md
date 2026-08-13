# v0.1.42 migration and rollback

v0.1.42 adds no database migration. Its expected Alembic head remains
`f5d6e7f8a9b0`, following the immutable-evidence head
`a7b8c9d0e1f2`. `sync_credentials` and `sync_delivery_state` remain unchanged.

Before upgrade, finish or stop active runs, back up the database, evidence,
reports, encrypted configuration, and record exact artifact hashes. Deploy API,
worker, and frontend together, then check health, version, worker heartbeat,
one report download, and evidence manifest.

To roll back, stop v0.1.42 after active work ends and restore the recorded
compatible v0.1.41 artifact or immutable image digests. Because this release
does not change the database head, no Alembic `downgrade` is required for the
v0.1.42 to v0.1.41 rollback. Keep the database backup, evidence, reports,
encrypted configuration, and raw artifacts. Mixed-version operation is
temporary recovery work, not an accepted steady state. A rollback to v0.1.27 or
earlier requires the documented `downgrade` operation to `f6a7b8c9d0e1` after
exporting Sync v2 receipts and artifacts.
