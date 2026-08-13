# v0.1.41 migration and rollback

v0.1.41 adds the append-only Nmap deployment-authority migration
`f4c5d6e7f8a9` and protected raw-evidence artifact migration
`f5d6e7f8a9b0`. The expected Alembic head is `f5d6e7f8a9b0`.
`sync_credentials` and `sync_delivery_state` remain unchanged.
The v0.1.40 database head before these migrations was `a7b8c9d0e1f2`.

Before upgrade, finish or stop active runs, back up the database, evidence,
reports, encrypted configuration, and record exact artifact hashes. Deploy API,
worker, and frontend together, then check health, version, worker heartbeat,
one report download, and evidence manifest.

To roll back, stop v0.1.41 after active work ends and restore the recorded
compatible v0.1.40 artifact or immutable image digests. Downgrade the database
through `f4c5d6e7f8a9` and `f3b4c5d6e7f8` only after exporting raw-evidence
records and confirming that no protected artifact is still referenced. Keep
the database backup, evidence, reports, encrypted configuration, and raw
artifacts. Mixed-version operation is temporary recovery work, not an accepted
steady state. A rollback to v0.1.27 or earlier requires the documented
`downgrade` operation to `f6a7b8c9d0e1` after exporting Sync v2 receipts and
artifacts.
