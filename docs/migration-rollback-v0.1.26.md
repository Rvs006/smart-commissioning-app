# v0.1.26 migration and rollback

Use this procedure for both hosted and portable upgrades. v0.1.26 adds tables
and columns; rollback keeps them in place. The pre-migration backup is the only
supported route back to plaintext secret placement after encrypted secret
extraction.

My preference is a slower maintenance window with a tested restore and written
counts. Fifteen saved minutes are worthless if the signing key is missing.

## Stop conditions

Do not continue when a backup cannot be restored, a worker is still claiming
runs, terminal row counts change unexpectedly, any terminal run lacks a result
hash and seal after backfill, or the target build does not match the approved
release SHA.

## 1. Record the release boundary

```sh
git rev-parse HEAD
git status --short
docker compose -f infra/docker-compose.yml --env-file infra/.env config --quiet
```

Record the 40-character SHA. The worktree must be clean on the release runner.
Save counts for `runs`, `run_issues`, `discovered_devices`,
`discovered_points`, and `discovered_topics`, grouped by run status where useful.

## 2. Drain hosted work

Stop new run creation at the reverse proxy or maintenance control. Wait for
queued and running field operations to finish or cancel them through the API.
Then stop the worker:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env stop worker
```

Confirm no run is `queued`, `running`, or `cancelling`. A retry after the upgrade
creates a new run ID.

## 3. Back up before migration

Create one timestamped set containing all of these items:

```sh
mkdir -p backups/v0.1.26-pre
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > backups/v0.1.26-pre/database.dump

docker run --rm \
  -v <compose-project>_api_runtime:/source:ro \
  -v "$PWD/backups/v0.1.26-pre:/backup" alpine:3.20 \
  tar -czf /backup/api-runtime.tgz -C /source .
```

Replace `<compose-project>` with the prefix shown by `docker volume ls`. Back up
the `secret_store`, `report_artifacts`, and `report_signing` named volumes the
same way, and record every resolved name in the backup manifest.
Keep the database dump and all volume archives together on encrypted storage.

For portable Windows, close `SmartCommissioningApp.exe` and copy the whole
`%LOCALAPPDATA%\SmartCommissioning` directory. Do not copy only the SQLite file.

## 4. Apply and verify the additive migration

Start the v0.1.26 API without the worker. The API owns Alembic migration:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d postgres redis runtime-init api
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T api \
  python -c "from alembic.script import ScriptDirectory; from app.core.config import get_settings; from smart_commissioning_core.db.migrate import build_alembic_config; print(ScriptDirectory.from_config(build_alembic_config(get_settings().database_url)).get_current_head())"
```

The Alembic upgrade performs the terminal backfill in the same migration
transaction. Restart the API once more; the second migration pass must be a
no-op. Verify that the count of terminal `runs` equals the counts in
`run_results` and `run_seals`, and that every terminal row has a 64-character
`result_sha256`. Re-run the issue, device, point, and topic counts saved before
the upgrade. Every count must match.

The migration records `legacy_report_integrity.classification` as `missing`,
`conflicting`, or `present_unverified` on every legacy report before sealing the
backfill. It sets `silently_resigned` to `false`; no old report is regenerated or
signed during migration or download.

On the first configuration read, the compatibility reader moves plaintext
passwords and tokens into versioned encrypted files and writes a new snapshot
containing only `secret://` references. Certificate and private-key material
already uses the encrypted store. Verify that no current configuration snapshot
contains plaintext, then retain the pre-migration backup for the full rollback
window. Compatibility imports remain readable for this release, but exports no
longer return secret values.

## 5. Start and smoke hosted execution

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d worker frontend
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
docker compose -f infra/docker-compose.yml --env-file infra/.env exec -T worker \
  python -c "import bacpypes3, app.tasks; print('hosted worker imports OK')"
```

Worker health must prove Redis, the exact Alembic head, read access to the shared
encrypted-store key, and BACnet imports. Generate and download one report twice;
the bytes and SHA-256 must match.

## 6. Application rollback

Stop the worker first. Deploy the prior application images while leaving every
v0.1.26 table and column in place. Disable v2 creation paths; do not downgrade
the database schema. Older code must not regenerate or modify v0.1.26 report
artifacts.

If the prior version cannot read the additive schema safely, restore the full
pre-migration set into an isolated environment, verify it, and then replace the
deployment. Reversing secret extraction requires that backup because the older
application does not own the v0.1.26 versioned-secret contract.

Run `python scripts/backup_rollback_smoke.py` in CI and before the maintenance
window. It uses disposable state and proves SQLite, encrypted secrets, imports,
report artifacts, and the API-only signing key restore byte for byte.
