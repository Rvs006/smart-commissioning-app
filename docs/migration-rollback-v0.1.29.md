# v0.1.29 migration and rollback

v0.1.29 changes MQTT capture, report rendering, and report controls. It adds no
database migration. The required Alembic head remains `a7b8c9d0e1f2`, the same
head published with v0.1.28. `sync_credentials`, `sync_delivery_state`, and the
other Sync v2 tables remain unchanged.

## Before upgrading

1. Back up the application database, report artifacts, report-signing material,
   and encrypted configuration store.
2. Record the current image digests or Windows ZIP SHA-256.
3. Confirm `alembic current` reports `a7b8c9d0e1f2`.
4. Stop active validation and report jobs. Keep the existing runtime data.

## Upgrade

- Windows portable: extract the v0.1.29 ZIP into a new directory and start the
  new executable. The app continues to use `%LOCALAPPDATA%\SmartCommissioning`.
- Hosted: deploy the three v0.1.29 digest references together, run the normal
  migration command, and confirm the head is still `a7b8c9d0e1f2`.

Do not reuse a v0.1.28 executable renamed as v0.1.29. The executable properties,
`README_FIRST.txt`, release evidence, frontend stamp, and API health version must
all identify v0.1.29 and the same source commit.

## Rollback

Application rollback does not require an Alembic downgrade because the schema
does not change. Stop v0.1.29, restore the recorded v0.1.28 executable or image
digests, and keep the database at `a7b8c9d0e1f2`.

Use a database downgrade only when returning to v0.1.27 or earlier. That older
path moves from `a7b8c9d0e1f2` to `f6a7b8c9d0e1` and removes Sync v2 state, so
export required receipts and artifacts before running it.

## Mixed-version operation

Mixed-version API, worker, and frontend deployments are not an accepted steady
state. Upgrade or roll back all three components together. A v0.1.28 edge and a
v0.1.29 hub retain the v0.1.28 Sync v1/v2 compatibility rules, but release
acceptance still uses one exact version and commit across the deployment.
