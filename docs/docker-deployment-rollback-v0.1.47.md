# v0.1.47 Docker deployment and rollback

Deploy only the immutable API, worker, and frontend digest references in the
verified `docker-image-evidence.json` bundle. Do not rebuild a branch during
deployment.

```text
API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:{{API_IMAGE_DIGEST}}
WORKER_IMAGE={{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}
FRONTEND_IMAGE={{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}
APP_VERSION=v0.1.47
SOURCE_COMMIT={{COMMIT}}
```

End active work, back up persistent volumes, set the exact references, and run
`docker compose ... up -d --no-build`. For inline execution add
`-f infra/docker-compose.inline.yml`. Confirm API health, visible frontend
version, worker heartbeat, an idempotent IP retry, and one report download.

For rollback, stop the v0.1.47 stack, restore the recorded v0.1.46 digest set,
and return the database to `f5d6e7f8a9b0` using the migration guide before
starting the older containers. Do not delete volumes containing evidence,
protected raw artifacts, or encrypted configuration.
