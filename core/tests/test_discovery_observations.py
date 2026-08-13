import unittest
from copy import deepcopy
from datetime import UTC, datetime

from pydantic import ValidationError
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
    DiscoveryObservationViewV1,
    DiscoveryProjectionV1,
    fold_discovery_observations,
    observation_payload,
)
from smart_commissioning_core.run_context import canonical_sha256


def _observation(
    *,
    cursor: int,
    entity_key: str,
    entity_version: int,
    event_key: str,
    payload: dict[str, object],
    entity_kind: str = "device",
    protocol: str = "bacnet",
) -> DiscoveryObservationViewV1:
    return DiscoveryObservationViewV1.model_validate(
        {
            "cursor": cursor,
            "run_id": "run-progressive",
            "attempt": 2,
            "protocol": protocol,
            "entity_kind": entity_kind,
            "entity_key": entity_key,
            "entity_version": entity_version,
            "event_key": event_key,
            "phase": "enrichment",
            "outcome": "observed",
            "payload_schema_version": "1.0",
            "payload": payload,
            "payload_sha256": canonical_sha256(payload),
            "observed_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            "created_at": datetime(2026, 8, 10, 12, 0, 1, tzinfo=UTC),
        }
    )


def _ip_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "coverage_state": "attempted",
        "reachability_state": "reachable",
        "probe_outcome": "connected",
        "register_match": "expected_match",
        "policy_verdict": "pass",
        "target": "192.0.2.20",
        "port": 443,
        "transport": "tcp",
        "provider": "builtin_tcp_connect",
        "provider_version": "0.1.41",
        "provider_contract_version": "1.0",
        "provenance": {
            "profile": "gentle",
            "source_ip": "192.0.2.10",
            "source_interface": "Ethernet 2",
            "packet_plan_sha256": "a" * 64,
            "register_import_id": "imp_01JTEST",
            "register_rows_sha256": "b" * 64,
        },
        "reason": "TCP connection accepted.",
        "attempts": 1,
        "elapsed_ms": 12.5,
        "port_hint": "https",
        "detected_service": None,
        "detected_version": None,
        "control_reason": None,
        "last_packet_dispatched_at": datetime(2026, 8, 11, 9, 15, 30, tzinfo=UTC),
    }
    payload.update(overrides)
    return payload


class DiscoveryObservationFoldTests(unittest.TestCase):
    def test_operator_xml_import_is_a_truthful_provenance_profile(self) -> None:
        payload = _ip_payload()
        payload["provenance"]["profile"] = "operator_xml_import"

        observation = DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="port",
            entity_key="port:192.0.2.20:443:tcp",
            entity_version=1,
            event_key="port:192.0.2.20:443:tcp:operator-xml-v1",
            phase="finalize",
            outcome="observed",
            payload={"ip_v1": payload},
        )

        self.assertEqual(
            observation.payload["ip_v1"]["provenance"]["profile"],
            "operator_xml_import",
        )

    def test_ip_projection_port_lists_have_a_strict_4096_item_contract(self) -> None:
        def projection(ports: list[object]) -> DiscoveryProjectionV1:
            return DiscoveryProjectionV1(
                collection="devices",
                record={
                    "address": "192.0.2.20",
                    "attributes": {"open_ports": ports},
                },
            )

        self.assertEqual(
            len(
                projection(list(range(1, 514))).record["attributes"][
                    "open_ports"
                ]
            ),
            513,
        )
        self.assertEqual(
            len(
                projection(list(range(1, 4_097))).record["attributes"][
                    "open_ports"
                ]
            ),
            4_096,
        )
        for invalid in (
            list(range(1, 4_098)),
            [1, 1],
            [2, 1],
            [0],
            [65_536],
            [True],
            ["443"],
        ):
            with self.subTest(invalid=invalid[:3]), self.assertRaises(
                ValidationError
            ):
                projection(invalid)

        with self.assertRaisesRegex(ValueError, "public sequence limit"):
            observation_payload({"value": list(range(513))})

    def test_observation_payload_rejects_one_row_over_65536_canonical_bytes(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "65,536-byte canonical UTF-8 byte limit",
        ):
            observation_payload({"value": "é" * 32_768})

    def test_ip_payload_v1_preserves_typed_probe_evidence_and_provenance(self) -> None:
        last_packet_at = datetime(2026, 8, 11, 9, 15, 30, tzinfo=UTC)

        observation = DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="port",
            entity_key="port:192.0.2.20:443:tcp",
            entity_version=1,
            event_key="port:192.0.2.20:443:tcp:v1",
            phase="reachability",
            outcome="connected",
            payload_schema_version="1.0",
            payload={
                "ip_v1": _ip_payload(
                    control_reason="authorization_expired",
                    last_packet_dispatched_at=last_packet_at,
                )
            },
        )

        evidence = observation.payload["ip_v1"]
        self.assertEqual(evidence["probe_outcome"], "connected")
        self.assertEqual(evidence["provenance"]["register_import_id"], "imp_01JTEST")
        self.assertEqual(
            evidence["last_packet_dispatched_at"],
            "2026-08-11T09:15:30Z",
        )
        self.assertIsNone(evidence["detected_service"])
        self.assertEqual(evidence["control_reason"], "authorization_expired")

        normalized, encoded, digest = observation_payload(observation.payload)
        self.assertEqual(normalized, observation.payload)
        self.assertIn(b'"last_packet_dispatched_at":"2026-08-11T09:15:30Z"', encoded)
        self.assertEqual(len(digest), 64)

    def test_ip_payload_v1_rejects_unknown_unsafe_or_incomplete_evidence(self) -> None:
        invalid_payloads: list[tuple[dict[str, object], str]] = []

        invalid_enum = _ip_payload(coverage_state="complete")
        invalid_payloads.append((invalid_enum, "complete"))

        unknown_field = _ip_payload(raw_packet="4500003c")
        invalid_payloads.append((unknown_field, "4500003c"))

        unsafe_reason = _ip_payload(reason=r"read C:\Users\operator\scan.xml")
        invalid_payloads.append((unsafe_reason, r"C:\Users\operator\scan.xml"))

        invalid_control = _ip_payload(control_reason="operator_pressed_button")
        invalid_payloads.append((invalid_control, "operator_pressed_button"))

        naive_timestamp = _ip_payload(
            last_packet_dispatched_at=datetime(2026, 8, 11, 9, 15, 30)
        )
        invalid_payloads.append((naive_timestamp, "2026-08-11 09:15:30"))

        incomplete_register = _ip_payload()
        provenance = deepcopy(incomplete_register["provenance"])
        assert isinstance(provenance, dict)
        provenance["register_rows_sha256"] = None
        incomplete_register["provenance"] = provenance
        invalid_payloads.append((incomplete_register, "imp_01JTEST"))

        for ip_payload, protected_text in invalid_payloads:
            with self.subTest(protected_text=protected_text):
                with self.assertRaises(ValidationError) as rejected:
                    DiscoveryObservationInputV1(
                        protocol="ip",
                        entity_kind="port",
                        entity_key="port:192.0.2.20:443:tcp",
                        entity_version=1,
                        event_key="port:192.0.2.20:443:tcp:v1",
                        phase="reachability",
                        outcome="observed",
                        payload={"ip_v1": ip_payload},
                    )
                self.assertNotIn(protected_text, str(rejected.exception))

    def test_ip_payload_v1_rejects_coerced_numeric_evidence(self) -> None:
        for field, value in (
            ("port", "443"),
            ("port", True),
            ("attempts", "1"),
            ("attempts", True),
            ("elapsed_ms", "12.5"),
            ("elapsed_ms", True),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(
                ValidationError
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="port",
                    entity_key="port:192.0.2.20:443:tcp",
                    entity_version=1,
                    event_key="port:192.0.2.20:443:tcp:v1",
                    phase="reachability",
                    outcome="observed",
                    payload={"ip_v1": _ip_payload(**{field: value})},
                )

    def test_ip_register_match_names_exact_frozen_authority(self) -> None:
        without_register = deepcopy(_ip_payload()["provenance"])
        assert isinstance(without_register, dict)
        without_register["register_import_id"] = None
        without_register["register_rows_sha256"] = None

        not_configured = DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="port",
            entity_key="port:192.0.2.20:443:tcp",
            entity_version=1,
            event_key="port:192.0.2.20:443:tcp:v1",
            phase="comparison",
            outcome="observed",
            payload={
                "ip_v1": _ip_payload(
                    register_match="not_configured",
                    provenance=without_register,
                )
            },
        )
        self.assertEqual(
            not_configured.payload["ip_v1"]["register_match"],
            "not_configured",
        )

        mismatches = (
            ("expected_match", without_register),
            ("not_configured", _ip_payload()["provenance"]),
        )
        for register_match, provenance in mismatches:
            with self.subTest(register_match=register_match), self.assertRaisesRegex(
                ValidationError,
                "register_match must agree with frozen register provenance",
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="port",
                    entity_key="port:192.0.2.20:443:tcp",
                    entity_version=1,
                    event_key="port:192.0.2.20:443:tcp:v1",
                    phase="comparison",
                    outcome="observed",
                    payload={
                        "ip_v1": _ip_payload(
                            register_match=register_match,
                            provenance=provenance,
                        )
                    },
                )

    def test_ip_host_versions_fold_to_one_target_device_and_child_entities_do_not_project(
        self,
    ) -> None:
        def host_payload(name: str) -> dict[str, object]:
            normalized = DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="host",
                entity_key="host:192.0.2.20",
                entity_version=1,
                event_key=f"host:192.0.2.20:{name}",
                phase="finalize",
                outcome="finalized",
                payload={
                    "ip_v1": _ip_payload(
                        port=None,
                        transport=None,
                        probe_outcome=None,
                    ),
                    "projection_v1": {
                        "collection": "devices",
                        "record": {
                            "address": "192.0.2.20",
                            "name": name,
                            "attributes": {},
                        },
                    },
                },
            )
            return normalized.payload

        first = _observation(
            cursor=1,
            entity_key="host:192.0.2.20",
            entity_version=1,
            event_key="host:192.0.2.20:v1",
            entity_kind="host",
            protocol="ip",
            payload=host_payload("provisional"),
        )
        final = _observation(
            cursor=2,
            entity_key="host:192.0.2.20",
            entity_version=2,
            event_key="host:192.0.2.20:v2",
            entity_kind="host",
            protocol="ip",
            payload=host_payload("final"),
        )

        folded = fold_discovery_observations(
            [first, final],
            terminal_cursor=2,
            expected_count=2,
        )

        self.assertEqual(len(folded.devices), 1)
        self.assertEqual(folded.devices[0]["address"], "192.0.2.20")
        self.assertEqual(folded.devices[0]["name"], "final")

        projection = {
            "collection": "devices",
            "record": {"address": "192.0.2.20", "attributes": {}},
        }
        for entity_kind, entity_key in (
            ("port", "port:192.0.2.20:443:tcp"),
            ("diagnostic", "diagnostic:192.0.2.20:control"),
        ):
            with self.subTest(entity_kind=entity_kind), self.assertRaisesRegex(
                ValidationError,
                "cannot carry a terminal projection",
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind=entity_kind,
                    entity_key=entity_key,
                    entity_version=1,
                    event_key=f"{entity_key}:v1",
                    phase="finalize",
                    outcome="observed",
                    payload={
                        "ip_v1": _ip_payload(),
                        "projection_v1": projection,
                    },
                )

        invalid_host_projections = (
            (
                "host:192.0.2.21",
                projection,
                "entity_key must be stable",
            ),
            (
                "host:192.0.2.20",
                {
                    "collection": "devices",
                    "record": {"address": "192.0.2.21", "attributes": {}},
                },
                "address must match its target",
            ),
            (
                "host:192.0.2.20",
                {"collection": "summary", "record": {"reachable": True}},
                "can project only to devices",
            ),
        )
        for entity_key, invalid_projection, message in invalid_host_projections:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValidationError,
                message,
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="host",
                    entity_key=entity_key,
                    entity_version=1,
                    event_key="host:192.0.2.20:invalid:v1",
                    phase="finalize",
                    outcome="observed",
                    payload={
                        "ip_v1": _ip_payload(
                            port=None,
                            transport=None,
                            probe_outcome=None,
                        ),
                        "projection_v1": invalid_projection,
                    },
                )

    def test_ip_port_outcomes_never_strengthen_silence_into_reachability(self) -> None:
        def observation(**overrides: object) -> DiscoveryObservationInputV1:
            return DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="port",
                entity_key="port:192.0.2.20:443:tcp",
                entity_version=1,
                event_key="port:192.0.2.20:443:tcp:v1",
                phase="reachability",
                outcome="observed",
                payload={"ip_v1": _ip_payload(**overrides)},
            )

        refusal = observation(
            probe_outcome="connection_refused",
            reachability_state="unconfirmed",
            policy_verdict="expected_closed",
        )
        self.assertEqual(
            refusal.payload["ip_v1"]["reachability_state"],
            "unconfirmed",
        )

        unconfirmed_outcomes = (
            "timed_out",
            "network_unreachable",
            "host_unreachable",
            "permission_denied",
            "cancelled",
            "provider_error",
        )
        for probe_outcome in unconfirmed_outcomes:
            with self.subTest(probe_outcome=probe_outcome):
                unconfirmed = observation(
                    probe_outcome=probe_outcome,
                    reachability_state="unconfirmed",
                    policy_verdict="unconfirmed",
                )
                self.assertEqual(
                    unconfirmed.payload["ip_v1"]["reachability_state"],
                    "unconfirmed",
                )
                with self.assertRaisesRegex(
                    ValidationError,
                    "probe outcome requires unconfirmed reachability",
                ):
                    observation(
                        probe_outcome=probe_outcome,
                        reachability_state="reachable",
                        policy_verdict="unconfirmed",
                    )

        with self.assertRaisesRegex(
            ValidationError,
            "probe outcome requires unconfirmed reachability",
        ):
            observation(
                probe_outcome="connection_refused",
                reachability_state="reachable",
                policy_verdict="expected_closed",
            )

    def test_ip_port_identity_and_policy_verdict_require_matching_evidence(self) -> None:
        def observation(
            *,
            entity_key: str = "port:192.0.2.20:443:tcp",
            **overrides: object,
        ) -> DiscoveryObservationInputV1:
            return DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="port",
                entity_key=entity_key,
                entity_version=1,
                event_key="port:192.0.2.20:443:tcp:v1",
                phase="reachability",
                outcome="observed",
                payload={"ip_v1": _ip_payload(**overrides)},
            )

        self.assertEqual(observation().payload["ip_v1"]["transport"], "tcp")

        invalid_identity = (
            ({"port": None}, "requires port and transport"),
            ({"transport": None}, "requires port and transport"),
            (
                {"entity_key": "port:192.0.2.20:443:udp"},
                "entity_key must preserve target, port, and transport",
            ),
        )
        for values, message in invalid_identity:
            with self.subTest(values=values), self.assertRaisesRegex(
                ValidationError,
                message,
            ):
                entity_key = str(
                    values.get("entity_key", "port:192.0.2.20:443:tcp")
                )
                overrides = {
                    key: value for key, value in values.items() if key != "entity_key"
                }
                observation(entity_key=entity_key, **overrides)

        invalid_verdicts = (
            ("timed_out", "forbidden_open", "open-port verdict requires connected"),
            (
                "connection_refused",
                "unexpected_open_review",
                "open-port verdict requires connected",
            ),
            ("timed_out", "expected_closed", "expected_closed requires refusal"),
        )
        for probe_outcome, policy_verdict, message in invalid_verdicts:
            with self.subTest(policy_verdict=policy_verdict), self.assertRaisesRegex(
                ValidationError,
                message,
            ):
                observation(
                    probe_outcome=probe_outcome,
                    reachability_state="unconfirmed",
                    policy_verdict=policy_verdict,
                )

    def test_ip_not_attempted_port_cannot_fabricate_probe_or_policy_evidence(self) -> None:
        def observation(**overrides: object) -> DiscoveryObservationInputV1:
            values: dict[str, object] = {
                "coverage_state": "not_attempted",
                "reachability_state": "unconfirmed",
                "probe_outcome": None,
                "policy_verdict": "not_attempted",
                "reason": "Removed by the frozen profile port cap.",
                "attempts": 0,
                "elapsed_ms": 0,
                "last_packet_dispatched_at": None,
            }
            values.update(overrides)
            return DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="port",
                entity_key="port:192.0.2.20:443:tcp",
                entity_version=1,
                event_key="port:192.0.2.20:443:tcp:v1",
                phase="finalize",
                outcome="not_attempted",
                payload={"ip_v1": _ip_payload(**values)},
            )

        self.assertEqual(
            observation().payload["ip_v1"]["coverage_state"],
            "not_attempted",
        )

        invalid = (
            {"probe_outcome": "connected", "reachability_state": "reachable"},
            {"attempts": 1},
            {"policy_verdict": "unconfirmed"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                ValidationError,
                "not_attempted coverage cannot carry probe or policy evidence",
            ):
                observation(**overrides)

    def test_ip_udp_47808_omission_carries_only_the_bacnet_capability_action(
        self,
    ) -> None:
        values = {
            "coverage_state": "not_attempted",
            "reachability_state": "not_applicable",
            "probe_outcome": None,
            "policy_verdict": "not_attempted",
            "port": 47808,
            "transport": "udp",
            "reason": "The built-in TCP provider does not support UDP probes.",
            "attempts": 0,
            "elapsed_ms": 0,
            "last_packet_dispatched_at": None,
            "capability_action": "use_bacnet_discovery",
        }

        observation = DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="port",
            entity_key="port:192.0.2.20:47808:udp",
            entity_version=1,
            event_key="port:192.0.2.20:47808:udp:v1",
            phase="planned",
            outcome="not_attempted",
            payload={"ip_v1": _ip_payload(**values)},
        )

        self.assertEqual(
            observation.payload["ip_v1"]["capability_action"],
            "use_bacnet_discovery",
        )
        for overrides in (
            {"transport": "tcp"},
            {"port": 161},
            {"coverage_state": "attempted", "attempts": 1},
        ):
            invalid_values = {**values, **overrides}
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                ValidationError,
                "BACnet capability action requires an unattempted UDP/47808 port",
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="port",
                    entity_key=(
                        "port:192.0.2.20:47808:tcp"
                        if overrides.get("transport") == "tcp"
                        else "port:192.0.2.20:161:udp"
                        if overrides.get("port") == 161
                        else "port:192.0.2.20:47808:udp"
                    ),
                    entity_version=1,
                    event_key="port:192.0.2.20:47808:udp:invalid",
                    phase="planned",
                    outcome="not_attempted",
                    payload={"ip_v1": _ip_payload(**invalid_values)},
                )

        for probe_outcome, reachability_state in (
            ("connected", "reachable"),
            ("connection_refused", "reachable"),
            ("timed_out", "unconfirmed"),
        ):
            attempted_values = {
                **values,
                "coverage_state": "attempted",
                "reachability_state": reachability_state,
                "probe_outcome": probe_outcome,
                "policy_verdict": "pass",
                "attempts": 1,
                "capability_action": None,
            }
            with self.subTest(probe_outcome=probe_outcome), self.assertRaisesRegex(
                ValidationError,
                "built-in TCP provider cannot emit attempted UDP evidence",
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="port",
                    entity_key="port:192.0.2.20:47808:udp",
                    entity_version=1,
                    event_key=f"port:192.0.2.20:47808:udp:{probe_outcome}",
                    phase="reachability",
                    outcome=probe_outcome,
                    payload={"ip_v1": _ip_payload(**attempted_values)},
                )

        with self.assertRaisesRegex(
            ValidationError,
            "built-in UDP/47808 omissions require use_bacnet_discovery",
        ):
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="port",
                entity_key="port:192.0.2.20:47808:udp",
                entity_version=1,
                event_key="port:192.0.2.20:47808:udp:missing-action",
                phase="planned",
                outcome="not_attempted",
                payload={
                    "ip_v1": _ip_payload(
                        **{**values, "capability_action": None}
                    )
                },
            )

    def test_ip_port_hint_is_never_promoted_to_builtin_detected_service(self) -> None:
        def observation(**overrides: object) -> DiscoveryObservationInputV1:
            return DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="port",
                entity_key="port:192.0.2.20:443:tcp",
                entity_version=1,
                event_key="port:192.0.2.20:443:tcp:v1",
                phase="enrichment",
                outcome="observed",
                payload={"ip_v1": _ip_payload(**overrides)},
            )

        hinted = observation(port_hint="https")
        self.assertEqual(hinted.payload["ip_v1"]["port_hint"], "https")
        self.assertIsNone(hinted.payload["ip_v1"]["detected_service"])

        with self.assertRaisesRegex(
            ValidationError,
            "built-in TCP provider cannot claim detected service evidence",
        ):
            observation(detected_service="https")

        with self.assertRaisesRegex(
            ValidationError,
            "detected_version requires detected_service",
        ):
            observation(
                provider="approved_protocol_provider",
                detected_version="1.2.3",
            )

    def test_observation_identities_reject_viewer_visible_evidence(self) -> None:
        forbidden_identities = (
            ("entity_key", "host=/Users/operator/runtime/scan.xml"),
            ("event_key", r"scan[C:\Users\operator\runtime\scan.xml"),
            ("entity_key", "secret://runtime-password"),
            ("event_key", "secret:runtime-password"),
            ("event_key", "file:///var/tmp/scan.xml"),
            ("entity_key", "file:runtime.xml"),
            ("entity_key", "certificate=-----BEGIN PRIVATE KEY-----"),
            ("event_key", "-----END CERTIFICATE-----"),
            ("event_key", "password=hunter2"),
            ("entity_key", "password:hunter2"),
        )

        for field, identity in forbidden_identities:
            values = {
                "protocol": "ip",
                "entity_kind": "host",
                "entity_key": "host:192.0.2.20",
                "entity_version": 1,
                "event_key": "host:192.0.2.20:v1",
                "phase": "reachability",
                "outcome": "observed",
                "payload_schema_version": "1.0",
                "payload": {"reachable": True},
                field: identity,
            }
            with self.subTest(field=field, identity=identity):
                with self.assertRaisesRegex(
                    ValidationError,
                    "opaque normalized syntax",
                ) as rejected:
                    DiscoveryObservationInputV1.model_validate(values)
                self.assertNotIn(identity, str(rejected.exception))

    def test_opaque_identity_grammar_preserves_existing_keys_and_ot_values(self) -> None:
        existing_identity_pairs = (
            ("192.0.2.20", "host:192.0.2.20:v1"),
            ("device:4001", "device:4001:v1"),
            (
                "projection_v1:devices:00000000",
                "projection_v1:devices:00000000:v12",
            ),
            ("i-am:7:duplicate", "i-am-7-duplicate-1"),
            ("summary", "projection:summary:v1"),
            ("192.0.2.99", "drift-after-proof:event:late"),
        )
        ot_values = [
            "site/AHU-01/events/pointset",
            "runtime/AHU-01/state",
            "analog-input,1",
            "Password reset status is unavailable.",
            "password_status=disabled",
            "token_status=active",
            "secret_state=closed",
            "Authorization alarm is active.",
            "active/inactive",
            "Dew point: -3.2 °C",
        ]

        for entity_key, event_key in existing_identity_pairs:
            with self.subTest(entity_key=entity_key, event_key=event_key):
                observation = DiscoveryObservationInputV1(
                    protocol="bacnet",
                    entity_kind="device",
                    entity_key=entity_key,
                    entity_version=1,
                    event_key=event_key,
                    phase="enrichment",
                    outcome="observed",
                    payload_schema_version="1.0",
                    payload={"value": ot_values},
                )
                self.assertEqual(observation.entity_key, entity_key)
                self.assertEqual(observation.event_key, event_key)
                self.assertEqual(observation.payload["value"], ot_values)

    def test_viewer_safe_contract_rejects_raw_evidence_and_allows_opaque_ids(self) -> None:
        forbidden = (
            {"raw_xml": "<nmaprun />"},
            {"diagnostic": {"stderr": "permission denied"}},
            {"pcap": "packet bytes"},
            {"password": "hunter2"},
            {"source": r"C:\\Users\\operator\\scan.xml"},
            {"source": "/var/tmp/scan.xml"},
            {"value": "secret://runtime-password"},
        )
        for payload in forbidden:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                ValueError,
                "public v1|raw|locally scoped",
            ):
                _observation(
                    cursor=1,
                    entity_key="viewer-safe-check",
                    entity_version=1,
                    event_key="viewer-safe-check-v1",
                    payload=payload,
                )

        with self.assertRaisesRegex(ValueError, "raw bytes"):
            DiscoveryObservationInputV1.model_validate(
                {
                    "protocol": "ip",
                    "entity_kind": "host",
                    "entity_key": "viewer-safe-check",
                    "entity_version": 1,
                    "event_key": "viewer-safe-check-v1",
                    "phase": "reachability",
                    "outcome": "observed",
                    "payload_schema_version": "1.0",
                    "payload": {"value": b"raw packet"},
                }
            )

        safe = _observation(
            cursor=1,
            entity_key="viewer-safe-check",
            entity_version=1,
            event_key="viewer-safe-check-v1",
            payload={
                "xml_artifact_id": "artifact_01J00000000000000000000000",
                "stderr_evidence_id": "evidence_01J0000000000000000000000",
                "reachable": False,
                "attempts": 0,
            }
        )
        self.assertFalse(safe.payload["reachable"])
        self.assertEqual(safe.payload["attempts"], 0)

    def test_public_payload_v1_is_allowlisted_versioned_and_bounded(self) -> None:
        forbidden = (
            {"auth_token": "viewer-must-not-see-this"},
            {"packet_dump": "4500003c"},
            {
                "diagnostic_text": (
                    r"scanner failed while reading C:\Users\operator\scan.xml"
                )
            },
            {"diagnostic_text": "parser returned <root>raw XML</root>"},
            {"diagnostic_text": "x" * 4_097},
            {"unexpected_public_field": "looks harmless but has no v1 contract"},
        )
        for payload in forbidden:
            with self.subTest(payload=next(iter(payload))), self.assertRaises(
                (TypeError, ValueError)
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="diagnostic",
                    entity_key="public-contract",
                    entity_version=1,
                    event_key=f"public-contract-{next(iter(payload))}",
                    phase="enrichment",
                    outcome="observed",
                    payload_schema_version="1.0",
                    payload=payload,
                )

        with self.assertRaises(ValidationError):
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="diagnostic",
                entity_key="future-contract",
                entity_version=1,
                event_key="future-contract-v2",
                phase="enrichment",
                outcome="observed",
                payload_schema_version="2.0",
                payload={"diagnostic_text": "safe text"},
            )

        accepted = DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="diagnostic",
            entity_key="public-contract-safe",
            entity_version=1,
            event_key="public-contract-safe-v1",
            phase="enrichment",
            outcome="observed",
            payload_schema_version="1.0",
            payload={
                "diagnostic_text": "Host did not answer within the bounded window.",
                "response_count": 0,
                "accepted": False,
            },
        )
        self.assertEqual(accepted.payload["response_count"], 0)
        self.assertIs(accepted.payload["accepted"], False)

    def test_viewer_strings_reject_embedded_secret_and_local_evidence_markers(
        self,
    ) -> None:
        forbidden_payloads = (
            {"diagnostic_text": "scanner note: file:///var/tmp/scan.xml"},
            {"value": ["probe failed,secret://runtime-password"]},
            {
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "name": r"captured[C:\Users\operator\scan.xml",
                        "attributes": {},
                    },
                }
            },
            {
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "name": "certificate=-----BEGIN PRIVATE KEY-----",
                        "attributes": {
                            "marker": "certificate=-----END PRIVATE KEY-----"
                        },
                    },
                }
            },
        )

        for payload in forbidden_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                ValueError,
                "raw or locally scoped evidence",
            ):
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="diagnostic",
                    entity_key="embedded-public-evidence",
                    entity_version=1,
                    event_key="embedded-public-evidence-v1",
                    phase="enrichment",
                    outcome="observed",
                    payload_schema_version="1.0",
                    payload=payload,
                )

    def test_viewer_strings_reject_local_runtime_paths_and_credential_assignments(
        self,
    ) -> None:
        forbidden_payloads = (
            {
                "diagnostic_text": (
                    "scanner persisted output at /Users/operator/runtime/scan.xml"
                )
            },
            {"value": ["probe output=(runtime/scan.xml)"]},
            {"value": ["probe output=../runtime/scan"]},
            {"value": ["probe reference secret:runtime-password"]},
            {"diagnostic_text": "probe reference file:runtime.xml"},
            {
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "name": "commissioning note password=hunter2",
                        "attributes": {},
                    },
                }
            },
            {"diagnostic_text": "commissioning note password:hunter2"},
            {"diagnostic_text": "commissioning note mqtt_password=hunter2"},
            {"value": ["authorization: Bearer viewer-token"]},
            {"value": ["token=abc123"]},
        )

        for payload in forbidden_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ValidationError,
                    "raw or locally scoped evidence",
                ) as rejected:
                    DiscoveryObservationInputV1(
                        protocol="ip",
                        entity_kind="diagnostic",
                        entity_key="viewer-safe-free-form",
                        entity_version=1,
                        event_key="viewer-safe-free-form:v1",
                        phase="enrichment",
                        outcome="observed",
                        payload_schema_version="1.0",
                        payload=payload,
                    )
                for content in (
                    "/Users/",
                    "runtime/scan.xml",
                    "../runtime/scan",
                    "runtime-password",
                    "runtime.xml",
                    "hunter2",
                    "viewer-token",
                    "abc123",
                ):
                    self.assertNotIn(content, str(rejected.exception))

    def test_projection_validation_errors_do_not_echo_restricted_content(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "raw or locally scoped evidence",
        ) as rejected:
            DiscoveryProjectionV1(
                collection="devices",
                record={"name": "password=hunter2", "attributes": {}},
            )

        self.assertNotIn("hunter2", str(rejected.exception))

    def test_public_projection_preserves_typed_bacnet_partial_read_markers(self) -> None:
        observation = DiscoveryObservationInputV1(
            protocol="bacnet",
            entity_kind="device",
            entity_key="device:4001",
            entity_version=1,
            event_key="device:4001:v1",
            phase="finalize",
            outcome="observed",
            payload_schema_version="1.0",
            payload={
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "address": "192.0.2.40:47808",
                        "device_type": "bacnet_device",
                        "attributes": {
                            "heard_not_enriched": True,
                            "object_list_read_failed": True,
                            "point_reads_aborted": {
                                "after_consecutive_failures": 5,
                                "points_not_attempted": 3,
                            },
                            "point_reads_truncated": {
                                "points_not_attempted": 4,
                            },
                        },
                    },
                }
            }
        )

        attributes = observation.payload["projection_v1"]["record"]["attributes"]
        self.assertIs(attributes["heard_not_enriched"], True)
        self.assertEqual(
            attributes["point_reads_aborted"],
            {"after_consecutive_failures": 5, "points_not_attempted": 3},
        )

    def test_entity_version_not_cursor_selects_the_terminal_projection(self) -> None:
        newer_state_arrived_first = _observation(
            cursor=10,
            entity_key="device:42",
            entity_version=2,
            event_key="device-42-v2",
            payload={
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "address": "192.0.2.42",
                        "name": "AHU-42",
                        "attributes": {"online": False, "objects": 0},
                    },
                }
            },
        )
        older_state_arrived_later = _observation(
            cursor=14,
            entity_key="device:42",
            entity_version=1,
            event_key="device-42-v1-late",
            payload={
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "address": "192.0.2.42",
                        "name": "stale-name",
                        "attributes": {"online": True, "objects": 99},
                    },
                }
            },
        )

        folded = fold_discovery_observations(
            [newer_state_arrived_first, older_state_arrived_later],
            terminal_cursor=14,
        )

        self.assertEqual(folded.observation_count, 2)
        self.assertEqual(folded.terminal_cursor, 14)
        self.assertEqual(len(folded.devices), 1)
        self.assertEqual(folded.projected_collections, frozenset({"devices"}))
        self.assertEqual(folded.devices[0]["name"], "AHU-42")
        self.assertEqual(
            folded.devices[0]["attributes"],
            {"online": False, "objects": 0},
        )

    def test_stream_digest_binds_non_projecting_evidence_and_order(self) -> None:
        projected = _observation(
            cursor=3,
            entity_key="device:7",
            entity_version=1,
            event_key="device-7",
            payload={
                "projection_v1": {
                    "collection": "devices",
                    "record": {"address": "192.0.2.7", "attributes": {}},
                }
            },
        )
        duplicate_response = _observation(
            cursor=8,
            entity_kind="diagnostic",
            entity_key="i-am:7:duplicate",
            entity_version=1,
            event_key="i-am-7-duplicate-1",
            payload={"response_count": 2, "accepted": False},
        )

        with_diagnostic = fold_discovery_observations(
            [projected, duplicate_response], terminal_cursor=8
        )
        without_diagnostic = fold_discovery_observations(
            [projected], terminal_cursor=3
        )

        self.assertNotEqual(
            with_diagnostic.observation_stream_sha256,
            without_diagnostic.observation_stream_sha256,
        )
        self.assertEqual(len(with_diagnostic.devices), 1)
        self.assertEqual(
            with_diagnostic.projected_collections,
            frozenset({"devices"}),
        )
        self.assertRegex(with_diagnostic.observation_stream_sha256, r"^[0-9a-f]{64}$")

    def test_projection_envelope_is_strict_and_viewer_safe(self) -> None:
        with self.assertRaises(ValidationError):
            _observation(
                cursor=1,
                entity_key="device:1",
                entity_version=1,
                event_key="device-1",
                payload={
                    "projection_v1": {
                        "collection": "devices",
                        "record": {"address": "192.0.2.1"},
                        "raw_stderr": "must not be accepted here",
                    }
                },
            )

    def test_fold_rejects_mixed_attempts_and_cursor_beyond_cutoff(self) -> None:
        first = _observation(
            cursor=4,
            entity_key="device:1",
            entity_version=1,
            event_key="device-1",
            payload={},
        )
        other_attempt = first.model_copy(
            update={"cursor": 5, "attempt": 3, "event_key": "other-attempt"}
        )

        with self.assertRaisesRegex(ValueError, "one run and attempt"):
            fold_discovery_observations([first, other_attempt], terminal_cursor=5)
        with self.assertRaisesRegex(ValueError, "terminal cursor"):
            fold_discovery_observations([first], terminal_cursor=3)


if __name__ == "__main__":
    unittest.main()
