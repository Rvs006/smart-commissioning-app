#!/usr/bin/env python3
"""Public-safe live hub acceptance for Sync v2 using a temporary edge database."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
from app.schemas.jobs import ReportRequest
from app.services.report_artifacts import (
    ARTIFACT_MANIFEST_SCHEMA_VERSION,
    REPORT_RENDERER_VERSION,
)
from app.services.report_artifacts import canonical_json_bytes as artifact_json_bytes
from app.services.run_service import RunService
from smart_commissioning_core import __version__ as APPLICATION_VERSION
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.integrity import SigningKey, sha256_bytes
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from smart_commissioning_core.sync_identity import EdgeIdentity
from smart_commissioning_core.sync_v2 import (
    BUNDLE_MEDIA_TYPE,
    SyncV2Error,
    build_sync_v2_bundle,
    canonical_json_bytes,
)

_NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_PROJECT = "sync-smoke-project"
_SITE = "sync-smoke-site"
_DENIED_PROJECT = "sync-smoke-denied-project"
_DENIED_SITE = "sync-smoke-denied-site"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub-base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--compose-file", action="append", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--hub-service", default="hub-api")
    return parser


def _compose_prefix(args: argparse.Namespace) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in args.compose_file:
        command.extend(("-f", compose_file))
    command.extend(("--env-file", args.env_file, "-p", args.compose_project))
    return command


def _provision_credential(
    args: argparse.Namespace,
    *,
    edge_id: str,
    signing_key_fingerprint: str,
) -> str:
    command = [
        *_compose_prefix(args),
        "exec",
        "-T",
        args.hub_service,
        "python",
        "-m",
        "app.scripts.sync_credentials",
        "--edge-id",
        edge_id,
        "--signing-key-fingerprint",
        signing_key_fingerprint,
        "--scope",
        _PROJECT,
        _SITE,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Hub sync-credential provisioning failed with exit {completed.returncode}."
        )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    raw_key = lines[-1] if lines else ""
    if len(raw_key) < 16:
        raise RuntimeError("Hub sync-credential provisioning returned no one-time key.")
    return raw_key


class EdgeFixture:
    def __init__(self, root: Path) -> None:
        database_root = root / "edge"
        database_root.mkdir()
        url = default_sqlite_url(database_root)
        upgrade_to_head(url)
        self.engine = create_engine_from_url(url)
        self.bundle_key = SigningKey.generate()
        self.artifact_key = SigningKey.generate()
        self.identity = EdgeIdentity(
            edge_id=f"edge-sync-smoke-{sha256_bytes(self.bundle_key.public_key_pem().encode())[:12]}",
            public_key_pem=self.bundle_key.public_key_pem(),
            public_key_fingerprint=self.bundle_key.public_key_fingerprint(),
        )
        self.artifacts: dict[str, bytes] = {}

    def close(self) -> None:
        self.engine.dispose()

    def report(self, project_id: str, site_id: str, marker: str) -> str:
        service = RunService(self.engine)
        with mock.patch("app.services.run_service.edge_identity", return_value=self.identity):
            run, _ = service.create_report_run(
                ReportRequest(
                    project_id=project_id,
                    site_id=site_id,
                    report_type="evidence_pack",
                    output_format="pdf",
                    source_run_ids=[],
                    report_title=f"Sync smoke {marker}",
                )
            )
        artifact = f"%PDF-1.4\npublic hosted sync smoke {marker}\n%%EOF\n".encode()
        unsigned = {
            "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
            "report_id": run.run_id,
            "snapshot_sha256": run.parameters["report_snapshot_sha256"],
            "file_name": f"sync-smoke-{marker}.pdf",
            "media_type": "application/pdf",
            "byte_size": len(artifact),
            "renderer_version": REPORT_RENDERER_VERSION,
            "evidence_set_id": run.parameters["evidence_set_id"],
            "artifact_sha256": sha256_bytes(artifact),
            "artifact_relpath": f"edge-{marker}.pdf",
            "origin": self.identity.edge_id,
            "signing_key_id": self.artifact_key.public_key_fingerprint(),
            "signed_at": run.parameters["report_generated_at"],
        }
        body = artifact_json_bytes(unsigned)
        manifest = {
            **unsigned,
            "signature_algorithm": "ed25519",
            "signature": base64.b64encode(self.artifact_key.sign(body)).decode(),
            "public_key_pem": self.artifact_key.public_key_pem(),
            "signed_manifest_sha256": sha256_bytes(body),
        }
        service.complete_report_run(run.run_id, manifest)
        self.artifacts[run.run_id] = artifact
        return run.run_id

    def bundle(self, run_ids: list[str]) -> bytes:
        return build_sync_v2_bundle(
            self.engine,
            run_ids=run_ids,
            signing_key=self.bundle_key,
            edge_identity=self.identity,
            created_at=_NOW,
            artifact_loader=lambda manifest: self.artifacts[str(manifest["report_id"])],
        )

    def prove_raw_secret_rejected(self, sentinel: str) -> None:
        lifecycle = RunLifecycleRepository(self.engine)
        context = RunContextV1.model_validate(
            {
                "project_id": _PROJECT,
                "site_id": _SITE,
                "configuration_snapshot": {"profile": {"version": 1}},
                "configuration_version": 1,
                "registers": [],
                "imports": [],
                "schema_versions": {},
                "engine_parameters": {},
                "connection_settings": {"private_key": "secret://smoke-key-v1"},
                "secret_references": {},
                "requesting_principal": "release-smoke",
                "application_version": APPLICATION_VERSION,
            }
        )
        envelope = lifecycle.create_run_with_context(
            job_type="mqtt_discovery",
            context=context,
            edge_id=self.identity.edge_id,
            now=_NOW,
        )
        lease = lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token="smoke-owner",
            now=_NOW,
        )
        if lease is None:
            raise RuntimeError("Secret-sentinel fixture could not claim its run.")
        outcome = lifecycle.finalize_run(
            envelope.run_id,
            "smoke-owner",
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary={"api_key": sentinel},
            ),
            now=_NOW,
        )
        if not outcome.applied:
            raise RuntimeError("Secret-sentinel fixture could not seal its run.")
        try:
            self.bundle([envelope.run_id])
        except SyncV2Error:
            return
        raise RuntimeError("Sync v2 accepted raw secret material into a bundle.")


def _read_bundle(bundle: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
    with ZipFile(BytesIO(bundle)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return json.loads(members.pop("manifest.json")), members


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            info = ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _conflict_bundle(bundle: bytes, key: SigningKey) -> bytes:
    manifest, members = _read_bundle(bundle)
    descriptors = manifest.get("items")
    if not isinstance(descriptors, list) or not descriptors:
        raise RuntimeError("Sync smoke bundle has no item descriptor.")
    descriptor = descriptors[0]
    if not isinstance(descriptor, dict):
        raise RuntimeError("Sync smoke item descriptor is malformed.")
    item_member = descriptor.get("item_member")
    if not isinstance(item_member, str):
        raise RuntimeError("Sync smoke item member is malformed.")
    item = json.loads(members[item_member])
    payload = dict(item["result"]["result_payload"])
    summary = dict(payload["summary"])
    summary["conflict_marker"] = "different-sealed-digest"
    payload["summary"] = summary
    terminal = TerminalResultV1.model_validate(payload)
    digest = terminal.sha256()
    item["result"]["summary"] = summary
    item["result"]["result_payload"] = terminal.model_dump(mode="json")
    item["result"]["result_sha256"] = digest
    item["seal"]["result_sha256"] = digest
    item["run"]["result_summary"] = summary
    descriptor["result_sha256"] = digest
    item_bytes = canonical_json_bytes(item)
    item_id = sha256_bytes(
        f"{descriptor['run_id']}\0{digest}".encode()
    )[:32]
    descriptor["item_id"] = item_id
    descriptor["item_member"] = f"items/{item_id}.json"
    descriptor["item_sha256"] = sha256_bytes(item_bytes)
    members.pop(item_member)
    members[descriptor["item_member"]] = item_bytes
    manifest["bundle_id"] = sha256_bytes(
        canonical_json_bytes(
            {
                name: value
                for name, value in manifest.items()
                if name not in {"bundle_id", "signature", "signed_manifest_sha256"}
            }
        )
    )
    body = canonical_json_bytes(
        {
            name: value
            for name, value in manifest.items()
            if name not in {"signature", "signed_manifest_sha256"}
        }
    )
    manifest["signed_manifest_sha256"] = sha256_bytes(body)
    manifest["signature"] = base64.b64encode(key.sign(body)).decode()
    return _zip_bytes(
        {**members, "manifest.json": json.dumps(manifest, indent=2, sort_keys=True).encode()}
    )


def _assert_receipts(
    response: httpx.Response,
    *,
    expected_bundle_id: str,
    expected_classes: list[str],
    expected_acknowledged_run_ids: list[str],
    expected_all_acknowledged: bool,
) -> dict[str, object]:
    response.raise_for_status()
    payload = response.json()
    if payload.get("protocol_version") != "2.0":
        raise RuntimeError("Hub receipt did not identify Sync protocol version 2.0.")
    if payload.get("bundle_id") != expected_bundle_id:
        raise RuntimeError("Hub receipt bundle identity does not match the upload.")
    receipts = payload.get("receipts")
    if not isinstance(receipts, list):
        raise RuntimeError("Hub returned no Sync v2 receipts.")
    classes = [str(receipt.get("class")) for receipt in receipts]
    if classes != expected_classes:
        raise RuntimeError(
            f"Hub returned receipt classes {classes!r}, expected {expected_classes!r}."
        )
    if payload.get("acknowledged_run_ids") != expected_acknowledged_run_ids:
        raise RuntimeError("Hub acknowledged the wrong Sync v2 run IDs.")
    if payload.get("all_acknowledged") is not expected_all_acknowledged:
        raise RuntimeError("Hub returned the wrong all_acknowledged state.")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sentinel = "public-sync-secret-sentinel-never-export"
    with tempfile.TemporaryDirectory(prefix="sync-v2-hosted-") as temp_dir:
        edge = EdgeFixture(Path(temp_dir))
        try:
            sync_key = _provision_credential(
                args,
                edge_id=edge.identity.edge_id,
                signing_key_fingerprint=edge.bundle_key.public_key_fingerprint(),
            )
            edge.prove_raw_secret_rejected(sentinel)
            hub = args.hub_base_url.rstrip("/")
            sync_headers = {"X-Sync-Key": sync_key}
            with httpx.Client(timeout=60.0) as client:
                capabilities = client.get(f"{hub}/hub/sync/capabilities", headers=sync_headers)
                capabilities.raise_for_status()
                if capabilities.json().get("preferred_protocol_version") != "2.0":
                    raise RuntimeError("Hub did not negotiate Sync v2.")

                primary = edge.report(_PROJECT, _SITE, "primary")
                primary_bundle = edge.bundle([primary])
                if sync_key.encode() in primary_bundle or sentinel.encode() in primary_bundle:
                    raise RuntimeError("Secret material crossed the Sync v2 request boundary.")
                first = client.post(
                    f"{hub}/hub/sync/v2/ingest",
                    headers={**sync_headers, "Content-Type": BUNDLE_MEDIA_TYPE},
                    content=primary_bundle,
                )
                primary_bundle_id = str(_read_bundle(primary_bundle)[0]["bundle_id"])
                _assert_receipts(
                    first,
                    expected_bundle_id=primary_bundle_id,
                    expected_classes=["accepted"],
                    expected_acknowledged_run_ids=[primary],
                    expected_all_acknowledged=True,
                )
                retry = client.post(
                    f"{hub}/hub/sync/v2/ingest",
                    headers={**sync_headers, "Content-Type": BUNDLE_MEDIA_TYPE},
                    content=primary_bundle,
                )
                _assert_receipts(
                    retry,
                    expected_bundle_id=primary_bundle_id,
                    expected_classes=["byte_identical"],
                    expected_acknowledged_run_ids=[primary],
                    expected_all_acknowledged=True,
                )
                if sync_key in retry.text or sentinel in retry.text:
                    raise RuntimeError("Secret material crossed the Sync v2 receipt boundary.")

                download_headers = {"X-API-Key": args.api_key}
                one = client.get(f"{hub}/reports/{primary}/download", headers=download_headers)
                two = client.get(f"{hub}/reports/{primary}/download", headers=download_headers)
                one.raise_for_status()
                two.raise_for_status()
                if one.content != edge.artifacts[primary] or one.content != two.content:
                    raise RuntimeError("Hub report download is not the exact edge artifact.")

                conflict_bundle = _conflict_bundle(primary_bundle, edge.bundle_key)
                conflict = client.post(
                    f"{hub}/hub/sync/v2/ingest",
                    headers={**sync_headers, "Content-Type": BUNDLE_MEDIA_TYPE},
                    content=conflict_bundle,
                )
                _assert_receipts(
                    conflict,
                    expected_bundle_id=str(_read_bundle(conflict_bundle)[0]["bundle_id"]),
                    expected_classes=["conflict"],
                    expected_acknowledged_run_ids=[],
                    expected_all_acknowledged=False,
                )

                denied_project = edge.report(_DENIED_PROJECT, _SITE, "denied-project")
                denied_site = edge.report(_PROJECT, _DENIED_SITE, "denied-site")
                for denied in (denied_project, denied_site):
                    denied_bundle = edge.bundle([denied])
                    response = client.post(
                        f"{hub}/hub/sync/v2/ingest",
                        headers={**sync_headers, "Content-Type": BUNDLE_MEDIA_TYPE},
                        content=denied_bundle,
                    )
                    _assert_receipts(
                        response,
                        expected_bundle_id=str(_read_bundle(denied_bundle)[0]["bundle_id"]),
                        expected_classes=["unauthorized"],
                        expected_acknowledged_run_ids=[],
                        expected_all_acknowledged=False,
                    )

                mixed_allowed = edge.report(_PROJECT, _SITE, "mixed-allowed")
                mixed_denied = edge.report(_DENIED_PROJECT, _SITE, "mixed-denied")
                mixed_bundle = edge.bundle([mixed_allowed, mixed_denied])
                mixed = client.post(
                    f"{hub}/hub/sync/v2/ingest",
                    headers={**sync_headers, "Content-Type": BUNDLE_MEDIA_TYPE},
                    content=mixed_bundle,
                )
                _assert_receipts(
                    mixed,
                    expected_bundle_id=str(_read_bundle(mixed_bundle)[0]["bundle_id"]),
                    expected_classes=["accepted", "unauthorized"],
                    expected_acknowledged_run_ids=[mixed_allowed],
                    expected_all_acknowledged=False,
                )
        finally:
            edge.close()

    print(
        json.dumps(
            {
                "sync_v2": "passed",
                "hosted_sync_v2_round_trip": "passed",
                "exact_artifact_bytes": "passed",
                "lost_response_retry": "passed",
                "immutable_conflict": "passed",
                "credential_scope": "passed",
                "mixed_receipts": "passed",
                "secret_boundary": "passed",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
