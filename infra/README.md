# Smart Commissioning App — hosted deployment (Docker Compose)

This compose stack is the **hosted profile**: nginx-served frontend, FastAPI
backend, Dramatiq worker, Postgres, and password-protected Redis. The
**edge profile** is the portable Windows executable built from
`packaging/windows_portable/`, which binds to `127.0.0.1` only, uses SQLite,
runs jobs inline, and defaults to `AUTH_MODE=local` (no API key). Use compose
when several users share a server; use the portable exe on a technician laptop.

## Required environment

Copy the template and fill in real values — compose fails fast (via
`${VAR:?}`) when any of these are missing:

```sh
cd infra
cp .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `POSTGRES_DB`, `POSTGRES_USER` | Database name/role shared by api and worker. |
| `POSTGRES_PASSWORD` | Postgres password. Generate: `openssl rand -hex 32` |
| `REDIS_PASSWORD` | Redis `requirepass` password. Generate: `openssl rand -hex 32` |
| `API_KEY` | Shared key clients must send when `AUTH_MODE=api_key`. Generate: `openssl rand -hex 32` |
| `FRONTEND_PORT` (optional, default 8080) | Loopback host port for the frontend + `/api` proxy. |
| `API_PORT` (optional, default 8000) | Loopback host port for direct API debugging. |
| `CORS_ORIGINS` (optional) | Comma-separated origins for direct cross-origin API access. |

`DATABASE_URL` and `REDIS_URL` are assembled inside `docker-compose.yml` from
the values above — do not define them separately.

## First start

```sh
docker compose -f infra/docker-compose.yml up -d --build
```

Startup order is handled by healthchecks: Postgres and Redis must report
healthy before the api starts; the api applies Alembic migrations on startup
(it owns the schema); the worker waits for the api so the schema exists.

Open the app at `http://127.0.0.1:8080` (or your `FRONTEND_PORT`). Everything
binds to loopback only — to expose the app beyond the host, put a
TLS-terminating reverse proxy in front of the frontend port.

## Verifying health

```sh
# Container-level view (healthcheck status per service)
docker compose -f infra/docker-compose.yml ps

# Liveness and readiness through the nginx proxy
curl http://127.0.0.1:8080/api/v1/health
curl http://127.0.0.1:8080/api/v1/ready

# Or directly against the api port
curl http://127.0.0.1:8000/api/v1/ready
```

`/api/v1/ready` returns 503 until migrations have run and the run store is
reachable. Redis has no published host port; to ping it:

```sh
docker compose -f infra/docker-compose.yml exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping'
```

## Rotating secrets

**API_KEY** — generate a new value (`openssl rand -hex 32`), update `.env`,
then recreate the api so it picks up the new environment:

```sh
docker compose -f infra/docker-compose.yml up -d api
```

Clients (the frontend stores the key locally in the browser) must re-enter the
new key. No data is affected.

**REDIS_PASSWORD** — update `.env`, then recreate redis, api, and worker
together (all three embed the password in their environment):

```sh
docker compose -f infra/docker-compose.yml up -d redis api worker
```

In-flight queued jobs survive (Redis persists to the `redis_data` volume with
appendonly enabled).

**POSTGRES_PASSWORD** — the postgres image only applies `POSTGRES_PASSWORD` on
first initialization, so change the role password in place first, then update
`.env` and recreate the dependents:

```sh
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER $POSTGRES_USER WITH PASSWORD '<new-password>';"
# then edit .env and:
docker compose -f infra/docker-compose.yml up -d api worker
```

## Notes

- Images run as non-root users; the api's writable state (uploaded import
  files, secret material, dev SQLite) lives in the `api_runtime` volume.
- Postgres publishes `127.0.0.1:5432` for host `psql` access during
  development; remove that mapping in locked-down deployments.
- The former MinIO/object-storage service was removed: nothing in
  `backend/`, `worker/`, or `core/` ever used it.
