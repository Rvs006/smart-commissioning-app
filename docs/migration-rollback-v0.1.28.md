# v0.1.28 migration and rollback

v0.1.28 advances Alembic from `f6a7b8c9d0e1` to `a7b8c9d0e1f2`. The migration
adds five Sync v2 tables and leaves existing run, context, result, seal, report,
v1 sync, secret, and artifact records unchanged.

The new tables are:

- `sync_credentials`, containing hashed machine credentials and edge key binding;
- `sync_credential_scopes`, containing approved project/site pairs;
- `sync_artifacts`, containing immutable metadata for exact imported report bytes;
- `sync_receipts`, containing per-item delivery outcomes;
- `sync_delivery_state`, containing the v2 acknowledgement watermark.

The downgrade drops those tables. Treat that as loss of credential scopes,
receipt audit history, v2 watermarks, and hub pointers to imported artifact bytes.
Back up first.

## Stop conditions

Stop the upgrade or rollback when any condition below is true:

- a release asset, OCI digest, SBOM, or source label names a different commit;
- the database, runtime, secret, artifact, or signing-key backup is incomplete;
- an inline or queued run is active;
- an edge is sending a Sync v2 bundle;
- API and worker images do not use the same version and source revision;
- the Alembic head is not `f6a7b8c9d0e1` before upgrade or
  `a7b8c9d0e1f2` after upgrade;
- receipt, artifact, result, seal, or report counts change unexpectedly.

## 1. Record the boundary

Record the release commit, application version, current Alembic revision, image
digests, and row counts for all run-lifecycle and Sync tables. For hosted
Postgres:

```sh
git rev-parse HEAD
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T postgres \
  sh -ec 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"'
```

For Windows portable, close the application and record the EXE path/hash plus the
SQLite revision:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '<old-extraction>\SmartCommissioningApp.exe'
python -c "import sqlite3; p=r'$env:LOCALAPPDATA\SmartCommissioning\smart_commissioning.db'; print(sqlite3.connect(p).execute('select version_num from alembic_version').fetchone()[0])"
```

## 2. Quiesce execution and sync

Block new run creation and Sync uploads. Let active work finish or cancel it
through the API so it reaches one terminal result and seal. Stop the hosted
worker before the API image changes:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env stop worker
```

Close `SmartCommissioningApp.exe` before copying portable state. Confirm there is
no remaining listener or process.

## 3. Back up the complete state set

Portable state lives under `%LOCALAPPDATA%\SmartCommissioning`. Copy the whole
directory to restricted storage, including SQLite, imports, encrypted secrets,
report artifacts, logs, and the report-signing key. Record archive size and
SHA-256.

Hosted backup requires one consistent Postgres dump plus the `api_runtime`,
`secret_store`, `report_artifacts`, and `report_signing` volumes. Record the
resolved Compose volume names before archiving. The database and runtime files
form one restore set; copying only Postgres drops exact report bytes and signing
material.

Keep the pre-v0.1.28 backup until queued and inline lifecycle tests, Sync v2
exact-byte transfer, and repeat downloads pass.

## 4. Upgrade

### Hosted

Pull the API, worker, and frontend by the immutable references in
`docker-image-evidence.json`. Confirm their OCI revision labels equal the release
commit. Update the deployment to those digests, then start the API first:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d postgres redis runtime-init api
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T api \
  python -m alembic -c /app/core/alembic.ini current
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d worker frontend
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
```

The API migration must reach `a7b8c9d0e1f2` before the worker starts. Deploy API
and worker from the same v0.1.28 commit; running a v0.1.27 worker against the new
head fails the exact-head readiness check.

### Windows portable

1. Download the workflow-built v0.1.28 ZIP from GitHub Releases.
2. Verify the ZIP and EXE SHA-256 values against the release and
   `SHA256SUMS.txt`.
3. Extract into a new empty directory. Never mix portable versions.
4. Verify EXE `ProductVersion` and the frontend build stamp.
5. Launch that exact EXE from File Explorer. Startup upgrades the local SQLite
   database to `a7b8c9d0e1f2`.

State stays under `%LOCALAPPDATA%\SmartCommissioning`, outside the extraction.

## 5. Verify the upgrade

Require all of the following before reopening normal work:

- `/`, `/api/v1/health`, and `/api/v1/ready` return HTTP 200;
- the database check reports `ok` and Alembic reports `a7b8c9d0e1f2`;
- prior projects, sites, configuration, imports, runs, reports, artifacts, and
  secrets remain readable;
- API and worker use the shared heartbeat timing and frozen context;
- one queued and one deliberately inline capture cross the original lease
  boundary without recovery while alive;
- worker termination and redelivery produce one recovery, result, and seal;
- Redis interruption creates no inline copy;
- a scoped Sync v2 credential accepts an authorized item, rejects an unauthorized
  pair generically, and mixed receipts advance only acknowledged IDs;
- the hub download equals the edge report bytes twice;
- browser console and API, worker, Redis, Postgres, and frontend logs contain no
  unhandled error or secret sentinel.

Follow `release-validation-v0.1.28.md` for the complete blocking record.

## 6. Mixed-version behavior

- A v0.1.28 edge selects v2 only after an authenticated hub advertises protocol
  `2.0`.
- A genuine older hub response can select the unchanged v1 endpoint. V1 delivery
  cannot mark a v2 item acknowledged.
- A v0.1.27 edge may continue to use the v1 endpoint on a v0.1.28 hub.
- Authentication failure, timeout, malformed capability data, and server error
  stop delivery. They never trigger a downgrade.
- New v0.1.28 report evidence requires a terminal result, seal, frozen snapshot,
  signed manifest, and exact bytes. Legacy incomplete report runs remain v1 or
  pending.

## 7. Application rollback while keeping v0.1.28 state

A v0.1.27 API or worker expects Alembic head `f6a7b8c9d0e1`. It must not be started
against a database still at `a7b8c9d0e1f2`. If v2 state must be retained, keep
the v0.1.28 schema and roll forward with corrected v0.1.28 images instead.

Frontend-only rollback is possible when its API contract remains compatible,
but record the image digest and test all controls before reopening access.

## 8. Schema downgrade to v0.1.27

Use this only when losing active v2 delivery state is approved and the backup has
been tested.

1. Stop edge uploads, frontend, worker, and API.
2. Export `sync_credentials` metadata without plaintext keys,
   `sync_credential_scopes`, `sync_artifacts`, `sync_receipts`, and
   `sync_delivery_state` to restricted audit storage. Archive every hub-owned
   imported artifact referenced by `sync_artifacts`.
3. Record export counts and hashes.
4. Run the downgrade with the v0.1.28 API image:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env run --rm api \
  python -m alembic -c /app/core/alembic.ini downgrade f6a7b8c9d0e1
```

5. Confirm the five Sync v2 tables are gone, `alembic_version` is
   `f6a7b8c9d0e1`, and existing run/result/seal/report counts still match.
6. Deploy the exact prior API, worker, and frontend digests together.
7. Start API, then worker and frontend. Verify health, readiness, v1 sync, prior
   reports, and a controlled queued run.

For Windows portable, use the same order with a v0.1.28 maintenance environment
against a copied SQLite database. Downgrade the copy first and verify it before
replacing live state or launching the intact v0.1.27 extraction.

Restore the full pre-upgrade backup only if state is damaged or the controlled
downgrade fails. Test that restore in an isolated Compose project or copied
portable directory before replacing live state.
