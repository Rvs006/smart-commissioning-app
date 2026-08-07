# v0.1.39 migration and rollback

v0.1.39 adds application, report, evidence, and release-provenance fixes
without a database migration. The Alembic head remains `a7b8c9d0e1f2`.

## Before upgrade

1. Finish or safely stop active validation and report work.
2. Back up the database, report artifacts, signing material, and encrypted
   configuration store.
3. Record the exact portable ZIP SHA-256 or hosted image digests.
4. Confirm the current Alembic head is `a7b8c9d0e1f2`.

## Upgrade

Use the exact v0.1.39 workflow artifacts. Deploy the API, worker, and frontend
together, or extract the portable package into a new directory. Confirm health,
the frontend version, worker heartbeat, one report download, and the presence of
the evidence-set manifest before resuming field work.

## Rollback

Stop v0.1.39 after active work ends, restore the previously recorded compatible
v0.1.38 package or image digests, and leave the database at
`a7b8c9d0e1f2`. Do not delete evidence, report, signing, or encrypted-
configuration volumes.

Mixed API, worker, and frontend versions are not an accepted steady state.

The `sync_credentials` and `sync_delivery_state` tables remain unchanged. A
rollback to v0.1.27 or earlier is a separate database operation: export the
required receipts and artifacts first, follow the documented `downgrade` path,
and confirm the older Alembic head `f6a7b8c9d0e1` before removing Sync v2 state.
Mixed-version operation is temporary recovery work only, not an accepted
steady state.
