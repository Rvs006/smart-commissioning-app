# v0.1.45 migration and rollback

v0.1.45 adds no database migration. Its expected Alembic head remains
`f5d6e7f8a9b0`, following the immutable-evidence head `a7b8c9d0e1f2`.
`sync_credentials` and `sync_delivery_state` remain unchanged.

Before upgrade, finish or stop active runs and back up the database, evidence,
reports, encrypted configuration, and exact artifact hashes. Deploy API,
worker, and frontend together, then confirm health, visible version, worker
heartbeat, one report download, and the evidence manifest.

For a source build without a frontend version stamp, v0.1.45 now reports the
package version instead of `dev`. The portable release continues to stamp the
frontend, backend, README, and EXE from one build version. Nmap approval stays
recorded for the detected local installation; engineers use the approved fixed
profile and do not enter Nmap configuration details.

To roll back, stop v0.1.45 after active work ends and restore the recorded
compatible v0.1.44 artifact or immutable image digests. Because this release
does not change the database head, no Alembic `downgrade` is required for the
v0.1.45 to v0.1.44 rollback. Mixed-version operation is temporary recovery
work, not an accepted steady state. A rollback to v0.1.27 or earlier requires
the documented `downgrade` operation to `f6a7b8c9d0e1` after exporting Sync v2
receipts and artifacts.
