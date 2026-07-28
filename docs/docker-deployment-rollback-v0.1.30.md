# v0.1.30 Docker deployment and rollback

Deploy v0.1.30 only from the immutable references in
`docker-image-evidence.json`. API, worker, and frontend entries must share the
release commit and the `v0.1.30` OCI version label.

## Deploy

1. Back up the database and persistent artifacts.
2. Set the digest references recorded in the release evidence:

   ```text
   API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:<digest>
   WORKER_IMAGE=ghcr.io/rvs006/smart-commissioning-app-worker@sha256:<digest>
   FRONTEND_IMAGE=ghcr.io/rvs006/smart-commissioning-app-frontend@sha256:<digest>
   APP_VERSION=v0.1.30
   SOURCE_COMMIT=<release-commit>
   ```

3. Pull those exact references, then start the stack without rebuilding:

   ```text
   docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --no-build
   ```

4. For inline execution, add `-f infra/docker-compose.inline.yml`.
5. Confirm API health, frontend version, worker heartbeat, a validation run,
   and one report download before accepting the deployment.

## Roll back

Stop the application containers, restore the recorded v0.1.29 digest
references, and start with `up -d --no-build`. The database remains at
`a7b8c9d0e1f2`.

Do not delete volumes during deployment or rollback. Database, report, signing,
and encrypted-configuration volumes contain evidence that cannot be rebuilt
from container images.
