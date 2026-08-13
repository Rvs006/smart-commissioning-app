"""Protected Nmap XML import service contracts."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.auth import AuthPrincipal
from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapCapabilityState,
    NmapProviderMode,
)
from app.services.nmap_xml_import_service import (
    NmapXmlImportLifecycleError,
    NmapXmlImportService,
)
from app.services.raw_evidence_artifacts import (
    RawEvidenceArtifactStore,
    RawEvidenceError,
)
from app.services.run_service import RunService
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.rbac import Role

_VALID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">
  <host>
    <status state="up" reason="syn-ack"/>
    <address addr="192.0.2.18" addrtype="ipv4"/>
    <hostnames><hostname name="ahu-18.example"/></hostnames>
    <ports>
      <port protocol="tcp" portid="443">
        <state state="open" reason="syn-ack"/>
        <service name="https" product="Test Controller" version="1.2"/>
      </port>
      <port protocol="udp" portid="47808">
        <state state="open|filtered" reason="no-response"/>
      </port>
    </ports>
  </host>
  <runstats>
    <finished exit="success" elapsed="0.42"/>
    <hosts up="1" down="0" total="1"/>
  </runstats>
</nmaprun>
"""

_VERSION_WITHOUT_SERVICE_XML = b"""<?xml version="1.0"?>
<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">
  <host>
    <status state="up" reason="syn-ack"/>
    <address addr="192.0.2.19" addrtype="ipv4"/>
    <ports>
      <port protocol="tcp" portid="502">
        <state state="open" reason="syn-ack"/>
        <service version="3.1"/>
      </port>
    </ports>
  </host>
  <runstats>
    <finished exit="success" elapsed="0.12"/>
    <hosts up="1" down="0" total="1"/>
  </runstats>
</nmaprun>
"""

_DUPLICATE_PORT_XML = b"""<?xml version="1.0"?>
<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">
  <host>
    <status state="up" reason="syn-ack"/>
    <address addr="192.0.2.20" addrtype="ipv4"/>
    <ports>
      <port protocol="tcp" portid="443">
        <state state="open" reason="syn-ack"/>
        <service name="https"/>
      </port>
      <port protocol="tcp" portid="443">
        <state state="closed" reason="reset"/>
      </port>
    </ports>
  </host>
  <runstats>
    <finished exit="success" elapsed="0.12"/>
    <hosts up="1" down="0" total="1"/>
  </runstats>
</nmaprun>
"""

_UNCERTAIN_STATES_XML = b"""<?xml version="1.0"?>
<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">
  <host>
    <status state="up" reason="echo-reply"/>
    <address addr="192.0.2.21" addrtype="ipv4"/>
    <ports>
      <port protocol="udp" portid="161">
        <state state="filtered" reason="admin-prohibited"/>
      </port>
    </ports>
  </host>
  <host>
    <status state="down" reason="no-response"/>
    <address addr="192.0.2.22" addrtype="ipv4"/>
  </host>
  <runstats>
    <finished exit="success" elapsed="0.50"/>
    <hosts up="1" down="1" total="2"/>
  </runstats>
</nmaprun>
"""


class _FailingRawStore:
    def __init__(self) -> None:
        self.run_id: str | None = None

    def import_bytes(self, *, run_id: str, **_kwargs):
        self.run_id = run_id
        raise RawEvidenceError(r"C:\private\operator-output.xml could not be stored")

    def read_for_normalization(self, **_kwargs):
        raise AssertionError("unstored evidence reached normalization")


class _TestFilesystemSecurity:
    def secure_directory(self, _path: Path) -> None:
        return None

    def secure_file(self, _path: Path) -> None:
        return None

    def is_reparse_point(self, _path: Path) -> bool:
        return False


class _SlowRawStore:
    def __init__(
        self,
        delegate: RawEvidenceArtifactStore,
        renewed: threading.Event,
    ) -> None:
        self.delegate = delegate
        self.renewed = renewed

    def import_bytes(self, **kwargs):
        if not self.renewed.wait(timeout=2):
            raise AssertionError("slow storage did not observe a lease renewal")
        return self.delegate.import_bytes(**kwargs)

    def read_for_normalization(self, **kwargs):
        return self.delegate.read_for_normalization(**kwargs)


class _InjectedRenewingHeartbeat:
    def __init__(
        self,
        owned,
        renewed: threading.Event,
        renewals: list[bool],
        **kwargs,
    ) -> None:
        self.owned = owned
        self.renewed = renewed
        self.renewals = renewals
        self.lease_seconds = int(kwargs["lease_seconds"])
        self.stopped = False
        self.thread: threading.Thread | None = None

    @property
    def ownership_lost(self) -> bool:
        return False

    def start(self) -> None:
        def renew_once() -> None:
            self.renewals.append(self.owned.heartbeat(lease_seconds=self.lease_seconds))
            self.renewed.set()

        self.thread = threading.Thread(target=renew_once, daemon=True)
        self.thread.start()

    def stop_and_join(self) -> bool:
        self.stopped = True
        if self.thread is not None:
            self.thread.join(timeout=2)
        return self.thread is None or not self.thread.is_alive()


class _StartFailingHeartbeat:
    def __init__(self, owned, **_kwargs) -> None:
        self.owned = owned
        self.stopped = False

    def start(self) -> None:
        raise OSError(r"C:\private\heartbeat.config could not start")

    def stop_and_join(self) -> bool:
        self.stopped = True
        return True


class NmapXmlImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.engine = create_engine_from_url(f"sqlite:///{(root / 'xml-import.db').as_posix()}")
        Base.metadata.create_all(self.engine)
        self.store = RawEvidenceArtifactStore(
            self.engine,
            root=root / "raw-evidence",
            security=_TestFilesystemSecurity(),
        )
        self.run_service = RunService(self.engine)
        self.importer = NmapXmlImportService(
            self.engine,
            raw_store=self.store,
            run_service=self.run_service,
            producer_executor_id="inline:test-executor",
        )
        self.capability = NmapCapabilityResponse(
            state=NmapCapabilityState.XML_IMPORT_ONLY,
            reason="xml_import_allowed",
            provider_mode=NmapProviderMode.OPERATOR_XML_IMPORT,
            policy_id="policy-1",
            policy_revision=3,
            xml_import_allowed=True,
        )
        self.principal = AuthPrincipal(None, "local", Role.ADMIN, "local")

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_complete_xml_is_stored_then_normalized_and_terminally_sealed(self) -> None:
        result = self.importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=_VALID_XML,
            capture_complete=True,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.diagnostic_code, "import_complete")
        self.assertEqual(result.nmap_version, "7.95")
        self.assertEqual(result.host_count, 1)
        self.assertEqual(result.port_count, 2)
        self.assertTrue(result.artifact_id.startswith("artifact_"))

        descriptor, raw = self.store.read_for_normalization(
            run_id=result.run_id,
            artifact_id=result.artifact_id,
        )
        self.assertEqual(raw, _VALID_XML)
        self.assertEqual(descriptor.project_id, "project-import")
        self.assertEqual(descriptor.site_id, "site-import")

        run = self.run_service.get_run(result.run_id)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.stage, "operator_xml_import_complete")
        terminal = self.run_service.get_verified_terminal_result(result.run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.status, "succeeded")
        self.assertEqual(terminal.summary["xml_artifact_id"], result.artifact_id)
        self.assertIn("observation_evidence_v1", terminal.summary)
        self.assertEqual(len(terminal.devices), 1)
        self.assertEqual(terminal.devices[0]["address"], "192.0.2.18")

        observations = RunLifecycleRepository(self.engine).list_discovery_observations(
            result.run_id,
            1,
            project_id="project-import",
            site_id="site-import",
        )
        self.assertEqual(
            [(item.entity_kind, item.entity_key) for item in observations],
            [
                ("host", "host:192.0.2.18"),
                ("port", "port:192.0.2.18:443:tcp"),
                ("port", "port:192.0.2.18:47808:udp"),
            ],
        )
        self.assertEqual(
            observations[1].payload["ip_v1"]["detected_service"],
            "https",
        )
        self.assertEqual(observations[1].payload["ip_v1"]["detected_version"], "1.2")
        self.assertEqual(observations[0].payload["state"], "up")
        self.assertIsNone(observations[0].payload["ip_v1"]["probe_outcome"])
        self.assertEqual(observations[0].payload["ip_v1"]["attempts"], 0)
        self.assertEqual(observations[1].payload["state"], "open")
        self.assertIsNone(observations[1].payload["ip_v1"]["probe_outcome"])
        self.assertEqual(observations[1].payload["ip_v1"]["attempts"], 0)
        self.assertEqual(observations[2].payload["state"], "open|filtered")
        self.assertIsNone(observations[2].payload["ip_v1"]["probe_outcome"])
        self.assertEqual(observations[2].payload["ip_v1"]["attempts"], 0)
        serialized = result.model_dump_json() + terminal.model_dump_json()
        self.assertNotIn("<nmaprun", serialized)
        self.assertNotIn(str(self.store.root), serialized)
        self.assertNotIn("Test Controller", serialized)

    def test_unsafe_or_malformed_xml_is_retained_but_cannot_create_claims(self) -> None:
        hostile_payloads = {
            "doctype": b"""<?xml version="1.0"?>
<!DOCTYPE nmaprun [<!ENTITY secret SYSTEM "file:///private/secret">]>
<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">
  <runstats><finished exit="success"/><hosts up="0" down="0" total="0"/></runstats>
</nmaprun>""",
            "xinclude": b"""<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05"
 xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///etc/passwd" parse="text"/>
 <runstats><finished exit="success"/><hosts up="0" down="0" total="0"/></runstats></nmaprun>""",
            "truncated": b'<nmaprun scanner="nmap" version="7.95" xmloutputversion="1.05">',
            "invalid_utf8": b'<?xml version="1.0"?><nmaprun>\xff</nmaprun>',
        }

        for label, payload in hostile_payloads.items():
            with self.subTest(label=label):
                result = self.importer.import_payload(
                    project_id="project-import",
                    site_id="site-import",
                    principal=self.principal,
                    capability=self.capability,
                    payload=payload,
                    capture_complete=True,
                )

                self.assertEqual(result.status, "failed")
                self.assertTrue(result.diagnostic_code.startswith("xml_"))
                descriptor, retained = self.store.read_verified(
                    run_id=result.run_id,
                    artifact_id=result.artifact_id,
                )
                self.assertEqual(retained, payload)
                self.assertTrue(descriptor.capture_complete)
                terminal = self.run_service.get_verified_terminal_result(result.run_id)
                self.assertIsNotNone(terminal)
                assert terminal is not None
                self.assertEqual(terminal.status, "failed")
                self.assertEqual(terminal.stage, "operator_xml_import_rejected")
                self.assertEqual(terminal.devices, ())
                observations = RunLifecycleRepository(self.engine).list_discovery_observations(
                    result.run_id,
                    1,
                    project_id="project-import",
                    site_id="site-import",
                )
                self.assertEqual(observations, [])
                public = result.model_dump_json() + terminal.model_dump_json()
                self.assertNotIn("DOCTYPE", public)
                self.assertNotIn("passwd", public)
                self.assertNotIn(str(self.store.root), public)

    def test_version_without_service_is_not_published_as_an_invalid_claim(self) -> None:
        result = self.importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=_VERSION_WITHOUT_SERVICE_XML,
            capture_complete=True,
        )

        self.assertEqual(result.status, "succeeded")
        observations = RunLifecycleRepository(self.engine).list_discovery_observations(
            result.run_id,
            1,
            project_id="project-import",
            site_id="site-import",
        )
        self.assertEqual(len(observations), 2)
        port_payload = observations[1].payload["ip_v1"]
        self.assertIsNone(port_payload["detected_service"])
        self.assertIsNone(port_payload["detected_version"])
        terminal = self.run_service.get_verified_terminal_result(result.run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.status, "succeeded")

    def test_duplicate_port_identity_is_sealed_before_publishing_claims(self) -> None:
        result = self.importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=_DUPLICATE_PORT_XML,
            capture_complete=True,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.diagnostic_code, "normalization_contract_failed")
        observations = RunLifecycleRepository(self.engine).list_discovery_observations(
            result.run_id,
            1,
            project_id="project-import",
            site_id="site-import",
        )
        self.assertEqual(observations, [])
        terminal = self.run_service.get_verified_terminal_result(result.run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.status, "failed")

    def test_down_and_filtered_states_do_not_invent_timeout_evidence(self) -> None:
        result = self.importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=_UNCERTAIN_STATES_XML,
            capture_complete=True,
        )

        self.assertEqual(result.status, "succeeded")
        observations = RunLifecycleRepository(self.engine).list_discovery_observations(
            result.run_id,
            1,
            project_id="project-import",
            site_id="site-import",
        )
        by_key = {item.entity_key: item for item in observations}
        filtered = by_key["port:192.0.2.21:161:udp"].payload
        down = by_key["host:192.0.2.22"].payload
        self.assertEqual(filtered["state"], "filtered")
        self.assertEqual(filtered["ip_v1"]["reachability_state"], "unconfirmed")
        self.assertIsNone(filtered["ip_v1"]["probe_outcome"])
        self.assertEqual(filtered["ip_v1"]["attempts"], 0)
        self.assertEqual(down["state"], "down")
        self.assertEqual(down["ip_v1"]["reachability_state"], "unconfirmed")
        self.assertIsNone(down["ip_v1"]["probe_outcome"])
        self.assertEqual(down["ip_v1"]["attempts"], 0)
        terminal = self.run_service.get_verified_terminal_result(result.run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(
            [device["address"] for device in terminal.devices],
            ["192.0.2.21"],
        )

    def test_storage_failure_seals_run_and_never_exposes_raw_error(self) -> None:
        failing_store = _FailingRawStore()
        importer = NmapXmlImportService(
            self.engine,
            raw_store=failing_store,  # type: ignore[arg-type]
            run_service=self.run_service,
            producer_executor_id="inline:test-executor",
        )

        with self.assertRaises(RuntimeError) as raised:
            importer.import_payload(
                project_id="project-import",
                site_id="site-import",
                principal=self.principal,
                capability=self.capability,
                payload=_VALID_XML,
                capture_complete=True,
            )

        self.assertIsNotNone(failing_store.run_id)
        assert failing_store.run_id is not None
        run = self.run_service.get_run(failing_store.run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "operator_xml_import_storage_failed")
        terminal = self.run_service.get_verified_terminal_result(failing_store.run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.summary["diagnostic_code"], "evidence_storage_failed")
        public = str(raised.exception) + terminal.model_dump_json()
        self.assertNotIn("operator-output.xml", public)
        self.assertNotIn(r"C:\private", public)

    def test_aggregate_observation_limits_fail_before_the_first_append(self) -> None:
        cases = (
            (
                "NMAP_XML_IMPORT_MAX_OBSERVATIONS",
                2,
                "normalization_row_limit",
            ),
            (
                "NMAP_XML_IMPORT_MAX_OBSERVATION_PAYLOAD_BYTES",
                1,
                "normalization_payload_limit",
            ),
        )

        for constant, limit, diagnostic_code in cases:
            with (
                self.subTest(constant=constant),
                patch(
                    f"app.services.nmap_xml_import_service.{constant}",
                    limit,
                ),
            ):
                result = self.importer.import_payload(
                    project_id="project-import",
                    site_id="site-import",
                    principal=self.principal,
                    capability=self.capability,
                    payload=_VALID_XML,
                    capture_complete=True,
                )

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.diagnostic_code, diagnostic_code)
                observations = RunLifecycleRepository(self.engine).list_discovery_observations(
                    result.run_id,
                    1,
                    project_id="project-import",
                    site_id="site-import",
                )
                self.assertEqual(observations, [])
                terminal = self.run_service.get_verified_terminal_result(result.run_id)
                self.assertIsNotNone(terminal)
                assert terminal is not None
                self.assertEqual(terminal.status, "failed")

    def test_heartbeat_renews_during_slow_storage_and_stops_after_terminal(self) -> None:
        renewed = threading.Event()
        renewals: list[bool] = []
        heartbeats: list[_InjectedRenewingHeartbeat] = []

        def heartbeat_factory(owned, **kwargs):
            heartbeat = _InjectedRenewingHeartbeat(
                owned,
                renewed,
                renewals,
                **kwargs,
            )
            heartbeats.append(heartbeat)
            return heartbeat

        importer = NmapXmlImportService(
            self.engine,
            raw_store=_SlowRawStore(self.store, renewed),  # type: ignore[arg-type]
            run_service=self.run_service,
            producer_executor_id="inline:test-executor",
            heartbeat_factory=heartbeat_factory,
        )

        result = importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=_VALID_XML,
            capture_complete=True,
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(renewals, [True])
        self.assertEqual(len(heartbeats), 1)
        self.assertTrue(heartbeats[0].stopped)
        self.assertIsNotNone(heartbeats[0].thread)
        assert heartbeats[0].thread is not None
        self.assertFalse(heartbeats[0].thread.is_alive())

    def test_heartbeat_start_failure_is_sanitized_and_terminally_sealed(self) -> None:
        heartbeats: list[_StartFailingHeartbeat] = []

        def heartbeat_factory(owned, **kwargs):
            heartbeat = _StartFailingHeartbeat(owned, **kwargs)
            heartbeats.append(heartbeat)
            return heartbeat

        importer = NmapXmlImportService(
            self.engine,
            raw_store=self.store,
            run_service=self.run_service,
            producer_executor_id="inline:test-executor",
            heartbeat_factory=heartbeat_factory,
        )

        with self.assertRaises(NmapXmlImportLifecycleError) as raised:
            importer.import_payload(
                project_id="project-import",
                site_id="site-import",
                principal=self.principal,
                capability=self.capability,
                payload=_VALID_XML,
                capture_complete=True,
            )

        self.assertEqual(len(heartbeats), 1)
        self.assertTrue(heartbeats[0].stopped)
        run_id = heartbeats[0].owned.lease.run_id
        run = self.run_service.get_run(run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "operator_xml_import_heartbeat_start_failed")
        terminal = self.run_service.get_verified_terminal_result(run_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.summary["diagnostic_code"], "heartbeat_start_failed")
        public = str(raised.exception) + terminal.model_dump_json()
        self.assertNotIn("heartbeat.config", public)
        self.assertNotIn(r"C:\private", public)

    def test_oversize_and_incomplete_capture_are_sealed_without_parsing(self) -> None:
        parser_calls = 0

        def parser_must_not_run(*_args, **_kwargs):
            nonlocal parser_calls
            parser_calls += 1
            raise AssertionError("incomplete evidence reached the XML parser")

        importer = NmapXmlImportService(
            self.engine,
            raw_store=self.store,
            run_service=self.run_service,
            parser=parser_must_not_run,
            producer_executor_id="inline:test-executor",
        )
        with (
            patch(
                "app.services.nmap_xml_import_service.NMAP_XML_IMPORT_MAX_BYTES",
                64,
            ),
            patch(
                "app.services.nmap_xml_import_service.NMAP_XML_IMPORT_CAPTURE_BYTES",
                65,
            ),
        ):
            oversized = importer.import_payload(
                project_id="project-import",
                site_id="site-import",
                principal=self.principal,
                capability=self.capability,
                payload=b"x" * 65,
                capture_complete=False,
            )
        incomplete = importer.import_payload(
            project_id="project-import",
            site_id="site-import",
            principal=self.principal,
            capability=self.capability,
            payload=b"<nmaprun>",
            capture_complete=False,
        )

        self.assertEqual(oversized.status, "failed")
        self.assertEqual(oversized.diagnostic_code, "xml_byte_limit")
        self.assertEqual(incomplete.status, "failed")
        self.assertEqual(incomplete.diagnostic_code, "incomplete_capture")
        self.assertEqual(parser_calls, 0)
        for result in (oversized, incomplete):
            descriptor, _payload = self.store.read_verified(
                run_id=result.run_id,
                artifact_id=result.artifact_id,
            )
            self.assertFalse(descriptor.capture_complete)
            terminal = self.run_service.get_verified_terminal_result(result.run_id)
            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertEqual(terminal.status, "failed")


if __name__ == "__main__":
    unittest.main()
