#!/usr/bin/env python3
"""Verify that Docker inspect describes one concrete local release image."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

ALLOWED_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def validate_local_image_inspect(
    document: Any,
    *,
    reference: str,
) -> dict[str, str]:
    """Fail closed on indexes while supporting engines that omit Descriptor."""

    if not isinstance(document, list) or len(document) != 1:
        raise ValueError(f"{reference} did not resolve to exactly one local image.")
    image = document[0]
    if not isinstance(image, dict):
        raise ValueError(f"{reference} returned a malformed image-inspect record.")

    image_id = image.get("Id")
    operating_system = image.get("Os")
    architecture = image.get("Architecture")
    if not isinstance(image_id, str) or _SHA256.fullmatch(image_id) is None:
        raise ValueError(f"{reference} has no canonical local image ID.")
    if operating_system != "linux" or not isinstance(architecture, str) or not architecture:
        raise ValueError(f"{reference} has no concrete Linux platform identity.")

    descriptor = image.get("Descriptor")
    if descriptor is not None:
        if not isinstance(descriptor, dict):
            raise ValueError(f"{reference} has a malformed image descriptor.")
        media_type = descriptor.get("mediaType")
        if media_type not in ALLOWED_MANIFEST_MEDIA_TYPES:
            raise ValueError(
                f"{reference} is not a single image manifest: {media_type!r}."
            )
    else:
        # Older Docker engines omit Descriptor from `docker image inspect`.
        # A loaded image record with one ID, one platform, and concrete RootFS
        # layer diff IDs is the engine's single-platform representation. The
        # publisher separately checks the pushed registry mediaType by digest.
        rootfs = image.get("RootFS")
        layers = rootfs.get("Layers") if isinstance(rootfs, dict) else None
        if (
            not isinstance(rootfs, dict)
            or rootfs.get("Type") != "layers"
            or not isinstance(layers, list)
            or not layers
            or any(not isinstance(layer, str) or _SHA256.fullmatch(layer) is None for layer in layers)
        ):
            raise ValueError(
                f"{reference} lacks both a supported descriptor and a concrete RootFS."
            )
        media_type = "docker-engine-single-platform"

    return {
        "reference": reference,
        "image_id": image_id,
        "platform": f"{operating_system}/{architecture}",
        "media_type": media_type,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    args = parser.parse_args()
    try:
        result = validate_local_image_inspect(
            json.load(sys.stdin),
            reference=args.reference,
        )
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
