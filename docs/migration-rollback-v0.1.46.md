# v0.1.46 migration and rollback

v0.1.46 adds no database migration. Its expected Alembic head remains
`f5d6e7f8a9b0`, following immutable evidence head `a7b8c9d0e1f2`.
`sync_credentials` and `sync_delivery_state` are unchanged.

Before upgrade, finish or stop active runs and back up the database, evidence,
reports, encrypted configuration, and exact artifact hashes. Deploy API,
worker, and frontend together, then confirm health, visible version, worker
heartbeat, one report download, and the evidence manifest.

The packaged version is now the common release identity. A portable or Docker
stamp may use the same version with a `v` prefix. A runtime stamp for another
release is a startup error, so replace the mismatched image or bundle rather
than trying to operate mixed application bytes.

The Nmap approval remains attached to the detected local installation. An
engineer selects an approved fixed profile and does not enter Nmap configuration
details.

To roll back, stop v0.1.46 after active work ends and restore the recorded
compatible v0.1.44 artifact or immutable image digests. No Alembic `downgrade`
is required for this rollback. Mixed-version operation is temporary recovery
work, not an accepted steady state. A rollback to v0.1.27 or earlier requires
the documented `downgrade` to `f6a7b8c9d0e1` after exporting Sync v2 receipts
and artifacts.
