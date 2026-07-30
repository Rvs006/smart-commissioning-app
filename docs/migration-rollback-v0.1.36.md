# v0.1.36 migration and rollback

v0.1.36 adds release-contract and operator-surface fixes without a database
migration. The required Alembic head remains a7b8c9d0e1f2. The
sync_credentials and sync_delivery_state tables, along with the other Sync v2
records, are unchanged.

## Before upgrading

1. Schedule the upgrade only after active validation and report work has ended.
   Do not stop or alter a live field run to perform this release work.
2. Back up the application database, report artifacts, report-signing material,
   and encrypted configuration store.
3. Record the current exact image digests or Windows ZIP SHA-256.
4. Confirm alembic current reports a7b8c9d0e1f2.

## Upgrade

- Windows portable: extract the v0.1.36 ZIP into a new directory and start the
  new executable. The app continues to use %LOCALAPPDATA%\SmartCommissioning.
- Hosted: deploy the three v0.1.36 digest references together, run the normal
  migration command, and confirm the head remains a7b8c9d0e1f2.

The executable properties, README_FIRST.txt, release evidence, frontend stamp,
and API health version must all identify v0.1.36 and the same source commit.
Renaming an older executable is not an upgrade.

## Rollback

Application rollback needs no Alembic downgrade because the schema did not
change. Stop v0.1.36 only after active work ends, restore a previously recorded
compatible executable or image digest set, and keep the database at
a7b8c9d0e1f2.

Use a database downgrade only when returning to v0.1.27 or earlier. That path
moves the schema to f6a7b8c9d0e1 and removes Sync v2 state, so export required
receipts and artifacts first.

## Mixed-version operation

Mixed API, worker, and frontend versions are not an accepted steady state.
Upgrade or roll back all three components together. Release acceptance uses one
exact version and commit across the deployment.
