# v0.1.37 Docker deployment and rollback

Deploy only the immutable API, worker, and frontend references from the
verified hosted evidence for the exact release commit. Do not rebuild from a
branch tip during deployment.

The hosted evidence bundle includes `docker-image-evidence.json`. Its sanitized
deployment template uses these placeholders:

```text
API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:<digest>
WORKER_IMAGE=ghcr.io/rvs006/smart-commissioning-app-worker@sha256:<digest>
FRONTEND_IMAGE=ghcr.io/rvs006/smart-commissioning-app-frontend@sha256:<digest>
APP_VERSION=v0.1.37
```

## Deploy

1. End active work and back up the database and persistent artifacts.
2. Set the three recorded digest references and `APP_VERSION=v0.1.37`.
3. Start the stack with `docker compose ... up -d --no-build`.
4. For inline execution, add `-f infra/docker-compose.inline.yml`. Confirm API health, frontend version, worker heartbeat, an approved run, and
   one report download.

## Rollback

Restore the previously recorded compatible digest set and start with
`up -d --no-build`. Keep the database at Alembic head `a7b8c9d0e1f2`. Do not delete volumes containing evidence or encrypted configuration.

The actual image names, digests, broker endpoint, and site details belong in the
private deployment record, not this repository document.
