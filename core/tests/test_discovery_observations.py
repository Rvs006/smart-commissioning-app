import unittest
from datetime import UTC, datetime

from pydantic import ValidationError
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
    DiscoveryObservationViewV1,
    DiscoveryProjectionV1,
    fold_discovery_observations,
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
) -> DiscoveryObservationViewV1:
    return DiscoveryObservationViewV1.model_validate(
        {
            "cursor": cursor,
            "run_id": "run-progressive",
            "attempt": 2,
            "protocol": "bacnet",
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


class DiscoveryObservationFoldTests(unittest.TestCase):
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
