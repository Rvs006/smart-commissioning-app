from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import RunContextV1, canonical_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from smart_commissioning_core.sealed_run_integrity import (
    SealedRunIntegrityError,
    verify_legacy_report_evidence_run,
    verify_report_evidence_run,
    verify_sealed_run,
)


def _sealed_snapshot() -> dict[str, object]:
    context = RunContextV1(
        project_id="integrity-project",
        site_id="integrity-site",
        configuration_snapshot={},
        configuration_version="fixture-1",
        engine_parameters={"dry_run": True},
        requesting_principal="integrity-test",
        application_version="test",
    )
    issue = ValidationIssueRecord(
        issue_id="issue-1",
        asset_id="ahu-1",
        issue_type="unexpected-port",
        severity="high",
        description="Unexpected service observed.",
        last_seen_at=datetime(2026, 8, 10, 11, 45, tzinfo=UTC),
    ).model_dump(mode="json")
    device = {
        "project_id": "integrity-project",
        "site_id": "integrity-site",
        "address": "192.0.2.10",
        "device_type": "ip_host",
        "name": "ahu-1",
        "vendor": None,
        "model": None,
        "attributes": {"open_ports": [80]},
    }
    point = {
        "device_ref": "192.0.2.10",
        "point_id": "supply-temp",
        "point_name": "Supply temperature",
        "observed_value": {"value": 21.5},
        "units": "C",
        "attributes": {},
    }
    topic = {
        "topic": "/devices/ahu-1/state",
        "last_payload": {"system": {"operational": True}},
        "message_count": 2,
        "attributes": {"qos": 1},
    }
    terminal = TerminalResultV1(
        status="succeeded",
        stage="engine_complete",
        summary={"devices_found": 1},
        issues=(issue,),
        devices=(device,),
        points=(point,),
        topics=(topic,),
    )
    result_sha256 = terminal.sha256()
    return {
        "run_id": "run_integrity_fixture",
        "run": {
            "status": terminal.status,
            "stage": terminal.stage,
            "result_summary": dict(terminal.summary),
            "error_message": terminal.error_message,
            "result_sha256": result_sha256,
        },
        "context": {
            "context_json": context.model_dump(mode="json"),
            "context_sha256": context.sha256(),
        },
        "result": {
            "schema_version": terminal.schema_version,
            "terminal_status": terminal.status,
            "terminal_stage": terminal.stage,
            "summary": dict(terminal.summary),
            "result_payload": terminal.model_dump(mode="json"),
            "result_sha256": result_sha256,
        },
        "seal": {
            "terminal_status": terminal.status,
            "context_sha256": context.sha256(),
            "result_sha256": result_sha256,
            "sealed_at": datetime(2026, 8, 10, tzinfo=UTC),
        },
        "issues": [
            {
                **issue,
                "id": 11,
                "run_id": "run_integrity_fixture",
                "position": 0,
                "last_seen_at": datetime(2026, 8, 10, 11, 45, tzinfo=UTC),
            }
        ],
        "devices": [
            {
                **device,
                "id": 12,
                "run_id": "run_integrity_fixture",
                "position": 0,
                "created_at": "2026-08-10T12:00:00+00:00",
            }
        ],
        "points": [
            {
                **point,
                "id": 13,
                "run_id": "run_integrity_fixture",
                "position": 0,
                "created_at": "2026-08-10T12:00:00+00:00",
            }
        ],
        "topics": [
            {
                **topic,
                "id": 14,
                "run_id": "run_integrity_fixture",
                "position": 0,
                "created_at": "2026-08-10T12:00:00+00:00",
            }
        ],
    }


def _report_sealed_snapshot() -> dict[str, object]:
    report_id = "run_report_integrity_fixture"
    report_metadata = {
        "output_format": "pdf",
        "report_type": "ip_discovery",
        "report_title_custom": False,
        "report_title": "IP discovery report",
        "report_generated_at": "2026-08-10T12:00:00+00:00",
        "renderer_version": "0.1.40",
        "evidence_set_id": "evidence-fixture",
        "udmi_report_variant": "summary",
    }
    snapshot = {
        "schema_version": "2.0",
        "project_id": "integrity-project",
        "site_id": "integrity-site",
        "report_type": "ip_discovery",
        "output_format": "pdf",
        "source_run_ids": ["run_source_fixture"],
        "renderer_version": "0.1.40",
        "evidence_set_id": "evidence-fixture",
        "report_metadata": report_metadata,
        "source_run_snapshots": [],
        "source_run_seals": {},
        "source_discovery_snapshots": {},
        "udmi_scope": None,
    }
    snapshot_sha256 = canonical_sha256(snapshot)
    manifest = {
        "report_id": report_id,
        "snapshot_sha256": snapshot_sha256,
        "file_name": "ip-discovery-report.pdf",
        "media_type": "application/pdf",
        "byte_size": 123,
        "renderer_version": "0.1.40",
        "artifact_sha256": "a" * 64,
        "artifact_relpath": f"{report_id}-{'a' * 64}.pdf",
        "evidence_set_id": "evidence-fixture",
    }
    terminal = TerminalResultV1(
        status="succeeded",
        stage="report_ready",
        summary={"artifact_manifest": manifest},
    )
    result_sha256 = terminal.sha256()
    return {
        "run_id": report_id,
        "run": {
            "job_type": "report_generation",
            "project_id": "integrity-project",
            "site_id": "integrity-site",
            "status": terminal.status,
            "stage": terminal.stage,
            "parameters": {
                **report_metadata,
                "source_run_ids": ["run_source_fixture"],
                "source_run_snapshots": [],
                "source_run_seals": {},
                "source_discovery_snapshots": {},
                "udmi_scope": None,
                "report_snapshot_v2": snapshot,
                "report_snapshot_sha256": snapshot_sha256,
            },
            "result_summary": dict(terminal.summary),
            "error_message": terminal.error_message,
            "result_sha256": result_sha256,
            "terminal_at": datetime(2026, 8, 10, tzinfo=UTC),
        },
        "context": None,
        "result": {
            "schema_version": terminal.schema_version,
            "terminal_status": terminal.status,
            "terminal_stage": terminal.stage,
            "summary": dict(terminal.summary),
            "result_payload": terminal.model_dump(mode="json"),
            "result_sha256": result_sha256,
        },
        "seal": {
            "terminal_status": terminal.status,
            "context_sha256": snapshot_sha256,
            "result_sha256": result_sha256,
            "sealed_at": datetime(2026, 8, 10, tzinfo=UTC),
        },
        "issues": [],
        "devices": [],
        "points": [],
        "topics": [],
    }


def _stamp_observation_evidence(
    snapshot: dict[str, object],
    evidence: dict[str, object],
) -> None:
    payload = copy.deepcopy(snapshot["result"]["result_payload"])
    summary = dict(payload["summary"])
    summary["observation_evidence_v1"] = evidence
    payload["summary"] = summary
    terminal = TerminalResultV1.model_validate(payload)
    digest = terminal.sha256()
    snapshot["run"]["attempt"] = evidence["attempt"]
    snapshot["run"]["result_summary"] = summary
    snapshot["run"]["result_sha256"] = digest
    snapshot["result"]["summary"] = summary
    snapshot["result"]["result_payload"] = payload
    snapshot["result"]["result_sha256"] = digest
    snapshot["seal"]["result_sha256"] = digest


class SealedRunIntegrityTests(unittest.TestCase):
    def test_rejects_each_terminal_metadata_projection_that_drifted(self) -> None:
        mutations = {
            "run status": ("run", "status", "failed"),
            "run stage": ("run", "stage", "tampered-stage"),
            "run error": ("run", "error_message", "tampered-error"),
            "result schema": ("result", "schema_version", "9.9"),
            "result status": ("result", "terminal_status", "failed"),
            "result stage": ("result", "terminal_stage", "tampered-stage"),
            "seal status": ("seal", "terminal_status", "failed"),
        }
        for label, (section, field, value) in mutations.items():
            with self.subTest(label=label):
                snapshot = copy.deepcopy(_sealed_snapshot())
                snapshot[section][field] = value
                with self.assertRaisesRegex(SealedRunIntegrityError, "terminal metadata"):
                    verify_sealed_run(**snapshot)

    def test_rejects_each_persisted_evidence_projection_that_drifted(self) -> None:
        mutations = {
            "issues": ("issues", "description", "Tampered issue."),
            "devices": ("devices", "address", "192.0.2.99"),
            "points": ("points", "units", "F"),
            "topics": ("topics", "message_count", 999),
        }
        for label, (section, field, value) in mutations.items():
            with self.subTest(label=label):
                snapshot = copy.deepcopy(_sealed_snapshot())
                snapshot[section][0][field] = value
                with self.assertRaisesRegex(SealedRunIntegrityError, label):
                    verify_sealed_run(**snapshot)

    def test_accepts_normalized_persisted_evidence_metadata(self) -> None:
        verified = verify_sealed_run(**_sealed_snapshot())

        self.assertIsNotNone(verified)

    def test_accepts_a_well_formed_observation_stream_commitment(self) -> None:
        snapshot = _sealed_snapshot()
        _stamp_observation_evidence(
            snapshot,
            {
                "schema_version": "1.0",
                "attempt": 2,
                "observation_count": 2,
                "terminal_cursor": 8,
                "observation_stream_sha256": "b" * 64,
            },
        )

        verified = verify_sealed_run(**snapshot)

        self.assertEqual(
            verified.terminal_result.summary["observation_evidence_v1"][
                "terminal_cursor"
            ],
            8,
        )

    def test_rejects_a_coherently_rehashed_inconsistent_observation_commitment(self) -> None:
        snapshot = _sealed_snapshot()
        _stamp_observation_evidence(
            snapshot,
            {
                "schema_version": "1.0",
                "attempt": 2,
                "observation_count": 0,
                "terminal_cursor": 8,
                "observation_stream_sha256": "b" * 64,
            },
        )

        with self.assertRaisesRegex(SealedRunIntegrityError, "count and cursor"):
            verify_sealed_run(**snapshot)

    def test_legacy_compatibility_requires_every_lifecycle_component_to_be_absent(self) -> None:
        snapshot = _sealed_snapshot()
        snapshot["context"] = None
        snapshot["result"] = None
        snapshot["seal"] = None
        snapshot["run"]["result_sha256"] = None
        snapshot["run"]["terminal_at"] = None
        snapshot["run"]["attempt"] = 0
        snapshot["run"]["state_version"] = 0

        self.assertIsNone(verify_sealed_run(**snapshot))

        snapshot["run"]["result_sha256"] = "a" * 64
        with self.assertRaisesRegex(SealedRunIntegrityError, "modern lifecycle"):
            verify_sealed_run(**snapshot)

        snapshot["run"]["result_sha256"] = None
        snapshot["run"]["modern_lifecycle_component_present"] = True
        with self.assertRaisesRegex(SealedRunIntegrityError, "modern lifecycle"):
            verify_sealed_run(**snapshot)

    def test_rejects_a_partially_present_modern_lifecycle(self) -> None:
        snapshot = _sealed_snapshot()
        snapshot["result"] = None

        with self.assertRaisesRegex(SealedRunIntegrityError, "partially present"):
            verify_sealed_run(**snapshot)

    def test_accepts_a_report_sealed_to_its_canonical_frozen_snapshot(self) -> None:
        verified = verify_report_evidence_run(**_report_sealed_snapshot())

        self.assertEqual(verified.terminal_result.stage, "report_ready")

    def test_report_rejects_snapshot_tamper_even_when_superficial_hash_is_updated(self) -> None:
        report = _report_sealed_snapshot()
        report["run"]["parameters"]["report_snapshot_v2"]["tampered"] = True
        report["run"]["parameters"]["report_snapshot_sha256"] = canonical_sha256(
            report["run"]["parameters"]["report_snapshot_v2"]
        )

        with self.assertRaisesRegex(SealedRunIntegrityError, "run seal"):
            verify_report_evidence_run(**report)

    def test_report_rejects_a_missing_frozen_snapshot_or_terminal_projection(self) -> None:
        missing_snapshot = _report_sealed_snapshot()
        del missing_snapshot["run"]["parameters"]["report_snapshot_v2"]
        with self.assertRaisesRegex(SealedRunIntegrityError, "frozen snapshot"):
            verify_report_evidence_run(**missing_snapshot)

        missing_projection = _report_sealed_snapshot()
        payload = dict(missing_projection["result"]["result_payload"])
        payload["issues"] = [
            ValidationIssueRecord(
                issue_id="missing-issue",
                issue_type="report-integrity",
                severity="high",
                description="Sealed issue is absent from its projection.",
            ).model_dump(mode="json")
        ]
        terminal = TerminalResultV1.model_validate(payload)
        digest = terminal.sha256()
        missing_projection["result"]["result_payload"] = payload
        missing_projection["result"]["result_sha256"] = digest
        missing_projection["run"]["result_sha256"] = digest
        missing_projection["seal"]["result_sha256"] = digest
        with self.assertRaisesRegex(SealedRunIntegrityError, "issues"):
            verify_report_evidence_run(**missing_projection)

    def test_report_rejects_lifecycle_context_shape_confusion(self) -> None:
        report = _report_sealed_snapshot()
        report["context"] = _sealed_snapshot()["context"]

        with self.assertRaisesRegex(SealedRunIntegrityError, "must not carry"):
            verify_report_evidence_run(**report)

    def test_report_rejects_scope_metadata_snapshot_and_manifest_drift(self) -> None:
        mutations = {
            "project scope": ("run", "project_id", "other-project"),
            "site scope": ("run", "site_id", "other-site"),
            "output parameter": ("parameters", "output_format", "zip"),
            "title parameter": ("parameters", "report_title", "Changed title"),
            "source snapshots": ("parameters", "source_run_snapshots", [{"run_id": "other"}]),
            "manifest report id": ("manifest", "report_id", "run_other"),
            "manifest renderer": ("manifest", "renderer_version", "0.1.39"),
            "manifest evidence set": ("manifest", "evidence_set_id", "other-evidence"),
            "manifest media type": ("manifest", "media_type", "application/zip"),
        }
        for label, (section, field, value) in mutations.items():
            with self.subTest(label=label):
                report = copy.deepcopy(_report_sealed_snapshot())
                if section == "parameters":
                    report["run"]["parameters"][field] = value
                elif section == "manifest":
                    report["run"]["result_summary"]["artifact_manifest"][field] = value
                    report["result"]["summary"]["artifact_manifest"][field] = value
                    payload = report["result"]["result_payload"]
                    payload["summary"]["artifact_manifest"][field] = value
                    terminal = TerminalResultV1.model_validate(payload)
                    digest = terminal.sha256()
                    report["run"]["result_sha256"] = digest
                    report["result"]["result_sha256"] = digest
                    report["seal"]["result_sha256"] = digest
                else:
                    report[section][field] = value
                with self.assertRaises(SealedRunIntegrityError):
                    verify_report_evidence_run(**report)

    def test_f6_legacy_report_requires_its_marker_and_rejects_modern_components(self) -> None:
        report = _report_sealed_snapshot()
        legacy_summary = {
            "legacy_report_integrity": {
                "classification": "missing",
                "migration": "v0.1.26",
                "silently_resigned": False,
            }
        }
        legacy_terminal = TerminalResultV1(
            status="succeeded",
            stage="report_ready",
            summary=legacy_summary,
        )
        legacy_digest = legacy_terminal.sha256()
        run = report["run"]
        run["parameters"] = {"output_format": "pdf", "report_type": "ip_discovery"}
        run["result_summary"] = legacy_summary
        run["result_sha256"] = legacy_digest
        run["execution_mode"] = "inline"
        run["created_at"] = datetime(2026, 8, 10, tzinfo=UTC)
        run["updated_at"] = datetime(2026, 8, 10, tzinfo=UTC)
        report["result"]["summary"] = legacy_summary
        report["result"]["result_payload"] = legacy_terminal.model_dump(mode="json")
        report["result"]["result_sha256"] = legacy_digest
        report["seal"]["result_sha256"] = legacy_digest
        report["seal"]["context_sha256"] = canonical_sha256(
            {
                "schema_version": "legacy-0",
                "run_id": report["run_id"],
                "project_id": run["project_id"],
                "site_id": run["site_id"],
                "job_type": run["job_type"],
                "parameters": run["parameters"],
                "execution_mode": run["execution_mode"],
            }
        )

        verified = verify_legacy_report_evidence_run(**report)
        self.assertEqual(verified.terminal_result.summary, legacy_summary)

        report["run"]["parameters"]["report_snapshot_v2"] = {}
        with self.assertRaisesRegex(SealedRunIntegrityError, "modern"):
            verify_legacy_report_evidence_run(**report)


if __name__ == "__main__":
    unittest.main()
