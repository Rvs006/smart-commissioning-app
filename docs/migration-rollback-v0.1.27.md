# v0.1.27 migration and rollback

v0.1.27 is an application-only upgrade. It adds no Alembic revision, database
table, column, backfill, secret-store format, or report-manifest schema. A full
state copy is still cheaper than discovering that the signing key was missed.

Use this guide for Windows portable and hosted deployments. Keep the v0.1.26
backup until v0.1.27 has passed the long inline-run check and repeated-report
download check.

## Stop conditions

Pause the upgrade when any of these conditions is true:

- the downloaded ZIP or image does not match the release SHA or checksum;
- a backup cannot be opened or its manifest is incomplete;
- an inline or queued run is still active;
- the current Alembic head changes during the upgrade;
- existing run, report, artifact, or secret counts change unexpectedly;
- the new process uses an executable outside the fresh extraction directory.

## 1. Record the boundary

Record the 40-character release commit, current application version, current
Alembic head, and counts for runs, terminal results, report artifacts, and
configuration versions. For a repository deployment:

```sh
git rev-parse HEAD
git status --short
docker compose -f infra/docker-compose.yml --env-file infra/.env config --quiet
```

For Windows portable, record the existing EXE path and its SHA-256:

```powershell
Get-Process SmartCommissioningApp -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty Path
Get-FileHash -Algorithm SHA256 -LiteralPath '<old-extraction>\SmartCommissioningApp.exe'
```

## 2. Quiesce execution

Block new run creation for the maintenance window. Allow active work to finish,
or stop it through the API so retained evidence reaches one terminal state. Stop
the hosted worker before replacing API images:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env stop worker
```

Close `SmartCommissioningApp.exe` for a portable upgrade. Confirm that no old
portable process remains.

## 3. Back up all state

Portable state lives below `%LOCALAPPDATA%\SmartCommissioning`. Copy that whole
directory to restricted storage while the EXE is closed. Include SQLite,
imports, encrypted secrets, report artifacts, logs, and the report-signing key.
Copying only the database produces an incomplete restore.

For hosted deployments, take a Postgres dump and archive the runtime,
`secret_store`, `report_artifacts`, and `report_signing` volumes. Record the
resolved Compose volume names, archive sizes, and SHA-256 values in one backup
manifest. The backup contains operational evidence and encrypted secret material;
limit access and retention accordingly.

The v0.1.26 procedure in `docs/migration-rollback-v0.1.26.md` contains the full
volume archive commands. Reuse those commands with a new `v0.1.27-pre` backup
directory. Do not overwrite the earlier v0.1.26 backup set.

## 4. Replace the application

### Windows portable

1. Download the workflow-built v0.1.27 ZIP from GitHub Releases.
2. Verify its SHA-256 against the release notes and `SHA256SUMS.txt`.
3. Extract it into a completely new empty folder. Exercise one path containing
   spaces during release acceptance.
4. Verify the EXE SHA-256 and Windows Properties **ProductVersion**.
5. Start that exact EXE from File Explorer and confirm the running process path.

The portable bundle is unsigned. Windows SmartScreen may require **More info**,
then **Run anyway**, after the hashes and download source have been checked.
Managed laptops may require IT to allow the release EXE hash. Preserve the old
extraction folder until acceptance finishes; never mix files across versions.

### Hosted

Deploy the v0.1.27 API, worker, and frontend artifacts from the exact release
SHA. v0.1.27 creates no migration, so the Alembic head before and after startup
must match. Start the API first, verify readiness, then start the worker and
frontend:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d api
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d worker frontend
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
```

The normal hosted profile remains queued. A deliberately configured hosted
inline profile receives the same v0.1.27 heartbeat fix as Windows portable.

## 5. Verify the upgrade

Require all of these checks before ending the rollback window:

- the home page returns HTTP 200;
- `/api/v1/health` reports `ok`;
- `/api/v1/ready` reports `ready`, including the database check;
- prior configuration, imports, run history, and generated reports remain
  readable;
- a controlled quiet inline run stays `running` beyond 65 seconds without a
  `lease_expired` stage;
- Stop run reaches one terminal result and leaves no heartbeat thread;
- a separate controlled inline run completes once and keeps its source-run
  provenance;
- downloading the same stored report twice returns byte-identical files;
- logs contain no unhandled exception, repeated heartbeat failure, SQLite lock
  storm, credential text, or heartbeat-thread leak.

The detailed blocking record is `docs/release-validation-v0.1.27.md`.

## 6. Application rollback

Stop new work, finish or cancel active runs, and stop the worker before rollback.
For Windows, close v0.1.27 and launch the intact v0.1.26 extraction. For hosted
deployments, restore the prior application images while keeping the current
database and volumes.

No schema downgrade is required because v0.1.27 introduces no schema change.
Runs and report artifacts created by v0.1.27 stay sealed. Keep them in place and
serve their stored bytes; do not regenerate, edit, or replace terminal evidence.

Restore the pre-upgrade state backup only when state was damaged or the prior
application cannot read it. Test the restore in an isolated location first,
compare the recorded counts and hashes, then replace the deployment as one
complete set. A normal application rollback should leave the state backup
untouched for later audit.

The v0.1.26 inline lease defect returns after application rollback. Schedule long
portable or deliberately inline captures only after v0.1.27 is restored.
