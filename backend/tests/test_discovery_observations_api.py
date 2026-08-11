import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta

from harness import ApiTestCase
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.discovery_observations import DiscoveryObservationInputV1
from smart_commissioning_core.run_context import RunContextV1

_API_KEY = "test-discovery-observations-api-key"


def _context() -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": "demo-project",
            "site_id": "demo-site",
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [],
            "schema_versions": {},
            "engine_parameters": {"authorized": True, "dry_run": False},
            "network_interface": "192.0.2.10/24",
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "test-observation-api",
            "application_version": "0.1.41",
        }
    )


class DiscoveryObservationApiTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        super().setUp()
        from app.core.db import get_engine

        self.lifecycle = RunLifecycleRepository(get_engine())
        self.run_id, self.lease = self._create_claimed_run()

    def _create_envelope(self):
        envelope = self.lifecycle.create_run_with_context(
            job_type="ip_discovery",
            context=_context(),
            execution_mode="dramatiq_worker",
        )
        return envelope

    def _create_claimed_run(self):
        envelope = self._create_envelope()
        lease = self.lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            lease_seconds=60,
            owner_token="observation-api-owner",
        )
        assert lease is not None
        return envelope.run_id, lease

    @contextmanager
    def _tamper_terminal_evidence(
        self,
        run_id: str,
        mutate: Callable[[dict], None],
    ) -> Iterator[None]:
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Run, RunResult, RunSeal
        from smart_commissioning_core.integrity import sha256_bytes
        from smart_commissioning_core.run_context import canonical_json_bytes

        with session_factory(get_engine()).begin() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            seal = session.get(RunSeal, run_id)
            original_result_payload = deepcopy(result.result_payload)
            original_result_summary = deepcopy(result.summary)
            original_result_sha256 = result.result_sha256
            original_run_summary = deepcopy(run.result_summary)
            original_run_sha256 = run.result_sha256
            original_seal_sha256 = seal.result_sha256
            payload = deepcopy(result.result_payload)
            mutate(payload["summary"])
            digest = sha256_bytes(canonical_json_bytes(payload))
            result.result_payload = payload
            result.summary = dict(payload["summary"])
            result.result_sha256 = digest
            run.result_summary = dict(payload["summary"])
            run.result_sha256 = digest
            seal.result_sha256 = digest

        try:
            yield
        finally:
            with session_factory(get_engine()).begin() as session:
                run = session.get(Run, run_id)
                result = session.get(RunResult, run_id)
                seal = session.get(RunSeal, run_id)
                result.result_payload = deepcopy(original_result_payload)
                result.summary = deepcopy(original_result_summary)
                result.result_sha256 = original_result_sha256
                run.result_summary = deepcopy(original_run_summary)
                run.result_sha256 = original_run_sha256
                seal.result_sha256 = original_seal_sha256

    def _append(self, *, version: int, event_key: str, name: str) -> int:
        outcome = self.lifecycle.append_discovery_observation(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="host",
                entity_key="host:192.0.2.25",
                entity_version=version,
                event_key=event_key,
                phase="reachability",
                outcome="observed",
                payload_schema_version="1.0",
                payload={
                    "projection_v1": {
                        "collection": "devices",
                        "record": {
                            "address": "192.0.2.25",
                            "name": name,
                            "attributes": {"responded": version > 1},
                        },
                    }
                },
            ),
        )
        return outcome.cursor

    def test_cursor_page_is_bounded_additive_and_query_only(self) -> None:
        from app.services.run_service import RunService
        from sqlalchemy import event

        first_cursor = self._append(version=1, event_key="host-25-v1", name="pending")
        second_cursor = self._append(version=2, event_key="host-25-v2", name="AHU-25")
        statements: list[str] = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.strip().upper())

        engine = RunService().engine
        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            response = self.client.get(
                f"/api/v1/discovery/runs/{self.run_id}/observations",
                params={"after": 0, "limit": 1},
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["run_id"], self.run_id)
        self.assertEqual(body["attempt"], self.lease.attempt)
        self.assertEqual(len(body["observations"]), 1)
        self.assertEqual(body["observations"][0]["cursor"], first_cursor)
        self.assertEqual(body["next_cursor"], first_cursor)
        self.assertEqual(body["latest_cursor"], second_cursor)
        self.assertTrue(body["has_more"])
        self.assertIsNone(body["terminal"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(
            any(statement.startswith("BEGIN IMMEDIATE") for statement in statements),
            statements,
        )

        second_page = self.client.get(
            f"/api/v1/discovery/runs/{self.run_id}/observations",
            params={"after": first_cursor, "limit": 1000},
        )
        self.assertEqual(second_page.status_code, 200, second_page.text)
        second_body = second_page.json()
        self.assertEqual([row["cursor"] for row in second_body["observations"]], [second_cursor])
        self.assertFalse(second_body["has_more"])

    def test_attempt_zero_queued_run_has_a_defined_empty_page(self) -> None:
        envelope = self._create_envelope()

        response = self.client.get(f"/api/v1/discovery/runs/{envelope.run_id}/observations")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "run_id": envelope.run_id,
                "attempt": 1,
                "observations": [],
                "next_cursor": 0,
                "latest_cursor": 0,
                "has_more": False,
                "observations_pruned": False,
                "observations_quarantined": False,
                "terminal": None,
            },
        )

    def test_cancel_before_claim_has_a_defined_empty_terminal_page(self) -> None:
        envelope = self._create_envelope()
        cancelled = self.lifecycle.request_cancel(envelope.run_id)
        self.assertTrue(cancelled.changed)

        response = self.client.get(f"/api/v1/discovery/runs/{envelope.run_id}/observations")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["attempt"], 1)
        self.assertEqual(body["observations"], [])
        self.assertEqual(body["latest_cursor"], 0)
        self.assertEqual(
            body["terminal"],
            {"status": "cancelled", "terminal_cursor": 0},
        )

    def test_nonterminal_marker_skips_terminal_verification_queries(self) -> None:
        from app.services.run_service import RunService
        from sqlalchemy import event

        statements: list[str] = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.upper())

        service = RunService()
        event.listen(service.engine, "before_cursor_execute", record_statement)
        try:
            marker = service.get_terminal_observation_marker_read_only(self.run_id)
        finally:
            event.remove(service.engine, "before_cursor_execute", record_statement)

        self.assertIsNone(marker)
        executed_sql = "\n".join(statements)
        self.assertNotIn("RUN_EXECUTION_CONTEXTS", executed_sql)
        self.assertNotIn("RUN_DISPATCH_OUTBOX", executed_sql)

    def test_sealed_marker_skips_the_legacy_dispatch_probe(self) -> None:
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {"status": "succeeded", "stage": "complete"},
        )
        self.assertTrue(outcome.applied)

        from app.services.run_service import RunService
        from sqlalchemy import event

        statements: list[str] = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.upper())

        service = RunService()
        event.listen(service.engine, "before_cursor_execute", record_statement)
        try:
            marker = service.get_terminal_observation_marker_read_only(self.run_id)
        finally:
            event.remove(service.engine, "before_cursor_execute", record_statement)

        self.assertEqual(marker["status"], "succeeded")
        executed_sql = "\n".join(statements)
        self.assertIn("RUN_EXECUTION_CONTEXTS", executed_sql)
        self.assertNotIn("RUN_DISPATCH_OUTBOX", executed_sql)

    def test_terminal_marker_requires_the_exact_executor_attempt(self) -> None:
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {"status": "succeeded", "stage": "complete"},
        )
        self.assertTrue(outcome.applied)
        from app.services.run_service import RunService

        with self._tamper_terminal_evidence(
            self.run_id,
            lambda summary: summary["observation_evidence_v1"].update({"attempt": self.lease.attempt + 1}),
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                RunService().get_terminal_observation_marker_read_only(self.run_id)

    def test_terminal_marker_rejects_inconsistent_count_and_cursor(self) -> None:
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {"status": "succeeded", "stage": "complete"},
        )
        self.assertTrue(outcome.applied)
        from app.services.run_service import RunService

        with self._tamper_terminal_evidence(
            self.run_id,
            lambda summary: summary["observation_evidence_v1"].update({"observation_count": 1, "terminal_cursor": 0}),
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                RunService().get_terminal_observation_marker_read_only(self.run_id)

    def test_modern_terminal_without_observation_evidence_is_not_legacy(self) -> None:
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {"status": "succeeded", "stage": "complete"},
        )
        self.assertTrue(outcome.applied)
        from app.services.run_service import RunService
        from sqlalchemy import event

        statements: list[str] = []

        def record_statement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.upper())

        service = RunService()
        event.listen(service.engine, "before_cursor_execute", record_statement)
        try:
            with self._tamper_terminal_evidence(
                self.run_id,
                lambda summary: summary.pop("observation_evidence_v1"),
            ):
                with self.assertRaisesRegex(RuntimeError, "evidence is missing"):
                    service.get_terminal_observation_marker_read_only(self.run_id)
        finally:
            event.remove(service.engine, "before_cursor_execute", record_statement)

        self.assertNotIn("RUN_DISPATCH_OUTBOX", "\n".join(statements))

    def test_page_reports_the_sealed_terminal_cursor(self) -> None:
        terminal_cursor = self._append(
            version=1,
            event_key="host-25-final",
            name="AHU-25",
        )
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {
                "status": "succeeded",
                "stage": "engine_complete",
                "summary": {"targets": 1},
            },
        )
        self.assertTrue(outcome.applied)

        response = self.client.get(
            f"/api/v1/discovery/runs/{self.run_id}/observations",
            params={"after": terminal_cursor},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["observations"], [])
        self.assertEqual(
            body["terminal"],
            {"status": "succeeded", "terminal_cursor": terminal_cursor},
        )
        self.assertFalse(body["has_more"])
        self.assertFalse(body["observations_pruned"])
        self.assertFalse(body["observations_quarantined"])

    def test_pruned_terminal_history_crosses_to_sealed_results_without_looping(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import RunDiscoveryObservation
        from sqlalchemy import delete

        terminal_cursor = self._append(
            version=1,
            event_key="host-25-retained-until-expiry",
            name="AHU-25",
        )
        outcome = self.lifecycle.finalize_discovery_run(
            self.run_id,
            self.lease.owner_token,
            self.lease.attempt,
            {
                "status": "succeeded",
                "stage": "engine_complete",
                "summary": {"targets": 1},
            },
        )
        self.assertTrue(outcome.applied)
        with session_factory(get_engine()).begin() as session:
            session.execute(delete(RunDiscoveryObservation).where(RunDiscoveryObservation.run_id == self.run_id))

        response = self.client.get(
            f"/api/v1/discovery/runs/{self.run_id}/observations",
            params={"after": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["observations"], [])
        self.assertEqual(body["latest_cursor"], terminal_cursor)
        self.assertFalse(body["has_more"])
        self.assertTrue(body["observations_pruned"])
        self.assertFalse(body["observations_quarantined"])
        self.assertEqual(body["terminal"]["terminal_cursor"], terminal_cursor)

    def test_quarantined_terminal_history_is_not_reported_as_retention_expiry(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import RunDiscoveryObservation
        from sqlalchemy import update

        terminal_cursor = self._append(
            version=1,
            event_key="host-25-corrupt-before-expiry",
            name="AHU-25",
        )
        with session_factory(get_engine()).begin() as session:
            session.execute(
                update(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == self.run_id)
                .values(payload_sha256="0" * 64)
            )
        recovered = self.lifecycle.recover_expired_leases(
            now=self.lease.lease_expires_at + timedelta(seconds=1),
        )
        self.assertIn(self.run_id, recovered)

        response = self.client.get(
            f"/api/v1/discovery/runs/{self.run_id}/observations",
            params={"after": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["latest_cursor"], terminal_cursor)
        self.assertEqual(body["observations"], [])
        self.assertTrue(body["observations_quarantined"])
        self.assertFalse(body["observations_pruned"])
        self.assertFalse(body["has_more"])

    def test_tampered_page_payload_hash_fails_closed_without_disclosing_the_row(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import RunDiscoveryObservation
        from sqlalchemy import update

        self._append(
            version=1,
            event_key="host-25-page-integrity",
            name="AHU-25",
        )
        with session_factory(get_engine()).begin() as session:
            session.execute(
                update(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == self.run_id)
                .values(
                    payload={
                        "projection_v1": {
                            "collection": "devices",
                            "record": {
                                "address": "192.0.2.25",
                                "name": "must-not-leak",
                                "attributes": {"responded": False},
                            },
                        }
                    }
                )
            )

        response = self.client.get(
            f"/api/v1/discovery/runs/{self.run_id}/observations",
            params={"after": 0},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json(),
            {"detail": "Stored discovery observation evidence failed integrity verification."},
        )
        self.assertNotIn("must-not-leak", response.text)
        self.assertNotIn("payload_digest_mismatch", response.text)

    def test_foreign_or_missing_run_is_concealed(self) -> None:
        missing = self.client.get("/api/v1/discovery/runs/run_missing_observations/observations")
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
