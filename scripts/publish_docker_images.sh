#!/usr/bin/env bash
# Publish accepted candidates, pull by digest, and emit canonical image evidence.
set -euo pipefail

: "${RELEASE_VERSION:?RELEASE_VERSION is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${API_IMAGE:?API_IMAGE is required}"
: "${WORKER_IMAGE:?WORKER_IMAGE is required}"
: "${FRONTEND_IMAGE:?FRONTEND_IMAGE is required}"
: "${GHCR_API_IMAGE:?GHCR_API_IMAGE is required}"
: "${GHCR_WORKER_IMAGE:?GHCR_WORKER_IMAGE is required}"
: "${GHCR_FRONTEND_IMAGE:?GHCR_FRONTEND_IMAGE is required}"

candidate_file="build/release-evidence/candidate-image-ids.env"
test -s "$candidate_file"
API_CANDIDATE_ID="$(sed -n 's/^API_CANDIDATE_ID=//p' "$candidate_file")"
WORKER_CANDIDATE_ID="$(sed -n 's/^WORKER_CANDIDATE_ID=//p' "$candidate_file")"
FRONTEND_CANDIDATE_ID="$(sed -n 's/^FRONTEND_CANDIDATE_ID=//p' "$candidate_file")"
: "${API_CANDIDATE_ID:?API_CANDIDATE_ID is required}"
: "${WORKER_CANDIDATE_ID:?WORKER_CANDIDATE_ID is required}"
: "${FRONTEND_CANDIDATE_ID:?FRONTEND_CANDIDATE_ID is required}"

remote_digest() {
  local reference="$1"
  local raw_file error_file media_type digest
  raw_file="$(mktemp)"
  error_file="$(mktemp)"
  if ! docker buildx imagetools inspect "$reference" --raw >"$raw_file" 2>"$error_file"; then
    if grep -Eiq 'manifest unknown|name unknown|not found' "$error_file"; then
      rm -f "$raw_file" "$error_file"
      return 1
    fi
    cat "$error_file" >&2
    rm -f "$raw_file" "$error_file"
    return 2
  fi
  media_type="$(python -c 'import json,sys; print(json.load(sys.stdin).get("mediaType", ""))' < "$raw_file")"
  case "$media_type" in
    application/vnd.oci.image.manifest.v1+json|application/vnd.docker.distribution.manifest.v2+json) ;;
    *)
      echo "::error::Unsupported or empty manifest mediaType '$media_type': $reference" >&2
      rm -f "$raw_file" "$error_file"
      return 2
      ;;
  esac
  digest="$(docker buildx imagetools inspect "$reference" --format '{{.Manifest.Digest}}')"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
  rm -f "$raw_file" "$error_file"
  printf '%s\n' "$digest"
}

verify_labels() {
  local reference="$1"
  test "$(docker image inspect "$reference" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')" = "$RELEASE_VERSION"
  test "$(docker image inspect "$reference" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$RELEASE_SHA"
  test "$(docker image inspect "$reference" --format '{{ index .Config.Labels "org.opencontainers.image.source" }}')" = "https://github.com/$GITHUB_REPOSITORY"
}

roles=(api worker frontend)
local_images=("$API_IMAGE" "$WORKER_IMAGE" "$FRONTEND_IMAGE")
remote_images=("$GHCR_API_IMAGE" "$GHCR_WORKER_IMAGE" "$GHCR_FRONTEND_IMAGE")
candidate_ids=("$API_CANDIDATE_ID" "$WORKER_CANDIDATE_ID" "$FRONTEND_CANDIDATE_ID")
output_names=(PUBLISHED_API_IMAGE PUBLISHED_WORKER_IMAGE PUBLISHED_FRONTEND_IMAGE)
declare -a sha_references version_references
declare -a sha_digests version_digests final_digests
declare -a sha_present=(0 0 0) version_present=(0 0 0)

# Phase 1 is read-only. Prove all three accepted local candidates and discover
# every existing public SHA/version reference before any package mutation.
for index in "${!roles[@]}"; do
  local_image="${local_images[$index]}"
  remote_image="${remote_images[$index]}"
  candidate_id="${candidate_ids[$index]}"
  sha_references[index]="$remote_image:$RELEASE_SHA"
  version_references[index]="$remote_image:$RELEASE_VERSION"

  test "$(docker image inspect "$local_image" --format '{{.Id}}')" = "$candidate_id"
  verify_labels "$local_image"
  docker image inspect "$local_image" | python scripts/verify_local_docker_image.py --reference "$local_image"

  if sha_digests[index]="$(remote_digest "${sha_references[index]}")"; then
    sha_present[index]=1
  else
    status=$?
    test "$status" -eq 1 || exit "$status"
  fi
  if version_digests[index]="$(remote_digest "${version_references[index]}")"; then
    version_present[index]=1
  else
    status=$?
    test "$status" -eq 1 || exit "$status"
  fi
  if [ "${sha_present[$index]}" -eq 1 ] && [ "${version_present[$index]}" -eq 1 ]; then
    test "${sha_digests[$index]}" = "${version_digests[$index]}"
  fi
done

# Still read-only: pull and validate every existing reference across every
# role. A poisoned alias cannot be discovered after another role was pushed.
for index in "${!roles[@]}"; do
  if [ "${sha_present[$index]}" -eq 1 ]; then
    docker pull "${sha_references[$index]}"
    verify_labels "${sha_references[$index]}"
    test "$(docker image inspect "${sha_references[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  fi
  if [ "${version_present[$index]}" -eq 1 ]; then
    docker pull "${version_references[$index]}"
    verify_labels "${version_references[$index]}"
    test "$(docker image inspect "${version_references[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  fi
done

# Phase 2 publishes only immutable commit-SHA tags. The human-facing version
# aliases are promoted by a later packages-write job after evidence validates.
for index in "${!roles[@]}"; do
  if [ "${sha_present[$index]}" -eq 0 ]; then
    docker tag "${local_images[$index]}" "${sha_references[$index]}"
    docker push "${sha_references[$index]}"
  fi
done

: > build/release-evidence/image-refs.env
for index in "${!roles[@]}"; do
  final_digests[index]="$(remote_digest "${sha_references[index]}")"
  if [ "${sha_present[$index]}" -eq 1 ]; then
    test "${final_digests[$index]}" = "${sha_digests[$index]}"
  fi
  if [ "${version_present[$index]}" -eq 1 ]; then
    test "$(remote_digest "${version_references[$index]}")" = "${final_digests[$index]}"
  fi

  docker image rm "${sha_references[$index]}" "${version_references[$index]}" >/dev/null 2>&1 || true
  immutable_reference="${remote_images[$index]}@${final_digests[$index]}"
  docker pull "$immutable_reference"
  test "$(docker image inspect "$immutable_reference" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  verify_labels "$immutable_reference"

  # Close the tag-movement window after the immutable pull.
  test "$(remote_digest "${sha_references[$index]}")" = "${final_digests[$index]}"
  if [ "${version_present[$index]}" -eq 1 ]; then
    test "$(remote_digest "${version_references[$index]}")" = "${final_digests[$index]}"
  fi
  printf '%s=%s\n' "${output_names[$index]}" "$immutable_reference" >> build/release-evidence/image-refs.env
done

PUBLISHED_API_IMAGE="$(sed -n 's/^PUBLISHED_API_IMAGE=//p' build/release-evidence/image-refs.env)"
PUBLISHED_WORKER_IMAGE="$(sed -n 's/^PUBLISHED_WORKER_IMAGE=//p' build/release-evidence/image-refs.env)"
PUBLISHED_FRONTEND_IMAGE="$(sed -n 's/^PUBLISHED_FRONTEND_IMAGE=//p' build/release-evidence/image-refs.env)"
{
  cat build/release-evidence/image-refs.env
  echo "API_IMAGE=$PUBLISHED_API_IMAGE"
  echo "WORKER_IMAGE=$PUBLISHED_WORKER_IMAGE"
  echo "FRONTEND_IMAGE=$PUBLISHED_FRONTEND_IMAGE"
} >> "$GITHUB_ENV"

python scripts/generate_docker_image_evidence.py \
  --version "$RELEASE_VERSION" \
  --source-commit "$RELEASE_SHA" \
  --repository "$GITHUB_REPOSITORY" \
  --image "api=$PUBLISHED_API_IMAGE" \
  --image "worker=$PUBLISHED_WORKER_IMAGE" \
  --image "frontend=$PUBLISHED_FRONTEND_IMAGE" \
  --output build/release-evidence/docker-image-evidence.json

echo "Accepted Docker images were SHA-published and re-pulled by immutable digest."
