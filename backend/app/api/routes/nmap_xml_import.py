"""Scoped, internal-only import of protected Nmap XML evidence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from smart_commissioning_core.rbac import Role

from app.api.routes.nmap import (
    get_nmap_capability_service,
    get_nmap_installation_probe,
)
from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.config import get_settings
from app.core.scopes import require_project_site_access
from app.schemas.nmap import NmapCapabilityState, NmapProviderMode
from app.schemas.nmap_xml_import import NmapXmlImportResponse
from app.services.nmap_capability_service import (
    NmapCapabilityDeniedError,
    NmapCapabilityService,
    NmapInstallationProbe,
)
from app.services.nmap_xml_import_service import (
    NMAP_XML_IMPORT_CAPTURE_BYTES,
    NMAP_XML_IMPORT_MAX_BYTES,
    NmapXmlImportLifecycleError,
    NmapXmlImportService,
)

router = APIRouter()
require_engineer = require_role(Role.ENGINEER)


class _XmlImporter(Protocol):
    engine: object

    def import_payload(self, **kwargs: object) -> NmapXmlImportResponse: ...


ScopeAuthorizer = Callable[..., object]


def get_nmap_xml_import_capability_service(
    probe: NmapInstallationProbe = Depends(get_nmap_installation_probe),
) -> NmapCapabilityService:
    """Reuse the executor-bound Nmap capability authority."""

    if not get_settings().nmap_internal_provider_enabled:
        raise HTTPException(
            status_code=409,
            detail="Nmap XML import is unavailable.",
        )
    return get_nmap_capability_service(probe)


def get_nmap_xml_import_service() -> _XmlImporter:
    """Construct the synchronous importer only after the capability gate."""

    return NmapXmlImportService()


def get_xml_import_scope_authorizer() -> ScopeAuthorizer:
    return require_project_site_access


@router.post(
    "/xml-import",
    response_model=NmapXmlImportResponse,
    dependencies=[Depends(require_engineer)],
    status_code=201,
)
async def import_nmap_xml(
    project_id: str = Query(min_length=1, max_length=255),
    site_id: str = Query(min_length=1, max_length=255),
    upload: UploadFile = File(...),
    principal: AuthPrincipal = Depends(get_principal),
    capability: NmapCapabilityService = Depends(get_nmap_xml_import_capability_service),
    importer: _XmlImporter = Depends(get_nmap_xml_import_service),
    scope_authorizer: ScopeAuthorizer = Depends(get_xml_import_scope_authorizer),
) -> NmapXmlImportResponse:
    """Gate authority before reading, storing, or parsing the upload."""

    scope_authorizer(
        principal,
        project_id,
        site_id,
        engine=importer.engine,
    )
    try:
        approved = capability.assert_xml_import_allowed(
            project_id=project_id,
            site_id=site_id,
        )
    except NmapCapabilityDeniedError as error:
        raise HTTPException(
            status_code=409,
            detail="Nmap XML import is unavailable.",
        ) from error
    if (
        approved.state is not NmapCapabilityState.XML_IMPORT_ONLY
        or approved.provider_mode is not NmapProviderMode.OPERATOR_XML_IMPORT
        or not approved.xml_import_allowed
    ):
        raise HTTPException(
            status_code=409,
            detail="Nmap XML import is unavailable.",
        )

    try:
        payload = await upload.read(NMAP_XML_IMPORT_CAPTURE_BYTES)
    finally:
        await upload.close()
    try:
        return importer.import_payload(
            project_id=project_id,
            site_id=site_id,
            principal=principal,
            capability=approved,
            payload=payload,
            capture_complete=len(payload) <= NMAP_XML_IMPORT_MAX_BYTES,
        )
    except NmapXmlImportLifecycleError as error:
        raise HTTPException(
            status_code=409,
            detail="Nmap XML import could not be completed.",
        ) from error


__all__ = [
    "NMAP_XML_IMPORT_MAX_BYTES",
    "get_nmap_xml_import_capability_service",
    "get_nmap_xml_import_service",
    "import_nmap_xml",
    "router",
]
