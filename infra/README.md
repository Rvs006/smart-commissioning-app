# Smart Commissioning App: hosted deployment (Docker Compose)

This compose stack is the **hosted profile**: nginx-served frontend, FastAPI
backend, Dramatiq worker, Postgres, and password-protected Redis. The
**edge profile** is the portable Windows executable built from
`packaging/windows_portable/`, which binds to `127.0.0.1` only, uses SQLite,
runs jobs inline, and defaults to `AUTH_MODE=local` (no API key). Use compose
when several users share a server; use the portable exe on a technician laptop.

## Required environment

Generate `.env` from the template with the bootstrap script. It fills every
`CHANGE_ME` with a fresh crypto-random secret, prints the `API_KEY`, and
refuses to overwrite an existing `.env`:

```sh
# from the repo root
sh scripts/bootstrap-env.sh        # Windows: pwsh scripts/bootstrap-env.ps1
```

Manual fallback: `cp infra/.env.example infra/.env`, then fill each
`CHANGE_ME` yourself. Compose fails fast (via `${VAR:?}`) when any of these
are missing:

| Variable | Purpose |
| --- | --- |
| `POSTGRES_DB`, `POSTGRES_USER` | Database name/role shared by api and worker. |
| `POSTGRES_PASSWORD` | Postgres password. Generate: `openssl rand -hex 32` |
| `REDIS_PASSWORD` | Redis `requirepass` password. Generate: `openssl rand -hex 32` |
| `API_KEY` | Shared key clients must send when `AUTH_MODE=api_key`. Generate: `openssl rand -hex 32` |
| `SMART_COMMISSIONING_DEPLOYMENT_ID` | Stable non-secret name used when deriving unique MQTT client IDs. |
| `FRONTEND_PORT` (optional, default 8080) | Loopback host port for the frontend + `/api` proxy. |
| `API_PORT` (optional, default 8000) | Loopback host port for direct API debugging. |
| `POSTGRES_PORT` (optional, default 5432) | Loopback host port for direct Postgres debugging. |
| `CORS_ORIGINS` (optional) | Comma-separated origins for direct cross-origin API access. |

`DATABASE_URL` and `REDIS_URL` are assembled inside `docker-compose.yml` from
the values above. Do not define them separately.

## First start

```sh
docker compose -f infra/docker-compose.yml up -d --build
```

Release deployments use the immutable `name@sha256` references from
`docker-image-evidence.json`. Put them in `API_IMAGE`, `WORKER_IMAGE`, and
`FRONTEND_IMAGE`, then use `pull` followed by `up -d --no-build`. This prevents
Compose from replacing a verified registry image with a local build. See
`docs/docker-deployment-rollback-v0.1.28.md` for the complete deployment,
verification, backup, and rollback procedure.

Startup order is explicit. `runtime-init` creates the encrypted-store key and
sets named-volume ownership. Postgres and Redis must be healthy before the API
starts; the API applies Alembic migrations; the worker waits for API readiness.
Worker readiness then checks Redis, the exact Alembic head, read access to the
encrypted-store key, the task module, and `bacpypes3`.

Hosted execution is always `JOB_EXECUTION_MODE=queue`. If Redis publication is
unavailable, the durable outbox keeps the dispatch pending for automatic retry.
The API never starts a second inline copy of a queued job.

`infra/docker-compose.inline.yml` is the explicit parity profile. It sets the
API to asynchronous inline execution and keeps the worker behind a non-default
profile so only one executor lane is active. Do not add that override to a
normal queued deployment.

Open the app at `http://127.0.0.1:8080` (or your `FRONTEND_PORT`). Everything
binds to loopback only. To expose the app beyond the host, put a
TLS-terminating reverse proxy in front of the frontend port.

## First named administrator on an edge or hub

When the API service is configured with `DEPLOYMENT_ROLE=edge` or
`DEPLOYMENT_ROLE=hub`, the shared `API_KEY` reports `global_scope: false` and
cannot administer user-facing project data. Create the first named
administrator offline:

```sh
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.scripts.bootstrap_admin --username site-admin
```

For a Compose override that renames the API service, replace `api` with that
service name. The last stdout line is the raw key and appears once. Store it at
once; only its SHA-256 hash is written to Postgres. The command assigns the
fixed `admin` role, refuses while an active named administrator exists, permits
recovery when the active count is zero, and fails closed on duplicate or
concurrent attempts.

## Verifying health

```sh
# Container-level view (healthcheck status per service)
docker compose -f infra/docker-compose.yml ps

# Liveness and readiness through the nginx proxy
curl http://127.0.0.1:8080/api/v1/health
curl http://127.0.0.1:8080/api/v1/ready

# Or directly against the api port
curl http://127.0.0.1:8000/api/v1/ready

# Worker must report healthy, then prove the real BACnet package once more.
docker compose -f infra/docker-compose.yml exec -T worker \
  python -c "import bacpypes3, app.tasks; print('worker imports OK')"
```

`/api/v1/ready` returns 503 until migrations have run and the run store is
reachable. Redis has no published host port; to ping it:

```sh
docker compose -f infra/docker-compose.yml exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping'
```

## Rotating secrets

The `secret_store` volume is mounted read/write in the API and read-only in the
worker. Both processes use `/app/runtime/secrets`, including the same
`.secret_store_key`. Report signing uses the separate `report_signing` volume,
which is never mounted in the worker. Immutable report bytes use the
`report_artifacts` volume and stay API-owned.

**API_KEY:** generate a new value (`openssl rand -hex 32`), update `.env`,
then recreate the api so it picks up the new environment:

```sh
docker compose -f infra/docker-compose.yml up -d api
```

Clients (the frontend stores the key locally in the browser) must re-enter the
new key. No data is affected.

To rotate **all** secrets at once, move `infra/.env` aside and re-run
`scripts/bootstrap-env.sh` / `.ps1` (it refuses to overwrite an existing
`.env`). The `POSTGRES_PASSWORD` in-place caveat below still applies.

**REDIS_PASSWORD:** update `.env`, then recreate redis, api, and worker
together (all three embed the password in their environment):

```sh
docker compose -f infra/docker-compose.yml up -d redis api worker
```

In-flight queued jobs survive (Redis persists to the `redis_data` volume with
appendonly enabled).

**POSTGRES_PASSWORD:** the postgres image only applies `POSTGRES_PASSWORD` on
first initialization, so change the role password in place first, then update
`.env` and recreate the dependents:

```sh
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER $POSTGRES_USER WITH PASSWORD '<new-password>';"
# then edit .env and:
docker compose -f infra/docker-compose.yml up -d api worker
```

## Notes

- Images run as non-root users. Uploaded imports live in `api_runtime`,
  encrypted connection material in `secret_store`, report bytes in
  `report_artifacts`, and the signing key in `report_signing`.
- The worker image installs `core[bacnet]`. A green healthcheck proves the
  import, not live device communication. Real controller validation remains an
  on-site gate.
- Set broker ACLs for dynamic client IDs before rollout. See
  `docs/mqtt-client-id-and-acl.md`.
- Back up all four runtime volumes with Postgres before migration. Follow the
  migration guide for the version being deployed; a database-only backup is
  incomplete.
- Postgres publishes `127.0.0.1:${POSTGRES_PORT:-5432}` for host `psql` access
  during development; remove that mapping in locked-down deployments.
- The former MinIO/object-storage service was removed: nothing in
  `backend/`, `worker/`, or `core/` ever used it.
