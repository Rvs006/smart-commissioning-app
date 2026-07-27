# v0.1.28 Docker deployment and rollback

v0.1.28 publishes separate API, worker, and frontend images to GHCR. Treat
`docker-image-evidence.json` from the GitHub release as the source of truth. It
records each image as `name@sha256:<digest>`, together with the exact source
commit and OCI version, revision, and source labels.

Do not deploy `latest` or a version tag by itself. Tags are convenient aliases;
the digest reference is the immutable release identity.

## Before deployment

1. Download `docker-image-evidence.json`, `SHA256SUMS.txt`, the five CycloneDX
   SBOMs, and this guide from the same release.
2. Verify every file against `SHA256SUMS.txt`.
3. Confirm `release_version` is `v0.1.28`, `source_commit` is the signed tag
   target, and every image label records that same version and commit.
4. Back up Postgres plus the `api_runtime`, `secret_store`, `report_artifacts`,
   and `report_signing` volumes. Verify the backup manifest before upgrading.
5. Keep the prior release's three digest references with the backup record.

## Deploy the queued profile

Copy `infra/.env.example` to `infra/.env` or run the bootstrap script. Set the
three image variables to the exact values under `images` in
`docker-image-evidence.json`:

```text
API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:<api-digest>
WORKER_IMAGE=ghcr.io/rvs006/smart-commissioning-app-worker@sha256:<worker-digest>
FRONTEND_IMAGE=ghcr.io/rvs006/smart-commissioning-app-frontend@sha256:<frontend-digest>
APP_VERSION=v0.1.28
SOURCE_COMMIT=<signed-tag-commit>
SOURCE_REPOSITORY=https://github.com/Rvs006/smart-commissioning-app
```

Pull and start without permitting a local rebuild:

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env pull api worker frontend
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --no-build
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
```

The API applies the additive database migration before it becomes healthy. The
worker waits for the exact Alembic head, Redis, Postgres, and the shared
encrypted-secret store.

Verify the frontend, health, readiness, and worker state:

```sh
curl --fail http://127.0.0.1:8080/
curl --fail http://127.0.0.1:8080/api/v1/health
curl --fail http://127.0.0.1:8080/api/v1/ready
docker compose -f infra/docker-compose.yml --env-file infra/.env ps
```

## Deliberate inline profile

The hosted default remains queued execution. Use the inline override only when
the deployment decision is explicit. It keeps asynchronous API dispatch and
does not start the worker, which prevents two executor lanes:

```sh
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.inline.yml \
  --env-file infra/.env \
  up -d --no-build
```

Run history records the execution mode. A queued run must report
`dramatiq_worker`; an inline run must report `inline_local_fallback`.

## Image and SBOM verification

The release workflow first accepts the locally built API, worker, and frontend
images in disposable queued, inline, and edge-to-hub stacks. Before any GHCR
write, GitHub must verify that `v0.1.28` is a signed annotated tag pointing
directly to the requested release commit. An absent, lightweight, unverified,
or differently targeted tag stops publication.

Publication then has two separate phases. The first packages-write job archives
the exact accepted image IDs, validates every pre-existing commit and version
reference across all three roles, and publishes commit-SHA tags only. It pulls
each resulting registry digest and proves that the pulled image ID, single-image
manifest type, and OCI labels still match the accepted candidate. The SBOMs and
`docker-image-evidence.json` are generated from those digest-pulled images. A
read-only evidence job validates the complete set. Only then may the second
packages-write job promote the same digests to the `v0.1.28` aliases and pull
those aliases back for one final identity check.

If either phase stops, do not repair an alias by hand. Keep the signed tag fixed,
inspect the workflow evidence, correct the release workflow on a new commit and
version, then rerun the complete gate. A version alias is never the deployment
authority; the digest references in validated evidence are.

You can repeat the label check locally:

```sh
docker image inspect "$API_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.version" }} {{ index .Config.Labels "org.opencontainers.image.revision" }} {{ index .Config.Labels "org.opencontainers.image.source" }}'
```

Repeat for worker and frontend. The three `SBOM.image-*.cdx.json` files are
generated from the published digest references, not from mutable tags.

## Rollback

1. Stop new job submission and allow active runs to finish or cancel them.
2. Record the current three image digests and database migration head.
3. Back up Postgres and all four application volumes again.
4. Replace `API_IMAGE`, `WORKER_IMAGE`, and `FRONTEND_IMAGE` with the prior
   release's recorded digest references.
5. Follow the v0.1.28 migration and rollback guide before moving the database
   head backward. Never run an older API against an unreviewed newer schema.
6. Pull the prior digests and recreate the services with `--no-build`.
7. Verify health, readiness, worker readiness, existing run history, reports,
   encrypted connection material, and terminal evidence immutability.

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env pull api worker frontend
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --no-build --force-recreate
```

Do not delete volumes during an upgrade or rollback. `down -v` belongs only in
the disposable release-acceptance project, never in a deployment holding data.
