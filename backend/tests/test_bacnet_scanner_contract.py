"""Golden-fixture contract test for the bacnet_scanner sidecar adapter.

The BACnet analogue of ``test_ip_scanner_contract``: it pins the driving
contract between the SCT adapter and the vendored ``bacnet-scanner`` Node app.
If the vendored app changes its health version, I-Am device fields, per-asset
export JSON shape, RAG/register vocabulary, SSE event names, or its 9-column
register template — or if the adapter drifts from them — this suite fails,
forcing a deliberate re-import rather than a silent break.

stdlib ``unittest`` only (CI runs unittest, not pytest). No live Node process:
the vendored source files and template are read from disk as the golden truth.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from smart_commissioning_core.engines.bacnet_scanner_sidecar import (
    _RAG_SEVERITY,
    REGISTER_TEMPLATE_COLUMNS,
    _decode_export_zip,
    _fold_router_event,
    _map_result,
    _register_csv,
    _routers_from_fold,
    _scan_query,
    map_browse_objects,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_DIR = _REPO_ROOT / "scanners" / "vendor" / "bacnet-scanner"
_SERVER_JS = (_VENDOR_DIR / "server.js").read_text(encoding="utf-8")
_BACNET_JS = (_VENDOR_DIR / "bacnet.js").read_text(encoding="utf-8")
_TEMPLATE_CSV = (_VENDOR_DIR / "template" / "bacnet-device-register-template.csv").read_text(encoding="utf-8")

# --- Golden constants copied verbatim from the driving contract -------------

GOLDEN_HEALTH_VERSION = "1.0.0"

# The identity record an I-Am builds in bacnet.js discover().
GOLDEN_IAM_FIELDS = {
    "instance", "ip", "port", "network", "mac", "maxApdu",
    "segmentation", "segmentationCode", "vendorId", "vendor",
}

# The per-asset export JSON built by buildDeviceFiles() in server.js.
GOLDEN_ASSET_FIELDS = {
    "asset", "deviceInstance", "address", "network", "vendor", "model",
    "firmware", "objectCount", "pointsExported", "truncated", "exportedAt", "points",
}
GOLDEN_POINT_FIELDS = {"objectType", "objectInstance", "name", "presentValue", "units"}

GOLDEN_RAG = {"green", "amber", "red", "none"}
GOLDEN_REGISTER = {"match", "partial", "missing", "rogue", "none"}
GOLDEN_STATUS = {"reachable", "unreachable", "rogue"}

GOLDEN_SCAN_SSE_TYPES = {
    "start", "device", "router", "discovered", "device-update", "progress", "result", "complete", "error",
}
GOLDEN_EXPORT_SSE_TYPES = {"device", "objprogress", "device-done", "packaging", "ready", "error"}
# The /api/objects (on-demand per-device browse) SSE contract.
GOLDEN_OBJECTS_SSE_TYPES = {"progress", "objects", "complete", "error"}
GOLDEN_OBJECTS_QUERY_PARAMS = ("instance", "ip", "port", "network", "mac", "cap", "timeout")

GOLDEN_REGISTER_COLUMNS = (
    "Device Instance", "Device Name", "Network", "IP Address", "Vendor",
    "Model", "Location", "Expected Objects", "Description",
)


class HealthContractTest(unittest.TestCase):
    def test_health_version_literal(self) -> None:
        self.assertIn(f"version: '{GOLDEN_HEALTH_VERSION}'", _SERVER_JS)


class IamDeviceContractTest(unittest.TestCase):
    def test_every_iam_field_present_in_source(self) -> None:
        for field in GOLDEN_IAM_FIELDS:
            self.assertIn(field, _BACNET_JS, f"I-Am field '{field}' missing from bacnet.js")

    def test_adapter_maps_reachable_row_to_device(self) -> None:
        rows = [{
            "instance": 1001, "register": "match", "rag": "green", "status": "reachable",
            "ip": "10.0.0.11", "name": "AHU-1", "vendor": "Acme", "model": "V1", "firmware": "1.2",
        }]
        result = _map_result(rows, {}, [], {"project_id": "p", "site_id": "s"})
        devices = [r for r in result.structured_records if "device_ref" not in r]
        self.assertEqual(len(devices), 1)
        record = devices[0]
        allowed = {"project_id", "site_id", "address", "device_type", "name", "vendor", "model", "attributes"}
        self.assertLessEqual(set(record), allowed)
        self.assertEqual(record["address"], "10.0.0.11")
        self.assertEqual(record["device_type"], "bacnet_device")
        self.assertEqual(record["attributes"]["asset_id"], "bacnet-device-1001")
        self.assertEqual(record["attributes"]["register_state"], "match")


class ExportShapeContractTest(unittest.TestCase):
    def test_per_asset_json_fields_present_in_source(self) -> None:
        # Plain substring: the vendored object literal uses shorthand for some
        # keys (``exportedAt,``, ``points,``), so a "field:" match would miss them.
        for field in GOLDEN_ASSET_FIELDS:
            self.assertIn(field, _SERVER_JS, f"export asset field '{field}' missing from server.js")

    def test_point_fields_present_in_source(self) -> None:
        for field in GOLDEN_POINT_FIELDS:
            self.assertIn(field, _SERVER_JS, f"export point field '{field}' missing from server.js")

    def test_adapter_maps_export_points_to_point_rows(self) -> None:
        device_files = [{
            "deviceInstance": 1001,
            "points": [
                {"objectType": "analog-input", "objectInstance": 3, "name": "SAT",
                 "presentValue": "18.60", "units": "degreesCelsius"},
            ],
        }]
        result = _map_result([], {}, device_files, {})
        points = [r for r in result.structured_records if "device_ref" in r]
        self.assertEqual(len(points), 1)
        point = points[0]
        self.assertEqual(point["device_ref"], "bacnet-device-1001")
        self.assertEqual(point["point_id"], "analog-input-3")
        self.assertEqual(point["point_name"], "SAT")
        # observed_value is a JSON object wrapping the json-safe scalar.
        self.assertEqual(point["observed_value"], {"value": "18.60"})
        self.assertEqual(point["units"], "degreesCelsius")
        self.assertEqual(point["attributes"]["object_type"], "analog-input")


class RagVocabularyContractTest(unittest.TestCase):
    def test_rag_tokens_present_in_source(self) -> None:
        for token in GOLDEN_RAG:
            self.assertIn(f"'{token}'", _SERVER_JS, f"rag token '{token}' missing from server.js")

    def test_register_tokens_present_in_source(self) -> None:
        for token in GOLDEN_REGISTER:
            self.assertIn(f"'{token}'", _SERVER_JS, f"register token '{token}' missing from server.js")

    def test_status_tokens_present_in_source(self) -> None:
        for token in GOLDEN_STATUS:
            self.assertIn(f"'{token}'", _SERVER_JS, f"status token '{token}' missing from server.js")

    def test_adapter_severity_keys_are_rag_tokens(self) -> None:
        self.assertTrue(set(_RAG_SEVERITY).issubset(GOLDEN_RAG))
        self.assertEqual(set(_RAG_SEVERITY), {"red", "amber"})

    def test_adapter_projects_full_vocabulary(self) -> None:
        rows = [
            {"instance": 1, "register": "match", "rag": "green", "status": "reachable", "ip": "10.0.0.1"},
            {"instance": 2, "register": "partial", "rag": "amber", "status": "reachable", "ip": "10.0.0.2"},
            {"instance": 9, "register": "missing", "rag": "red", "status": "unreachable", "ip": "—"},
            {"instance": 5, "register": "rogue", "rag": "red", "status": "rogue", "ip": "10.0.0.5"},
        ]
        result = _map_result(rows, {}, [], {})
        devices = [r for r in result.structured_records if "device_ref" not in r]
        # missing (unreachable) -> issue only; the other three -> devices.
        self.assertEqual({d["address"] for d in devices}, {"10.0.0.1", "10.0.0.2", "10.0.0.5"})
        # green raises no issue; amber + two reds do.
        self.assertEqual(len(result.issues), 3)


class SseEventContractTest(unittest.TestCase):
    def _assert_emitted(self, name: str) -> None:
        self.assertTrue(
            f"type: '{name}'" in _SERVER_JS or f"type:'{name}'" in _SERVER_JS,
            f"SSE event type '{name}' is not emitted by the vendored app",
        )

    def test_every_scan_sse_type_emitted_in_source(self) -> None:
        for name in GOLDEN_SCAN_SSE_TYPES:
            self._assert_emitted(name)

    def test_every_export_sse_type_emitted_in_source(self) -> None:
        for name in GOLDEN_EXPORT_SSE_TYPES:
            self._assert_emitted(name)


class RouterVisibilityContractTest(unittest.TestCase):
    def test_router_event_shape_in_source(self) -> None:
        # Pin the exact emit the adapter's _fold_router_event decodes. If upstream
        # renames router/networks the fold silently drops routers -> this catches it.
        self.assertIn("type: 'router', router: r.ip, networks: r.networks", _SERVER_JS)

    def test_adapter_folds_router_events_per_ip_with_sorted_networks(self) -> None:
        fold: dict[str, list[int]] = {}
        _fold_router_event(fold, {"type": "router", "router": "192.0.2.9", "networks": [300, 200]})
        _fold_router_event(fold, {"type": "router", "router": "192.0.2.9", "networks": [200, 400]})
        _fold_router_event(fold, {"type": "router", "router": "", "networks": [1]})  # blank ip skipped
        self.assertEqual(
            _routers_from_fold(fold),
            [{"address": "192.0.2.9", "networks": [200, 300, 400]}],
        )

    def test_adapter_projects_routers_into_summary_only(self) -> None:
        routers = [{"address": "192.0.2.9", "networks": [200, 300]}]
        result = _map_result([], {}, [], {}, routers=routers)
        self.assertEqual(result.result_summary_extra["routers"], routers)
        # A router is never a device/point/issue -- it must not enter the tables
        # replace_devices owns.
        self.assertEqual(result.structured_records, [])
        self.assertEqual(result.discovered_assets, [])
        self.assertEqual(result.issues, [])

    def test_map_result_stamps_empty_router_list_when_none_heard(self) -> None:
        # The key is always present so the report/UI can tell "none responded"
        # (empty list) from a pre-router run (absent key).
        self.assertEqual(_map_result([], {}, [], {}).result_summary_extra["routers"], [])


class ObjectBrowseContractTest(unittest.TestCase):
    def test_objects_endpoint_present_in_source(self) -> None:
        self.assertIn("p === '/api/objects'", _SERVER_JS)

    def test_every_objects_sse_type_emitted_in_source(self) -> None:
        for name in GOLDEN_OBJECTS_SSE_TYPES:
            self.assertTrue(
                f"type: '{name}'" in _SERVER_JS or f"type:'{name}'" in _SERVER_JS,
                f"objects SSE type '{name}' is not emitted by the vendored app",
            )

    def test_objects_query_params_read_in_source(self) -> None:
        for param in GOLDEN_OBJECTS_QUERY_PARAMS:
            self.assertIn(
                f"searchParams.get('{param}')", _SERVER_JS,
                f"/api/objects no longer reads query param '{param}'",
            )

    def test_object_result_envelope_in_source(self) -> None:
        # readObjectList returns {objects, count, truncated, error}; map_browse_objects
        # depends on that envelope and on the per-object camelCase field names.
        self.assertIn("truncated: total > limit", _BACNET_JS)
        for field in ("typeName", "presentValue"):
            self.assertIn(field, _BACNET_JS, f"object field '{field}' missing from bacnet.js")

    def test_map_browse_objects_snake_cases_the_golden_shape(self) -> None:
        result = map_browse_objects(
            {
                "type": "objects",
                "objects": [
                    {"type": 0, "typeName": "analog-input", "instance": 3, "name": "SAT",
                     "presentValue": "18.60", "units": "degreesCelsius"},
                    {"typeName": "no-type-or-instance"},  # malformed -> skipped
                ],
                "count": 42,
                "truncated": True,
                "error": None,
            }
        )
        self.assertEqual(result["count"], 42)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["objects"]), 1)
        self.assertEqual(
            result["objects"][0],
            {
                "type": 0, "type_name": "analog-input", "instance": 3,
                "name": "SAT", "present_value": "18.60", "units": "degreesCelsius",
            },
        )

    def test_map_browse_objects_passes_no_answer_error_through_honestly(self) -> None:
        sentence = "Device did not answer the object-list read (no response after retries)."
        # The exact wording the operator will read is owned by the sidecar; pin it.
        self.assertIn(sentence, _BACNET_JS)
        result = map_browse_objects({"type": "objects", "objects": [], "count": 0, "error": sentence})
        # A no-answer read is an honest empty result carrying the device's error,
        # never fabricated rows.
        self.assertEqual(result["objects"], [])
        self.assertEqual(result["error"], sentence)


class RegisterTemplateContractTest(unittest.TestCase):
    def test_template_header_matches_golden_nine_columns(self) -> None:
        header = _TEMPLATE_CSV.splitlines()[0].split(",")
        self.assertEqual(tuple(h.strip() for h in header), GOLDEN_REGISTER_COLUMNS)

    def test_adapter_constant_matches_golden(self) -> None:
        self.assertEqual(REGISTER_TEMPLATE_COLUMNS, GOLDEN_REGISTER_COLUMNS)
        self.assertEqual(len(REGISTER_TEMPLATE_COLUMNS), 9)

    def test_device_instance_is_the_match_key(self) -> None:
        # Device Instance is column 0 and the sidecar keys compare() on it.
        self.assertEqual(REGISTER_TEMPLATE_COLUMNS[0], "Device Instance")
        self.assertIn("'device instance'", _SERVER_JS)

    def test_register_csv_roundtrips_columns(self) -> None:
        csv_text = _register_csv([{"Device Instance": "1001", "Device Name": "AHU-1"}])
        self.assertEqual(csv_text.splitlines()[0], ",".join(GOLDEN_REGISTER_COLUMNS))
        self.assertIn("1001", csv_text)


class ExportZipDecodeTest(unittest.TestCase):
    def test_decode_reads_json_entries_and_survives_corruption(self) -> None:
        import base64
        import io
        import json
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AHU-1_1001/AHU-1_1001.json", json.dumps({"deviceInstance": 1001, "points": []}))
            archive.writestr("AHU-1_1001/AHU-1_1001.xlsx", b"not json")  # non-json entries ignored
        assets = _decode_export_zip(base64.b64encode(buffer.getvalue()).decode("ascii"))
        self.assertEqual(assets, [{"deviceInstance": 1001, "points": []}])
        # A corrupt archive is honest "no points", never a raise.
        self.assertEqual(_decode_export_zip("not-base64!!"), [])


class ScanQueryContractTest(unittest.TestCase):
    def test_optional_range_passed_through(self) -> None:
        self.assertEqual(_scan_query({"low": 1, "high": 100}), {"low": "1", "high": "100"})

    def test_blank_range_is_global_who_is(self) -> None:
        self.assertEqual(_scan_query({}), {})


if __name__ == "__main__":
    unittest.main()
