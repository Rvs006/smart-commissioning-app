"""Unit tests for the BACnet point validation engine.

Everything here runs against canned expected/observed/tolerance data and an
in-memory FakeRunStore. There is NO real BACnet device or network involved —
the engine is a deterministic comparison, so this test suite exercises it fully.
"""

import unittest
from typing import Any

from smart_commissioning_core.engines.point_validation import (
    process_bacnet_validation_run,
    validate_bacnet_points,
)


def _expected(
    point_name: str,
    *,
    asset_id: str = "AHU-1",
    units: str = "degrees-celsius",
    value_type: str = "number",
    required: str = "required",
    expected_value: Any = None,
) -> dict[str, Any]:
    row = {
        "Asset ID": asset_id,
        "Expected point name": point_name,
        "Expected units": units,
        "Expected value type": value_type,
        "Required/optional flag": required,
    }
    if expected_value is not None:
        row["Expected value"] = expected_value
    return row


def _observed(
    point_name: str,
    value: Any,
    *,
    units: str = "degrees-celsius",
    device_ref: str = "AHU-1",
) -> dict[str, Any]:
    return {
        "point_name": point_name,
        "device_ref": device_ref,
        "observed_value": {"value": value},
        "units": units,
    }


class FakeRunStore:
    """Minimal in-memory RunStore capturing the wrapper's calls."""

    def __init__(self) -> None:
        self.record_summary: dict[str, Any] = {}
        self.issues: list[Any] = []
        self.last_status: str | None = None

    def update_run_status(self, run_id, *, status, stage=None, progress_percent=None, error_message=None):
        self.last_status = status
        return {
            "run_id": run_id,
            "status": status,
            "stage": stage,
            "progress_percent": progress_percent,
            "error_message": error_message,
            "result_summary": dict(self.record_summary),
        }

    def update_result_summary(self, run_id, result_summary, *, merge=True):
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id, issues):
        self.issues = list(issues)
        return {"run_id": run_id, "issues": list(issues)}


class ExactMatchTests(unittest.TestCase):
    def test_exact_match_no_value_declared_zero_issues(self) -> None:
        expected = [_expected("supply_air_temp")]
        observed = [_observed("supply_air_temp", 21.0)]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["unexpected"], 0)
        self.assertEqual(summary["total"], 1)

    def test_exact_value_match_zero_issues(self) -> None:
        expected = [_expected("setpoint", expected_value="21.0")]
        observed = [_observed("setpoint", 21.0)]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)


class ToleranceTests(unittest.TestCase):
    def test_value_just_inside_absolute_tolerance_is_ok(self) -> None:
        expected = [_expected("temp", expected_value="20.0")]
        observed = [_observed("temp", 20.4)]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "temp", "Tolerance": "0.5"}]
        issues, summary = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(issues, [])
        self.assertEqual(summary["out_of_tolerance"], 0)
        self.assertEqual(summary["ok"], 1)

    def test_value_at_exact_absolute_tolerance_boundary_is_ok(self) -> None:
        expected = [_expected("temp", expected_value="20.0")]
        observed = [_observed("temp", 20.5)]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "temp", "Tolerance": "0.5"}]
        issues, _ = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(issues, [], "boundary value (<= tolerance) must be accepted")

    def test_value_just_outside_absolute_tolerance_emits_issue(self) -> None:
        expected = [_expected("temp", expected_value="20.0")]
        observed = [_observed("temp", 20.6)]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "temp", "Tolerance": "0.5"}]
        issues, summary = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "out_of_tolerance")
        self.assertEqual(issues[0].match_basis, "absolute")
        self.assertEqual(summary["out_of_tolerance"], 1)
        self.assertEqual(summary["ok"], 0)

    def test_percent_tolerance_inside_is_ok(self) -> None:
        # 5% of 100 = 5.0 allowed band; 104 is inside.
        expected = [_expected("flow", units="cfm", expected_value="100")]
        observed = [_observed("flow", 104, units="cfm")]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "flow", "Tolerance": "5%"}]
        issues, summary = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)

    def test_percent_tolerance_outside_emits_issue(self) -> None:
        # 5% of 100 = 5.0; 106 is outside.
        expected = [_expected("flow", units="cfm", expected_value="100")]
        observed = [_observed("flow", 106, units="cfm")]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "flow", "Tolerance": "5%"}]
        issues, summary = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "out_of_tolerance")
        self.assertEqual(issues[0].match_basis, "percent")
        self.assertEqual(summary["out_of_tolerance"], 1)

    def test_per_type_tolerance_applies_when_no_per_point(self) -> None:
        # A per-type tolerance (point name "type:number") covers a point that
        # has no per-point tolerance row.
        expected = [_expected("temp", expected_value="20.0")]
        observed = [_observed("temp", 20.4)]
        tolerances = [{"Asset ID": "", "Point name": "type:number", "Tolerance": "0.5"}]
        issues, summary = validate_bacnet_points(
            expected_rows=expected, observed_rows=observed, tolerance_rows=tolerances
        )
        self.assertEqual(issues, [], "per-type tolerance should make 20.4 vs 20.0 acceptable")
        self.assertEqual(summary["ok"], 1)

    def test_no_tolerance_requires_exact_numeric_match(self) -> None:
        expected = [_expected("temp", expected_value="20.0")]
        observed = [_observed("temp", 20.1)]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "out_of_tolerance")
        self.assertEqual(issues[0].match_basis, "exact")


class MissingUnexpectedTests(unittest.TestCase):
    def test_missing_required_point_is_high_severity(self) -> None:
        expected = [_expected("missing_pt", required="required")]
        observed: list[dict[str, Any]] = []
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "missing_point")
        self.assertEqual(issues[0].severity, "high")
        self.assertEqual(summary["missing"], 1)

    def test_missing_optional_point_is_low_severity(self) -> None:
        expected = [_expected("opt_pt", required="optional")]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=[])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "low")
        self.assertEqual(summary["missing"], 1)

    def test_unexpected_observed_point_emits_issue(self) -> None:
        expected = [_expected("known")]
        observed = [_observed("known", 21.0), _observed("rogue", 99.0)]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "unexpected_point")
        self.assertEqual(issues[0].point_name, "rogue")
        self.assertEqual(summary["unexpected"], 1)
        self.assertEqual(summary["ok"], 1)


class TypeAndUnitTests(unittest.TestCase):
    def test_numeric_expected_but_non_numeric_observed_is_type_mismatch(self) -> None:
        expected = [_expected("temp", value_type="number", expected_value="20.0")]
        observed = [_observed("temp", "FAULT")]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "value_type_mismatch")
        self.assertEqual(summary["type_mismatch"], 1)

    def test_unit_mismatch_emits_issue(self) -> None:
        expected = [_expected("temp", units="degrees-celsius")]
        observed = [_observed("temp", 21.0, units="percent")]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "unit_mismatch")
        self.assertEqual(summary["unit_mismatch"], 1)

    def test_underscore_vs_hyphen_units_do_not_mismatch(self) -> None:
        expected = [_expected("temp", units="degrees-celsius")]
        observed = [_observed("temp", 21.0, units="degrees_celsius")]
        issues, _ = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(issues, [], "normalised units should match")

    def test_present_value_key_is_extracted(self) -> None:
        # observed_value may use the UDMI-style present_value key.
        expected = [_expected("temp", expected_value="20.0")]
        observed = [
            {
                "point_name": "temp",
                "device_ref": "AHU-1",
                "observed_value": {"present_value": 20.0},
                "units": "degrees-celsius",
            }
        ]
        issues, _ = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(issues, [])


class CountsTests(unittest.TestCase):
    def test_counts_are_correct_across_mixed_register(self) -> None:
        expected = [
            _expected("ok_pt", expected_value="10.0"),
            _expected("missing_pt"),
            _expected("oot_pt", expected_value="10.0"),
        ]
        observed = [
            _observed("ok_pt", 10.0),
            _observed("oot_pt", 50.0),
            _observed("extra_pt", 1.0),
        ]
        issues, summary = validate_bacnet_points(expected_rows=expected, observed_rows=observed)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["out_of_tolerance"], 1)
        self.assertEqual(summary["unexpected"], 1)
        self.assertEqual(summary["issue_count"], 3)  # missing + oot + unexpected


class CancellationTests(unittest.TestCase):
    def test_cancellation_stops_mid_register(self) -> None:
        # Build a register large enough to span multiple cancel-check chunks.
        expected = [_expected(f"pt_{i}") for i in range(500)]
        observed = [_observed(f"pt_{i}", 1.0) for i in range(500)]

        calls = {"n": 0}

        def is_cancelled() -> bool:
            calls["n"] += 1
            # Cancel on the first chunk-boundary check (index 200).
            return calls["n"] >= 1

        issues, summary = validate_bacnet_points(
            expected_rows=expected,
            observed_rows=observed,
            is_cancelled=is_cancelled,
        )
        self.assertTrue(summary["cancelled"])
        self.assertLess(summary["expected_processed"], 500, "must stop before processing all rows")
        # When cancelled mid-register the unexpected pass is skipped (so we do
        # not spuriously flag the unprocessed observed points).
        self.assertEqual(summary["unexpected"], 0)


class ProcessorTests(unittest.TestCase):
    def test_process_run_inline_succeeds_and_writes_summary(self) -> None:
        store = FakeRunStore()
        parameters = {
            "expected_points": [_expected("temp", expected_value="20.0")],
            "observed_points": [_observed("temp", 20.0)],
        }
        result = process_bacnet_validation_run(
            "run_bpv_1",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.last_status, "succeeded")
        self.assertEqual(store.record_summary["ok"], 1)
        self.assertEqual(store.record_summary["execution_mode"], "inline_local_fallback")
        self.assertEqual(store.issues, [])

    def test_process_run_uses_injected_loaders(self) -> None:
        store = FakeRunStore()
        import_calls: list[str] = []
        discovery_calls: list[str] = []

        def import_loader(import_id: str):
            import_calls.append(import_id)
            if import_id == "imp_expected":
                return [_expected("temp", expected_value="20.0")]
            if import_id == "imp_tol":
                return [{"Asset ID": "AHU-1", "Point name": "temp", "Tolerance": "1.0"}]
            return []

        def discovery_loader(run_id: str):
            discovery_calls.append(run_id)
            return [_observed("temp", 20.5)]

        parameters = {
            "import_id": "imp_expected",
            "tolerances_import_id": "imp_tol",
            "discovery_run_id": "disc_1",
        }
        result = process_bacnet_validation_run(
            "run_bpv_2",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
            import_loader=import_loader,
            discovery_loader=discovery_loader,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.record_summary["ok"], 1)  # 20.5 within +-1.0
        self.assertIn("imp_expected", import_calls)
        self.assertIn("imp_tol", import_calls)
        self.assertIn("disc_1", discovery_calls)

    def test_process_run_cancelled_sets_cancelled_status(self) -> None:
        store = FakeRunStore()
        parameters = {
            "expected_points": [_expected(f"pt_{i}") for i in range(500)],
            "observed_points": [_observed(f"pt_{i}", 1.0) for i in range(500)],
        }
        result = process_bacnet_validation_run(
            "run_bpv_3",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
            is_cancelled=lambda: True,
        )
        self.assertEqual(result["status"], "cancelled")

    def test_engine_performs_no_io_no_authorization_required(self) -> None:
        # Sanity: with empty parameters and no loaders, the engine just produces
        # an empty result and succeeds — it never tries to reach a network.
        store = FakeRunStore()
        result = process_bacnet_validation_run(
            "run_bpv_4",
            {},
            run_store=store,
            execution_mode="inline_local_fallback",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.record_summary["total"], 0)


if __name__ == "__main__":
    unittest.main()
