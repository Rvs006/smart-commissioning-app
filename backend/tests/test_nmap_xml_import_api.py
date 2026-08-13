"""Scoped operator Nmap XML import API contracts."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api.routes.nmap_xml_import import import_nmap_xml
from app.core.auth import AuthPrincipal
from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapCapabilityState,
    NmapProviderMode,
)
from app.services.nmap_capability_service import (
    NmapCapabilityDeniedError,
)
from app.services.nmap_xml_import_service import NmapXmlImportStorageError
from fastapi import HTTPException
from harness import ApiTestCase
from smart_commissioning_core.rbac import Role
from tests.test_nmap_xml_import_service import _VALID_XML


class _UnreadUpload:
    def __init__(self) -> None:
        self.read_calls = 0
        self.close_calls = 0

    async def read(self, _size: int = -1) -> bytes:
        self.read_calls += 1
        return b"<nmaprun/>"

    async def close(self) -> None:
        self.close_calls += 1


class _BlockedCapability:
    def __init__(self, reason: str = "external_deployment") -> None:
        self.calls = 0
        self.reason = reason

    def assert_xml_import_allowed(self, *, project_id: str, site_id: str):
        from app.schemas.nmap import (
            NmapCapabilityResponse,
            NmapCapabilityState,
        )

        self.calls += 1
        raise NmapCapabilityDeniedError(
            NmapCapabilityResponse(
                state=NmapCapabilityState.DISABLED,
                reason=self.reason,
            )
        )


class _AllowedCapability:
    def __init__(self) -> None:
        self.calls = 0
        self.response = NmapCapabilityResponse(
            state=NmapCapabilityState.XML_IMPORT_ONLY,
            reason="xml_import_allowed",
            provider_mode=NmapProviderMode.OPERATOR_XML_IMPORT,
            policy_id="xml-policy",
            policy_revision=4,
            xml_import_allowed=True,
        )

    def assert_xml_import_allowed(self, *, project_id: str, site_id: str):
        del project_id, site_id
        self.calls += 1
        return self.response


class _InconsistentCapability:
    def assert_xml_import_allowed(self, *, project_id: str, site_id: str):
        del project_id, site_id
        return NmapCapabilityResponse(
            state=NmapCapabilityState.DISABLED,
            reason="external_deployment",
            provider_mode=NmapProviderMode.DISABLED,
            xml_import_allowed=True,
        )


class _UnusedImporter:
    engine = object()

    def __init__(self) -> None:
        self.calls = 0

    def import_payload(self, **_kwargs):
        self.calls += 1
        raise AssertionError("blocked XML must not reach import or parsing")


class _StorageFailingImporter:
    engine = object()

    def import_payload(self, **_kwargs):
        raise NmapXmlImportStorageError(r"C:\private\operator-output.xml failed")


class NmapXmlImportRouteOrderingTests(unittest.TestCase):
    def test_runtime_feature_disable_rejects_the_mounted_route_dependency(self) -> None:
        from app.api.routes.nmap_xml_import import (
            get_nmap_xml_import_capability_service,
        )

        with (
            patch(
                "app.api.routes.nmap_xml_import.get_settings",
                return_value=SimpleNamespace(nmap_internal_provider_enabled=False),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            get_nmap_xml_import_capability_service(_BlockedCapability())

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "Nmap XML import is unavailable.")

    def test_blocked_modes_reject_before_upload_read_or_import(self) -> None:
        for reason in (
            "external_deployment",
            "policy_not_configured",
            "provider_disabled",
            "site_not_permitted",
        ):
            with self.subTest(reason=reason):
                upload = _UnreadUpload()
                capability = _BlockedCapability(reason)
                importer = _UnusedImporter()
                principal = AuthPrincipal(None, "local", Role.ADMIN, "local")

                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        import_nmap_xml(
                            project_id="project-a",
                            site_id="site-a",
                            upload=upload,
                            principal=principal,
                            capability=capability,
                            importer=importer,
                            scope_authorizer=lambda *_args, **_kwargs: None,
                        )
                    )

                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(
                    raised.exception.detail,
                    "Nmap XML import is unavailable.",
                )
                self.assertEqual(capability.calls, 1)
                self.assertEqual(upload.read_calls, 0)
                self.assertEqual(upload.close_calls, 0)
                self.assertEqual(importer.calls, 0)

    def test_inconsistent_capability_rejects_before_upload_read_or_import(self) -> None:
        upload = _UnreadUpload()
        importer = _UnusedImporter()
        principal = AuthPrincipal(None, "local", Role.ADMIN, "local")

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                import_nmap_xml(
                    project_id="project-a",
                    site_id="site-a",
                    upload=upload,
                    principal=principal,
                    capability=_InconsistentCapability(),
                    importer=importer,
                    scope_authorizer=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "Nmap XML import is unavailable.")
        self.assertEqual(upload.read_calls, 0)
        self.assertEqual(upload.close_calls, 0)
        self.assertEqual(importer.calls, 0)

    def test_storage_failure_returns_only_a_sanitized_conflict(self) -> None:
        upload = _UnreadUpload()
        principal = AuthPrincipal(None, "local", Role.ADMIN, "local")

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                import_nmap_xml(
                    project_id="project-a",
                    site_id="site-a",
                    upload=upload,
                    principal=principal,
                    capability=_AllowedCapability(),
                    importer=_StorageFailingImporter(),
                    scope_authorizer=lambda *_args, **_kwargs: None,
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            "Nmap XML import could not be completed.",
        )
        self.assertNotIn("operator-output.xml", raised.exception.detail)
        self.assertNotIn(r"C:\private", raised.exception.detail)
        self.assertEqual(upload.read_calls, 1)
        self.assertEqual(upload.close_calls, 1)


class NmapXmlImportApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": "nmap-xml-import-root",
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
        "SMART_COMMISSIONING_DEPLOYMENT_ID": "nmap-xml-import-tests",
        "SMART_COMMISSIONING_NETWORK_EXECUTOR_ID": "xml-import-executor",
    }
    client_headers = {"X-API-Key": "nmap-xml-import-root"}

    def setUp(self) -> None:
        from app.api.routes.nmap_xml_import import (
            get_nmap_xml_import_capability_service,
            get_nmap_xml_import_service,
        )
        from app.core.db import get_engine
        from app.services.nmap_xml_import_service import NmapXmlImportService
        from app.services.raw_evidence_artifacts import RawEvidenceArtifactStore
        from app.services.run_service import RunService
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Project, Site

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.capability = _AllowedCapability()
        engine = get_engine()
        self.store = RawEvidenceArtifactStore(
            engine,
            root=Path(self.temporary_directory.name) / "raw-evidence",
        )
        self.importer = NmapXmlImportService(
            engine,
            raw_store=self.store,
            run_service=RunService(engine),
            producer_executor_id="inline:xml-import-executor",
        )
        self.app.dependency_overrides[get_nmap_xml_import_capability_service] = lambda: self.capability
        self.app.dependency_overrides[get_nmap_xml_import_service] = lambda: self.importer

        suffix = uuid.uuid4().hex[:10]
        self.project_id = f"xml-project-{suffix}"
        self.site_id = f"xml-site-{suffix}"
        self.foreign_project_id = f"xml-foreign-project-{suffix}"
        self.foreign_site_id = f"xml-foreign-site-{suffix}"
        with session_factory(engine).begin() as session:
            session.add_all(
                [
                    Project(id=self.project_id, name="XML import project"),
                    Site(
                        id=self.site_id,
                        project_id=self.project_id,
                        name="XML import site",
                    ),
                    Project(
                        id=self.foreign_project_id,
                        name="Foreign XML project",
                    ),
                    Site(
                        id=self.foreign_site_id,
                        project_id=self.foreign_project_id,
                        name="Foreign XML site",
                    ),
                ]
            )

        self.engineer_headers = self._create_scoped_user("engineer", suffix)
        self.viewer_headers = self._create_scoped_user("viewer", suffix)

    def tearDown(self) -> None:
        from app.api.routes.nmap_xml_import import (
            get_nmap_xml_import_capability_service,
            get_nmap_xml_import_service,
        )

        self.app.dependency_overrides.pop(
            get_nmap_xml_import_capability_service,
            None,
        )
        self.app.dependency_overrides.pop(get_nmap_xml_import_service, None)
        self.temporary_directory.cleanup()

    def _create_scoped_user(self, role: str, suffix: str) -> dict[str, str]:
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"xml-{role}-{suffix}", "role": role},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        granted = self.client.post(
            f"/api/v1/users/{body['user']['id']}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Nmap XML import API test",
            },
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        return {"X-API-Key": body["api_key"]}

    def _post(
        self,
        *,
        headers: dict[str, str],
        project_id: str | None = None,
        site_id: str | None = None,
    ):
        return self.client.post(
            "/api/v1/nmap/xml-import",
            params={
                "project_id": project_id or self.project_id,
                "site_id": site_id or self.site_id,
            },
            files={"upload": ("operator-output.xml", _VALID_XML, "application/xml")},
            headers=headers,
        )

    def test_engineer_import_returns_path_free_sealed_result(self) -> None:
        response = self._post(headers=self.engineer_headers)

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["diagnostic_code"], "import_complete")
        self.assertEqual(body["nmap_version"], "7.95")
        self.assertEqual(body["host_count"], 1)
        self.assertEqual(body["port_count"], 2)
        self.assertNotIn("operator-output.xml", response.text)
        self.assertNotIn("<nmaprun", response.text)
        self.assertNotIn(str(self.store.root), response.text)

        run = self.client.get(
            f"/api/v1/discovery/runs/{body['run_id']}",
            headers=self.engineer_headers,
        )
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["status"], "succeeded")
        self.assertNotIn("<nmaprun", run.text)
        observations = self.client.get(
            f"/api/v1/discovery/runs/{body['run_id']}/observations",
            headers=self.engineer_headers,
        )
        self.assertEqual(observations.status_code, 200, observations.text)
        self.assertEqual(len(observations.json()["observations"]), 3)
        self.assertNotIn("Test Controller", observations.text)

    def test_viewer_is_forbidden_and_foreign_or_absent_scope_is_concealed(self) -> None:
        viewer = self._post(headers=self.viewer_headers)
        foreign = self._post(
            headers=self.engineer_headers,
            project_id=self.foreign_project_id,
            site_id=self.foreign_site_id,
        )
        absent = self._post(
            headers=self.engineer_headers,
            project_id="missing-project",
            site_id="missing-site",
        )

        self.assertEqual(viewer.status_code, 403, viewer.text)
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(absent.status_code, 404, absent.text)
        self.assertEqual(foreign.json(), absent.json())


if __name__ == "__main__":
    unittest.main()
