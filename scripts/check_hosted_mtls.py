#!/usr/bin/env python3
"""Prove API and worker resolve the same opaque mTLS execution-context refs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smart_commissioning_core.execution_context import resolve_context_parameters
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import RunLeaseV1

REFERENCES = {
    "ca_certificate": "secret://release-mtls-ca-certificate",
    "client_certificate": "secret://release-mtls-client-certificate",
    "private_key": "secret://release-mtls-private-key",
}
CONTRACT_FILE = ".release-mtls-contract.json"


def build_context() -> RunContextV1:
    return RunContextV1(
        project_id="release-project",
        site_id="release-site",
        configuration_snapshot={},
        configuration_version="release-mtls-v1",
        engine_parameters={
            "broker_host": "mqtt-acceptance",
            "broker_port": 1883,
            "use_tls": True,
            "mqtt_ca_certificate": REFERENCES["ca_certificate"],
            "mqtt_client_certificate": REFERENCES["client_certificate"],
            "mqtt_private_key": REFERENCES["private_key"],
        },
        requesting_principal="release-gate",
        application_version="0.1.28",
    )


def build_lease(context: RunContextV1, *, owner_token: str) -> RunLeaseV1:
    now = datetime.now(UTC)
    return RunLeaseV1(
        run_id="release-mtls-resolution",
        dispatch_id="release-mtls-dispatch",
        owner_token=owner_token,
        attempt=1,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=5),
        context_sha256=context.sha256(),
    )


def verify_resolution(
    context: RunContextV1,
    lease: RunLeaseV1,
    *,
    digests: dict[str, str],
    resolver: Callable[[str], bytes | None],
    deployment_id: str,
) -> None:
    resolved_references: set[str] = set()

    def guarded_resolver(reference: str) -> bytes:
        material = resolver(reference)
        if material is None:
            raise AssertionError(f"opaque mTLS reference was unavailable: {reference}")
        if hashlib.sha256(material).hexdigest() != digests.get(reference):
            raise AssertionError(f"opaque mTLS reference digest changed: {reference}")
        resolved_references.add(reference)
        return material

    parameters = resolve_context_parameters(
        context,
        lease,
        deployment_id=deployment_id,
        channel="mqtt_discovery",
        secret_resolver=guarded_resolver,
    )
    if resolved_references != set(REFERENCES.values()):
        raise AssertionError("execution-context resolution did not read every mTLS reference")
    for field, reference in REFERENCES.items():
        if parameters.get(field) != reference:
            raise AssertionError(f"execution context did not preserve opaque {field} reference")


def api_phase(root: Path, deployment_id: str) -> None:
    from app.services.configuration_service import (  # noqa: PLC0415
        read_secret_material,
        write_secret_material,
    )

    digests: dict[str, str] = {}
    for field, reference in REFERENCES.items():
        material = f"{field}:{secrets.token_urlsafe(72)}"
        write_secret_material(reference, material)
        digests[reference] = hashlib.sha256(material.encode("utf-8")).hexdigest()
    context = build_context()
    lease = build_lease(context, owner_token="release-api-owner")
    verify_resolution(
        context,
        lease,
        digests=digests,
        resolver=lambda reference: read_secret_material(reference).encode("utf-8"),
        deployment_id=deployment_id,
    )
    contract = {
        "context": context.model_dump(mode="json"),
        "lease": lease.model_dump(mode="json"),
        "references": REFERENCES,
        "digests": digests,
    }
    (root / CONTRACT_FILE).write_text(json.dumps(contract), encoding="utf-8")


def worker_phase(root: Path, deployment_id: str) -> None:
    from app.mqtt_config_provider import _resolve_secret  # noqa: PLC0415

    contract = json.loads((root / CONTRACT_FILE).read_text(encoding="utf-8"))
    if contract.get("references") != REFERENCES:
        raise AssertionError("worker received a different set of opaque mTLS references")
    context = RunContextV1.model_validate(contract["context"])
    lease = RunLeaseV1.model_validate(contract["lease"])
    verify_resolution(
        context,
        lease,
        digests=contract["digests"],
        resolver=_resolve_secret,
        deployment_id=deployment_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("api", "worker"))
    args = parser.parse_args(argv)
    root = Path(os.environ["SMART_COMMISSIONING_SECRETS_ROOT"])
    deployment_id = os.environ["SMART_COMMISSIONING_DEPLOYMENT_ID"]
    if args.phase == "api":
        api_phase(root, deployment_id)
    else:
        worker_phase(root, deployment_id)
    print(f"mTLS execution-context resolution: OK ({args.phase})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
