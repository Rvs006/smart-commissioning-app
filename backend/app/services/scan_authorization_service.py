"""Application service for sealed-preview scan authorizations."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime

from smart_commissioning_core.db.run_lifecycle import (
    RunLifecycleRepository,
    ScanAuthorizationError,
)
from smart_commissioning_core.run_lifecycle import ScanAuthorizationV1

from app.services.run_service import RunService


class ScanAuthorizationService:
    """Create approvals and recover the exact inputs from their sealed preview."""

    def __init__(self, run_service: RunService | None = None) -> None:
        self._runs = run_service or RunService()
        self._lifecycle = RunLifecycleRepository(self._runs.engine)

    def create(
        self,
        *,
        preview_run_id: str,
        approved_by: str,
        ticket: str,
        purpose: str,
        not_before: datetime,
        not_after: datetime,
    ) -> ScanAuthorizationV1:
        # Preserve the API's ordinary missing-resource contract.  The lifecycle
        # repository intentionally reports every incomplete preview as an
        # authorization error because it cannot infer caller visibility.
        self._runs.get_run(preview_run_id)
        return self._lifecycle.create_scan_authorization(
            preview_run_id=preview_run_id,
            approved_by=approved_by,
            ticket=ticket,
            purpose=purpose,
            not_before=not_before,
            not_after=not_after,
        )

    def get(self, authorization_id: str) -> ScanAuthorizationV1:
        return self._lifecycle.get_scan_authorization(authorization_id)

    def list(
        self,
        *,
        project_id: str | None = None,
        site_id: str | None = None,
        preview_run_id: str | None = None,
    ) -> list[ScanAuthorizationV1]:
        return self._lifecycle.list_scan_authorizations(
            project_id=project_id,
            site_id=site_id,
            preview_run_id=preview_run_id,
        )

    def revoke(
        self,
        authorization_id: str,
        *,
        revoked_by: str,
        reason: str,
    ) -> ScanAuthorizationV1:
        return self._lifecycle.revoke_scan_authorization(
            authorization_id,
            revoked_by=revoked_by,
            reason=reason,
        )

    def prepare_live_parameters(
        self,
        *,
        preview_run_id: str,
        authorization_id: str,
        project_id: str,
        site_id: str,
        job_type: str,
    ) -> dict[str, object]:
        """Return a live envelope copied from one exact sealed dry preview.

        The lifecycle transaction remains the final authority and consumes the
        approval only while it creates the child run, link, outbox, and resource
        slots.  This read prepares the engine inputs without trusting a second
        client-supplied parameter packet.
        """

        authorization = self._lifecycle.get_scan_authorization(authorization_id)
        if authorization.preview_run_id != preview_run_id:
            raise ScanAuthorizationError(
                "scan authorization does not belong to the selected sealed preview"
            )
        if (
            authorization.project_id != project_id
            or authorization.site_id != site_id
        ):
            raise ScanAuthorizationError(
                "scan authorization does not belong to the requested project and site"
            )

        preview = self._runs.get_run(preview_run_id)
        if (
            preview.project_id != project_id
            or preview.site_id != site_id
            or preview.job_type != job_type
        ):
            raise ScanAuthorizationError(
                "sealed preview does not belong to the requested job and scope"
            )
        stored = self._runs.get_execution_context(preview_run_id)
        parameters = copy.deepcopy(dict(stored.context.engine_parameters))
        if not _truthy(parameters.get("dry_run")):
            raise ScanAuthorizationError("authorization source run is not a dry preview")
        contract = parameters.get("scan_contract_v1")
        if not isinstance(contract, Mapping):
            raise ScanAuthorizationError("sealed preview is missing scan_contract_v1")
        packet_digest = str(contract.get("packet_plan_sha256") or "").strip().lower()
        if packet_digest != authorization.packet_plan_sha256:
            raise ScanAuthorizationError(
                "sealed preview packet plan does not match the scan authorization"
            )

        parameters["dry_run"] = False
        parameters.pop("authorized", None)
        parameters.pop("scan_authorization", None)
        # A preview may explicitly select simulation.  Live BACnet execution
        # must resolve the real backend and cannot inherit that dry-only switch.
        if job_type == "bacnet_discovery":
            parameters.pop("bacnet_backend", None)
        parameters["scan_authorization"] = {
            "authorized": True,
            "authorized_by": authorization.approved_by,
            "authorization_id": authorization.authorization_id,
            "preview_run_id": authorization.preview_run_id,
            "ticket": authorization.ticket,
            "purpose": authorization.purpose,
            "not_before": authorization.not_before.isoformat(),
            "not_after": authorization.not_after.isoformat(),
        }
        return parameters


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)
