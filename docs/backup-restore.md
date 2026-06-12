# Backup and Restore

How to back up and restore the Smart Commissioning App's persistent state for
both deployment profiles, with RPO/RTO guidance for the portable
"engineer-laptop loss" scenario and verification of restored data.

What persists, by profile:

| Profile | System of record | Secret material | Imports / evidence |
| --- | --- | --- | --- |
| Hosted (compose) | Postgres (`postgres_data` volume) | `api_runtime` volume → secrets root | `api_runtime` volume; reports generated in-memory at download |
| Edge / portable | SQLite file under the runtime root | secrets root under the runtime root | runtime root |

In both profiles the **database holds only `secret://` references**, never the
secret bytes; the encrypted secret material (and the Fernet
`.secret_store_key`) lives on disk under the secrets root
(`SMART_COMMISSIONING_SECRETS_ROOT`, default `backend/runtime/secrets/`). A
database backup without the secrets root is therefore **incomplete** — restoring
it gives you configuration that points at secrets you can no longer decrypt.

> The `python -m app.scripts.backup` CLI referenced below for the edge bundle is
> being added in a parallel phase. The manual procedures here work today; treat
> the CLI as the convenience wrapper around the same files. Likewise the
> evidence/backup/verify endpoints are a parallel-phase addition — referenced
> generically.

## 1. Edge / portable: SQLite bundle backup

The portable profile's entire durable state is the runtime directory:

- the SQLite database file (default `backend/runtime/smart_commissioning.db`),
- the secrets root (`backend/runtime/secrets/`), including `.secret_store_key`,
- uploaded import files (`backend/runtime/imports/`),
- any run/evidence artefacts under the runtime root.

### Back up

Stop the portable app (or ensure no run is mid-write) so SQLite is quiescent,
then copy the **whole runtime root** to encrypted external media:

```powershell
# PowerShell, portable laptop
Stop-Process -Name smart-commissioning -ErrorAction SilentlyContinue   # or close the app
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
Compress-Archive -Path .\backend\runtime\* -DestinationPath "D:\backups\sct-edge-$ts.zip"
```

The forthcoming `python -m app.scripts.backup` CLI bundles the same set
(database + secrets root + imports) into a single archive and, per the parallel
phase, writes a manifest with a content hash for verification (section 5). Run a
backup **dry-run first** where the CLI offers one, to confirm the file set
before writing.

Because the SQLite file and the `.secret_store_key` travel together in the
bundle, a restored bundle can decrypt its own secret material. **Encrypt the
backup archive itself** (the bundle contains the Fernet key in cleartext) — use
disk encryption or an encrypted archive password; do not leave an unencrypted
bundle on shared storage.

### Restore

```powershell
# Into a clean runtime root
Expand-Archive -Path "D:\backups\sct-edge-20260612-101500.zip" -DestinationPath .\backend\runtime\
# Start the app; it applies Alembic migrations on first start if the schema is older.
```

On start the API runs `upgrade_to_head`, so a bundle from an older app version
is migrated forward automatically. Verify per section 5.

## 2. Hosted: Postgres backup (pg_dump)

Back up the database with `pg_dump` and the secret material from the
`api_runtime` volume separately, then keep the two together.

### Back up the database

```sh
# Logical dump (portable, version-tolerant). Runs inside the postgres container.
docker compose -f infra/docker-compose.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
  > backups/sct-db-$(date +%Y%m%d-%H%M%S).dump
```

### Back up the secret material (api_runtime volume)

The encrypted secrets + the Fernet key live in the `api_runtime` volume under
the secrets root. Copy them out:

```sh
# Archive the secrets root from the api container's runtime mount.
docker compose -f infra/docker-compose.yml exec -T api \
  tar -czf - -C /app/backend/runtime secrets imports \
  > backups/sct-runtime-$(date +%Y%m%d-%H%M%S).tar.gz
```

Store the `.dump` and the runtime `.tar.gz` **as a pair** (same timestamp) and
on encrypted storage — the tarball contains the Fernet key.

### Restore

```sh
# 1. Restore secret material into the api_runtime volume FIRST.
cat backups/sct-runtime-<ts>.tar.gz | \
  docker compose -f infra/docker-compose.yml exec -T api \
  tar -xzf - -C /app/backend/runtime

# 2. Restore the database (into an empty DB; create it first if needed).
cat backups/sct-db-<ts>.dump | \
  docker compose -f infra/docker-compose.yml exec -T postgres \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists

# 3. Start/restart the api (it migrates to head on startup) and the worker.
docker compose -f infra/docker-compose.yml up -d api worker
```

Restore the secret material **before** the database is served so configuration
that references `secret://...` resolves against present, decryptable material.

## 3. RPO / RTO guidance — "engineer laptop loss" (edge profile)

The portable profile lives on a single technician laptop. Plan for the laptop
being lost, stolen, or failing mid-commissioning.

- **RPO (how much work you can afford to lose):** target **end-of-day** at
  minimum, and **per-site** before leaving a site. The cheap, reliable control
  is: after each significant commissioning session, run the backup (section 1)
  to encrypted external media or a synced encrypted folder. There is no
  continuous replication on the edge — RPO equals "time since your last bundle".
- **RTO (how fast you can be working again):** target **under 30 minutes** on a
  replacement laptop: install the portable build, expand the most recent bundle
  into a clean runtime root, start the app (auto-migrates), verify (section 5).
- **Confidentiality on loss:** the lost laptop holds the SQLite DB **and** the
  Fernet `.secret_store_key` and any uploaded CA/client certs and private keys.
  Mitigate with **full-disk encryption on the laptop** (so a lost device is
  inert), and treat any unencrypted-disk loss as a **secret-compromise incident**:
  rotate the exposed credentials (MQTT/BACnet client material, broker passwords)
  per `docs/runbook.md` section 7, and re-issue the secret-store key on the
  replacement device.
- **Backup hygiene:** keep at least the last few daily bundles (so a corrupt
  latest bundle is not your only copy); store them encrypted and off the laptop.

## 4. Evidence-pack retention

Evidence packs (per-device `state.json` / `pointset.json` / `errors.json`,
summary JSON/XLSX, and signed report bundles) accumulate per run. Retention
policy:

- **Default retention:** keep evidence for the commissioning engagement plus the
  contractual defect-liability / handover period (commonly 12 months); confirm
  the exact window with the project's contract before pruning.
- **Never auto-prune signed handover evidence** that has been delivered as part
  of an acceptance package — those are records, not cache.
- **Retention CLI:** the parallel-phase retention CLI prunes evidence older than
  a configured age. **Always run it `--dry-run` first** to see exactly which
  artefacts would be deleted, confirm against the retention window, then run for
  real. Take a backup (sections 1–2) before any destructive prune.
- **Edge:** evidence under the portable runtime root is included in the section-1
  bundle, so a backed-up bundle is itself the retained copy.

## 5. Verifying a restore

A backup you cannot restore is not a backup. After any restore:

1. **Process + readiness:** start the app and confirm
   `GET /api/v1/health` is `ok` and `GET /api/v1/ready` is `ready`
   (`docs/runbook.md` section 4). A 503 here means the DB did not come back.
2. **Schema current:** the API applies `upgrade_to_head` on startup; check the
   startup log shows the migration step completing without error.
3. **Configuration + secrets resolve:** load the configuration via the API and
   confirm secret fields come back **masked** (`********` / `secret://...`) —
   masking working proves the secret-store key restored and the material is
   decryptable. If a `secret://` reference cannot be read, the secrets root /
   Fernet key did not restore with the database.
4. **Run/import counts match:** spot-check run and import counts against the
   pre-backup state (the `sct_runs_by_status` metric and the runs list).
5. **Signature / hash check of restored evidence:** verify restored evidence
   packs against their recorded signature/hash using the evidence
   verify endpoint / CLI (parallel phase). For a bundle produced by the backup
   CLI, verify the bundle's manifest content hash before trusting it. A
   signature/hash mismatch means the artefact was altered or corrupted in
   transit — discard and restore from an earlier, verifiable copy.

Record the restore drill outcome (when, which backup, verification result) so
the RPO/RTO targets in section 3 are evidence-based, not aspirational.
