#!/usr/bin/env bash
# Promote validated commit-SHA images to the requested release aliases after evidence passes.
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
evidence_file="build/release-evidence/docker-image-evidence.json"
test -s "$candidate_file"
test -s "$evidence_file"

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

mapfile -t immutable_references < <(
  python - "$evidence_file" "$RELEASE_VERSION" "$RELEASE_SHA" "$GITHUB_REPOSITORY" \
    "$GHCR_API_IMAGE" "$GHCR_WORKER_IMAGE" "$GHCR_FRONTEND_IMAGE" <<'PY'
import json
import re
import sys

path, version, commit, repository, *names = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    evidence = json.load(stream)
expected_top = {"schema_version", "release_version", "source_commit", "registry", "images"}
assert set(evidence) == expected_top
assert evidence["schema_version"] == "1.0"
assert evidence["release_version"] == version
assert evidence["source_commit"] == commit
assert evidence["registry"] == "ghcr.io"
assert set(evidence["images"]) == {"api", "worker", "frontend"}
expected_labels = {
    "org.opencontainers.image.version": version,
    "org.opencontainers.image.revision": commit,
    "org.opencontainers.image.source": f"https://github.com/{repository}",
}
for role, name in zip(("api", "worker", "frontend"), names, strict=True):
    image = evidence["images"][role]
    assert set(image) == {"name", "digest", "immutable_reference", "labels"}
    assert image["name"] == name
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])
    assert image["immutable_reference"] == f"{name}@{image['digest']}"
    assert image["labels"] == expected_labels
    print(image["immutable_reference"])
PY
)
test "${#immutable_references[@]}" -eq 3

roles=(api worker frontend)
local_images=("$API_IMAGE" "$WORKER_IMAGE" "$FRONTEND_IMAGE")
remote_images=("$GHCR_API_IMAGE" "$GHCR_WORKER_IMAGE" "$GHCR_FRONTEND_IMAGE")
candidate_ids=("$API_CANDIDATE_ID" "$WORKER_CANDIDATE_ID" "$FRONTEND_CANDIDATE_ID")
declare -a expected_digests sha_references version_references
declare -a version_digests
declare -a version_present=(0 0 0)

# Read-only preflight across all roles. Validate accepted bytes, immutable
# evidence, SHA tags, and any existing version aliases before any promotion.
for index in "${!roles[@]}"; do
  expected_digests[index]="${immutable_references[index]##*@}"
  sha_references[index]="${remote_images[index]}:$RELEASE_SHA"
  version_references[index]="${remote_images[index]}:$RELEASE_VERSION"

  test "$(docker image inspect "${local_images[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  verify_labels "${local_images[$index]}"
  test "$(remote_digest "${sha_references[$index]}")" = "${expected_digests[$index]}"
  docker pull "${immutable_references[$index]}"
  test "$(docker image inspect "${immutable_references[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  verify_labels "${immutable_references[$index]}"

  if version_digests[index]="$(remote_digest "${version_references[index]}")"; then
    version_present[index]=1
    test "${version_digests[$index]}" = "${expected_digests[$index]}"
    docker pull "${version_references[$index]}"
    test "$(docker image inspect "${version_references[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
    verify_labels "${version_references[$index]}"
  else
    status=$?
    test "$status" -eq 1 || exit "$status"
  fi
done

for index in "${!roles[@]}"; do
  if [ "${version_present[$index]}" -eq 0 ]; then
    docker tag "${immutable_references[$index]}" "${version_references[$index]}"
    docker push "${version_references[$index]}"
  fi
done

# Re-read both aliases and re-pull the promoted version. This closes tag races
# and proves that each mutable alias still resolves to the accepted image ID.
: > build/release-evidence/docker-image-promotion.txt
for index in "${!roles[@]}"; do
  test "$(remote_digest "${sha_references[$index]}")" = "${expected_digests[$index]}"
  test "$(remote_digest "${version_references[$index]}")" = "${expected_digests[$index]}"
  docker image rm "${version_references[$index]}" >/dev/null 2>&1 || true
  docker pull "${version_references[$index]}"
  test "$(docker image inspect "${version_references[$index]}" --format '{{.Id}}')" = "${candidate_ids[$index]}"
  verify_labels "${version_references[$index]}"
  test "$(remote_digest "${sha_references[$index]}")" = "${expected_digests[$index]}"
  test "$(remote_digest "${version_references[$index]}")" = "${expected_digests[$index]}"
  printf '%s %s %s\n' "${roles[$index]}" "${sha_references[$index]}" "${version_references[$index]}" \
    >> build/release-evidence/docker-image-promotion.txt
done

echo "Validated commit-SHA images were promoted to ${RELEASE_VERSION} aliases."
