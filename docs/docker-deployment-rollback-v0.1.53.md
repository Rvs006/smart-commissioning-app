# v0.1.53 Docker deployment and rollback

Deploy only the immutable API, worker, and frontend digest references in the
verified `docker-image-evidence.json` bundle. Do not rebuild a branch during
deployment.

```text
API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:{{API_IMAGE_DIGEST}}
WORKER_IMAGE={{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}
FRONTEND_IMAGE={{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}
APP_VERSION=v0.1.53
SOURCE_COMMIT={{COMMIT}}
```

End active work, back up persistent volumes, set the exact references, and run
`docker compose ... up -d --no-build`. For inline execution add
`-f infra/docker-compose.inline.yml`. Confirm API health, the visible frontend
version, the updated Brief and Learning pages, worker heartbeat, an approved
sealed IP preview, and one report download.

For rollback, stop the v0.1.53 stack and restore the recorded v0.1.52 digest
set. Both versions retain the same database head, so no database downgrade is
required. Do not delete volumes containing evidence, protected raw artifacts,
or encrypted configuration.
