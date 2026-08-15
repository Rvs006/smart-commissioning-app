# v0.1.47 migration and rollback

v0.1.47 advances the Alembic head from `f5d6e7f8a9b0` to
`a6b7c8d9e0f1`. The migration adds `run_idempotency_keys`, the atomic mapping
from a scoped client retry key to its canonical run. It follows the retained
Sync v2 immutable-evidence head `a7b8c9d0e1f2`. `sync_credentials` and
`sync_delivery_state` are unchanged. The migration does not modify IP, BACnet,
MQTT, UDMI, report, evidence, or Nmap policy data.

Before upgrade, finish or stop active runs and back up the database, evidence,
reports, encrypted configuration, and exact artifact hashes. Deploy API,
worker, and frontend from the same v0.1.47 release, then confirm health,
visible version, worker heartbeat, one report download, and the evidence
manifest.

To roll back to v0.1.46, stop v0.1.47 after active work ends, restore the
recorded v0.1.46 EXE or immutable image digests, and run `alembic downgrade
f5d6e7f8a9b0` against the backed-up database before starting the older API.
That downgrade removes idempotency mappings only; the canonical run records
remain. A rollback to v0.1.27 or earlier requires the documented `downgrade` to
`f6a7b8c9d0e1` after exporting Sync v2 receipts and artifacts. Mixed-version
operation is temporary recovery work, not an accepted steady state. Do not run
mixed v0.1.46 and v0.1.47 API or worker processes.
