"""Unit tests for the BACnet<->MQTT mapping comparison engine.

All canned data + in-memory FakeRunStore. NO real BACnet/MQTT transport — the
comparison is deterministic and fully exercised here.
"""

import unittest
from typing import Any

from smart_commissioning_core.engines.comparison import (
    process_mapping_validation_run,
    validate_mapping,
)


def _mapping(
    *,
    asset_id: str = "AHU-1",
    bacnet_name: str = "supply_air_temperature",
    mqtt_field: str = "pointset.points.supply_air_temperature_sensor.present_value",
    bacnet_units: str = "degrees-celsius",
    mqtt_units: str = "degrees-celsius",
    tolerance: str = "0.5",
    required: str = "required",
    topic: str = "site/ahu/events/pointset",
) -> dict[str, Any]:
    return {
        "Asset ID": asset_id,
        "BACnet object name": bacnet_name,
        "BACnet units": bacnet_units,
        "MQTT topic": topic,
        "MQTT field/path": mqtt_field,
        "MQTT units": mqtt_units,
        "Tolerance": tolerance,
        "Mapping required flag": required,
    }


def _bacnet(name: str, value: Any, *, units: str = "degrees-celsius") -> dict[str, Any]:
    return {"point_name": name, "observed_value": {"value": value}, "units": units}


def _mqtt(field: str, value: Any) -> dict[str, Any]:
    return {"field": field, "observed_value": {"value": value}}


class FakeRunStore:
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


class MatchTests(unittest.TestCase):
    def test_exact_match_zero_issues(self) -> None:
        mapping = [_mapping(tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", 21.0)]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.0)]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["total"], 1)

    def test_value_just_inside_tolerance_is_ok(self) -> None:
        mapping = [_mapping(tolerance="0.5")]
        bacnet = [_bacnet("supply_air_temperature", 21.0)]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.4)]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)

    def test_value_just_outside_tolerance_emits_out_of_tolerance(self) -> None:
        mapping = [_mapping(tolerance="0.5")]
        bacnet = [_bacnet("supply_air_temperature", 21.0)]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.6)]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "out_of_tolerance")
        self.assertEqual(issues[0].match_basis, "absolute")
        self.assertEqual(summary["out_of_tolerance"], 1)

    def test_percent_tolerance_from_tolerances_register(self) -> None:
        # Mapping declares no row tolerance; per-point tolerances register supplies 10%.
        mapping = [_mapping(tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", 100.0, units="cfm")]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 108.0)]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "supply_air_temperature", "Tolerance": "10%"}]
        issues, summary = validate_mapping(
            mapping_rows=mapping,
            bacnet_observed_rows=bacnet,
            mqtt_observed_rows=mqtt,
            tolerance_rows=tolerances,
        )
        self.assertEqual(issues, [], "108 within 10% of 100")
        self.assertEqual(summary["ok"], 1)

    def test_percent_tolerance_outside(self) -> None:
        mapping = [_mapping(tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", 100.0, units="cfm")]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 115.0)]
        tolerances = [{"Asset ID": "AHU-1", "Point name": "supply_air_temperature", "Tolerance": "10%"}]
        issues, summary = validate_mapping(
            mapping_rows=mapping,
            bacnet_observed_rows=bacnet,
            mqtt_observed_rows=mqtt,
            tolerance_rows=tolerances,
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].match_basis, "percent")
        self.assertEqual(summary["out_of_tolerance"], 1)


class MissingTests(unittest.TestCase):
    def test_missing_bacnet_source(self) -> None:
        mapping = [_mapping()]
        bacnet: list[dict[str, Any]] = []
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.0)]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "missing_bacnet_source")
        self.assertEqual(issues[0].severity, "high")
        self.assertEqual(summary["missing_bacnet"], 1)

    def test_missing_mqtt_target(self) -> None:
        mapping = [_mapping()]
        bacnet = [_bacnet("supply_air_temperature", 21.0)]
        mqtt: list[dict[str, Any]] = []
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "missing_mqtt_target")
        self.assertEqual(summary["missing_mqtt"], 1)

    def test_missing_both_sides_optional_is_low_severity(self) -> None:
        mapping = [_mapping(required="optional")]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=[], mqtt_observed_rows=[]
        )
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(i.severity == "low" for i in issues))
        self.assertEqual(summary["missing_bacnet"], 1)
        self.assertEqual(summary["missing_mqtt"], 1)


class UnitAndValueTests(unittest.TestCase):
    def test_unit_mismatch(self) -> None:
        mapping = [_mapping(bacnet_units="degrees-celsius", mqtt_units="percent", tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", 21.0)]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.0)]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        # unit mismatch only (values equal => no value issue)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "unit_mismatch")
        self.assertEqual(summary["unit_mismatch"], 1)
        self.assertEqual(summary["ok"], 0)

    def test_non_numeric_value_mismatch(self) -> None:
        mapping = [_mapping(tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", "ON")]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", "OFF")]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].issue_type, "value_mismatch")
        self.assertEqual(issues[0].match_basis, "exact")
        self.assertEqual(summary["value_mismatch"], 1)

    def test_non_numeric_value_match_is_ok(self) -> None:
        mapping = [_mapping(tolerance="")]
        bacnet = [_bacnet("supply_air_temperature", "ON")]
        mqtt = [_mqtt("pointset.points.supply_air_temperature_sensor.present_value", "ON")]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(issues, [])
        self.assertEqual(summary["ok"], 1)


class CountsTests(unittest.TestCase):
    def test_counts_across_mixed_register(self) -> None:
        mapping = [
            _mapping(bacnet_name="ok", mqtt_field="ok_field", tolerance="0.5"),
            _mapping(bacnet_name="oot", mqtt_field="oot_field", tolerance="0.5"),
            _mapping(bacnet_name="gone_b", mqtt_field="gone_b_field", tolerance="0.5"),
        ]
        bacnet = [
            _bacnet("ok", 10.0),
            _bacnet("oot", 10.0),
            # gone_b has no bacnet observation
        ]
        mqtt = [
            _mqtt("ok_field", 10.2),
            _mqtt("oot_field", 99.0),
            _mqtt("gone_b_field", 5.0),
        ]
        issues, summary = validate_mapping(
            mapping_rows=mapping, bacnet_observed_rows=bacnet, mqtt_observed_rows=mqtt
        )
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["out_of_tolerance"], 1)
        self.assertEqual(summary["missing_bacnet"], 1)
        self.assertEqual(summary["missing_mqtt"], 0)
        self.assertEqual(summary["issue_count"], 2)


class CancellationTests(unittest.TestCase):
    def test_cancellation_stops_mid_register(self) -> None:
        mapping = [
            _mapping(bacnet_name=f"b{i}", mqtt_field=f"f{i}", tolerance="0.5")
            for i in range(500)
        ]
        bacnet = [_bacnet(f"b{i}", 1.0) for i in range(500)]
        mqtt = [_mqtt(f"f{i}", 1.0) for i in range(500)]
        issues, summary = validate_mapping(
            mapping_rows=mapping,
            bacnet_observed_rows=bacnet,
            mqtt_observed_rows=mqtt,
            is_cancelled=lambda: True,
        )
        self.assertTrue(summary["cancelled"])
        self.assertLess(summary["mappings_processed"], 500)


class ProcessorTests(unittest.TestCase):
    def test_process_run_inline_succeeds(self) -> None:
        store = FakeRunStore()
        parameters = {
            "mapping_rows": [_mapping(tolerance="0.5")],
            "bacnet_observed": [_bacnet("supply_air_temperature", 21.0)],
            "mqtt_observed": [
                _mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.3)
            ],
        }
        result = process_mapping_validation_run(
            "run_map_1",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.record_summary["ok"], 1)
        self.assertEqual(store.issues, [])

    def test_process_run_uses_injected_loaders(self) -> None:
        store = FakeRunStore()
        import_ids: list[str] = []
        discovery_ids: list[str] = []

        def import_loader(import_id: str):
            import_ids.append(import_id)
            if import_id == "imp_map":
                return [_mapping(tolerance="0.5")]
            return []

        def discovery_loader(run_id: str):
            discovery_ids.append(run_id)
            if run_id == "bacnet_run":
                return [_bacnet("supply_air_temperature", 21.0)]
            if run_id == "mqtt_run":
                return [
                    _mqtt("pointset.points.supply_air_temperature_sensor.present_value", 21.2)
                ]
            return []

        parameters = {
            "mapping_import_id": "imp_map",
            "bacnet_discovery_run_id": "bacnet_run",
            "mqtt_discovery_run_id": "mqtt_run",
        }
        result = process_mapping_validation_run(
            "run_map_2",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
            import_loader=import_loader,
            discovery_loader=discovery_loader,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.record_summary["ok"], 1)
        self.assertIn("imp_map", import_ids)
        self.assertIn("bacnet_run", discovery_ids)
        self.assertIn("mqtt_run", discovery_ids)

    def test_process_run_cancelled(self) -> None:
        store = FakeRunStore()
        parameters = {
            "mapping_rows": [
                _mapping(bacnet_name=f"b{i}", mqtt_field=f"f{i}") for i in range(500)
            ],
            "bacnet_observed": [_bacnet(f"b{i}", 1.0) for i in range(500)],
            "mqtt_observed": [_mqtt(f"f{i}", 1.0) for i in range(500)],
        }
        result = process_mapping_validation_run(
            "run_map_3",
            parameters,
            run_store=store,
            execution_mode="inline_local_fallback",
            is_cancelled=lambda: True,
        )
        self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
