# v0.1.30 migration and rollback

v0.1.30 changes MQTT capture scheduling, database wait bounds, report controls,
and independent capture tooling. It adds no database migration. The required
Alembic head remains `a7b8c9d0e1f2`. The `sync_credentials` and
`sync_delivery_state` tables, along with the other Sync v2 records, are
unchanged.

## Before upgrading

1. Back up the application database, report artifacts, report-signing material,
   and encrypted configuration store.
2. Record the current image digests or Windows ZIP SHA-256.
3. Confirm `alembic current` reports `a7b8c9d0e1f2`.
4. Stop active validation and report jobs. Keep the existing runtime data.

## Upgrade

- Windows portable: extract the v0.1.30 ZIP into a new directory and start the
  new executable. The app continues to use `%LOCALAPPDATA%\SmartCommissioning`.
- Hosted: deploy the three v0.1.30 digest references together, run the normal
  migration command, and confirm the head remains `a7b8c9d0e1f2`.

The executable properties, `README_FIRST.txt`, release evidence, frontend
stamp, and API health version must all identify v0.1.30 and the same source
commit. Renaming an older executable is not an upgrade.

## Rollback

Application rollback needs no Alembic downgrade because the schema did not
change. Stop v0.1.30, restore the recorded v0.1.29 executable or image digests,
and keep the database at `a7b8c9d0e1f2`.

Use a database downgrade only when returning to v0.1.27 or earlier. That path
moves the schema to `f6a7b8c9d0e1` and removes Sync v2 state, so export required
receipts and artifacts first.

## Mixed-version operation

Mixed API, worker, and frontend versions are not an accepted steady state.
Upgrade or roll back all three components together. Release acceptance uses one
exact version and commit across the deployment.
