# Backup and restore

Back up the database and its filesystem state as one timestamped set. A lone
database dump cannot decrypt `secret://` references, reproduce immutable report
bytes, or verify signatures if the matching keys are missing.

My preference is to rehearse a restore before a site handover. Finding a missing
4 KB signing-key file during an incident is an avoidable failure.

| Profile | Database | Secret material | Imports and evidence |
| --- | --- | --- | --- |
| Hosted Compose | `postgres_data` | `secret_store` (API read/write, worker read-only) and API-only `report_signing` | `api_runtime` imports and immutable `report_artifacts` |
| Windows portable | SQLite under the runtime root | `secrets/` and `report-signing/` | `imports/` and immutable `artifacts/` |

## Windows portable

The frozen application keeps its durable state in
`%LOCALAPPDATA%\SmartCommissioning`, unless `SMART_COMMISSIONING_DATA_DIR` is
set. A development checkout defaults to `backend/runtime`.

Close `SmartCommissioningApp.exe`, then copy the complete runtime directory to
encrypted storage:

```powershell
Stop-Process -Name SmartCommissioningApp -ErrorAction SilentlyContinue
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Compress-Archive `
  -Path "$env:LOCALAPPDATA\SmartCommissioning\*" `
  -DestinationPath "D:\backups\sct-edge-$timestamp.zip"
```

The directory must include all of these items:

- `smart_commissioning.db`
- `secrets/`, including `.secret_store_key`
- `imports/`
- `artifacts/`
- `report-signing/`, including the persisted evidence-signing key

Pre-v0.1.9 directories beside the executable are stale after migration. Use the
Local AppData runtime as the source.

`python -m app.scripts.backup create --out <backup.zip>` and
`POST /api/v1/evidence/backup` create a consistent, signed SQLite bundle with
the database, encrypted secrets, import files, immutable report artifacts, and
the separate report-signing root. Verify the bundle manifest before restore.
The quiescent whole-directory archive above remains the simplest manual option.

Restore the archive into a clean runtime root, start the application, and let
the API apply additive Alembic migrations:

```powershell
Expand-Archive `
  -Path "D:\backups\sct-edge-20260726-101500.zip" `
  -DestinationPath "$env:LOCALAPPDATA\SmartCommissioning"
```

## Hosted Compose

Drain and stop the worker before taking a maintenance backup. Create one folder
for the Postgres dump and all four runtime-volume archives.

### Database

```sh
backup_dir="backups/sct-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$backup_dir/database.dump"
```

### Volumes

Resolve the actual Compose-prefixed names first:

```sh
docker volume ls
```

Archive `api_runtime`, `secret_store`, `report_artifacts`, and `report_signing`.
Replace `<compose-project>` and `<timestamp>` with the recorded values:

```sh
docker run --rm \
  -v <compose-project>_secret_store:/source:ro \
  -v "$PWD/backups/sct-<timestamp>:/backup" alpine:3.20 \
  tar -czf /backup/secret-store.tgz -C /source .
```

Repeat that command for the other three volumes. Encrypt the resulting backup
set because the secret archive holds the decryption key and the signing archive
holds the report-signing key.

### Restore

1. Stop the API, worker, and frontend.
2. Restore all four named volumes from the same timestamped set.
3. Restore the database into an empty Postgres database.
4. Start runtime initialization and the API. Let migrations finish before the
   worker starts.

```sh
cat backups/sct-<timestamp>/database.dump | \
  docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists'

docker compose -f infra/docker-compose.yml --env-file infra/.env \
  up -d runtime-init api
docker compose -f infra/docker-compose.yml --env-file infra/.env \
  up -d worker frontend
```

Keep the v0.1.26 schema during an application rollback. The release migration is
additive, and older application images must not rewrite sealed results or report
artifacts. Use `docs/migration-rollback-v0.1.26.md` for the complete sequence.

## RPO and RTO for a lost engineer laptop

- Back up at the end of each day and before leaving a commissioning site. The
  recovery-point objective is the time since that archive was written.
- Target a 30-minute recovery on a replacement laptop: install the portable
  build, restore the latest archive, start the app, and run the checks below.
- Use full-disk encryption. If an unencrypted laptop is lost, rotate the broker
  passwords, certificates, private keys, and other site credentials.
- Retain several dated archives off the laptop. One corrupt latest copy should
  not become the only recovery option.

## Retention

Keep signed handover evidence for the contractual retention period. Do not
auto-prune delivered acceptance records.

Preview database retention first:

```sh
python -m app.scripts.retention --keep-days 365
```

Add `--apply` only after checking the preview and taking a current backup.

## Restore verification

After every restore:

1. Confirm `GET /api/v1/health` returns `ok` and `GET /api/v1/ready` returns
   `ready`.
2. Confirm the Alembic revision equals the application migration head.
3. Load stored configuration through the API. Secret values must remain masked,
   and a worker smoke run must resolve the shared encrypted material.
4. Compare run, issue, device, point, topic, and import counts with the recorded
   pre-backup counts.
5. Download one completed report twice. The bytes and SHA-256 must match.
6. Call `GET /api/v1/evidence/reports/{report_id}/verify`; require matching hash,
   a valid signature, and the expected signing-key fingerprint.
7. Run `python scripts/backup_rollback_smoke.py` for the disposable byte-exact
   backup check.

Record the date, backup identifier, source version, target version, and each
verification result. That record is the evidence that the recovery targets are
real.
