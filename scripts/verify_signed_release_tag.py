#!/usr/bin/env python3
"""Fail closed unless a GitHub release ref is a verified annotated commit tag."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+")
_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")


class ReleaseTagError(RuntimeError):
    """Raised when the GitHub tag cannot prove the release commit."""


def validate_release_tag(
    *,
    ref_payload: Mapping[str, Any],
    tag_payload: Mapping[str, Any],
    version: str,
    expected_commit: str,
) -> None:
    """Validate the ref indirection and signed annotated-tag object."""

    if ref_payload.get("ref") != f"refs/tags/{version}":
        raise ReleaseTagError("GitHub returned a different tag ref")
    ref_object = ref_payload.get("object")
    if not isinstance(ref_object, Mapping) or ref_object.get("type") != "tag":
        raise ReleaseTagError("release ref is not an annotated tag object")
    tag_sha = ref_object.get("sha")
    if not isinstance(tag_sha, str) or _COMMIT_RE.fullmatch(tag_sha) is None:
        raise ReleaseTagError("annotated tag object SHA is invalid")
    if tag_payload.get("sha") != tag_sha:
        raise ReleaseTagError("annotated tag response does not match the tag ref")
    if tag_payload.get("tag") != version:
        raise ReleaseTagError("annotated tag name does not match the release version")

    target = tag_payload.get("object")
    if not isinstance(target, Mapping) or target.get("type") != "commit":
        raise ReleaseTagError("annotated tag does not point directly to a commit")
    if target.get("sha") != expected_commit:
        raise ReleaseTagError("annotated tag commit does not match RELEASE_SHA")

    verification = tag_payload.get("verification")
    if not isinstance(verification, Mapping) or verification.get("verified") is not True:
        raise ReleaseTagError("annotated tag signature is not verified by GitHub")


def _get_json(url: str, *, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "smart-commissioning-release-gate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise ReleaseTagError(f"GitHub API request failed for {url}") from error
    if not isinstance(payload, dict):
        raise ReleaseTagError("GitHub API response is not an object")
    return payload


def verify_remote_release_tag(
    *,
    api_url: str,
    repository: str,
    version: str,
    expected_commit: str,
    token: str,
) -> None:
    """Fetch and validate an annotated tag from the Git database API."""

    encoded_version = urllib.parse.quote(version, safe="")
    base = api_url.rstrip("/")
    ref_payload = _get_json(
        f"{base}/repos/{repository}/git/ref/tags/{encoded_version}", token=token
    )
    ref_object = ref_payload.get("object")
    tag_sha = ref_object.get("sha") if isinstance(ref_object, Mapping) else ""
    if not isinstance(tag_sha, str) or _COMMIT_RE.fullmatch(tag_sha) is None:
        raise ReleaseTagError("release ref did not return a valid annotated tag SHA")
    tag_payload = _get_json(f"{base}/repos/{repository}/git/tags/{tag_sha}", token=token)
    validate_release_tag(
        ref_payload=ref_payload,
        tag_payload=tag_payload,
        version=version,
        expected_commit=expected_commit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    args = parser.parse_args(argv)

    if _REPOSITORY_RE.fullmatch(args.repository) is None:
        parser.error("--repository must be an owner/repository slug")
    if _VERSION_RE.fullmatch(args.version) is None:
        parser.error("--version must be an exact semantic version tag such as v0.1.28")
    if _COMMIT_RE.fullmatch(args.commit) is None:
        parser.error("--commit must be a full lowercase Git SHA")
    token = os.environ.get(args.token_env, "")
    if not token:
        parser.error(f"{args.token_env} is required")

    try:
        verify_remote_release_tag(
            api_url=args.api_url,
            repository=args.repository,
            version=args.version,
            expected_commit=args.commit,
            token=token,
        )
    except ReleaseTagError as error:
        parser.error(str(error))
    print(f"GitHub verified signed annotated tag {args.version} -> {args.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
