# v0.1.43 migration and rollback

v0.1.43 adds no database migration. Its expected Alembic head remains
`f5d6e7f8a9b0`, following the immutable-evidence head `a7b8c9d0e1f2`.
`sync_credentials` and `sync_delivery_state` remain unchanged.

Before upgrade, finish or stop active runs and back up the database, evidence,
reports, encrypted configuration, and the exact artifact hashes. Deploy API,
worker, and frontend together, then confirm health, visible version, worker
heartbeat, one report download, and the evidence manifest.

The v0.1.43 unified portable enables the Nmap approval capability. The existing
stable local app data continues to hold a valid Nmap approval. If the local Nmap
installation changes, a global administrator must approve it again after
upgrade.

To roll back, stop v0.1.43 after active work ends and restore the recorded
compatible v0.1.42 artifact or immutable image digests. Because this release
does not change the database head, no Alembic `downgrade` is required for the
v0.1.43 to v0.1.42 rollback. Mixed-version operation is temporary recovery
work, not an accepted steady state. A rollback to v0.1.27 or earlier requires
the documented `downgrade` operation to `f6a7b8c9d0e1` after exporting Sync v2
receipts and artifacts.
