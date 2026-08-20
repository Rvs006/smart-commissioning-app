"""Golden-fixture contract test for the mqtt_scanner sidecar adapter.

The upstream re-import gate for the MQTT lane: it pins the driving contract between
the SCT adapter and the vendored ``mqtt-discovery`` Node app. If the vendored app
changes its health version, export manifest shape, register columns, match keys,
or its connect/status/export endpoint vocabulary — or if the adapter drifts from
them — this suite fails, forcing a deliberate re-import rather than a silent break.

stdlib ``unittest`` only (CI runs unittest, not pytest). No live Node process:
the vendored source files and template are read from disk as the golden truth.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from smart_commissioning_core.engines.mqtt_scanner_sidecar import (
    MAX_CAPTURE_SECONDS,
    REGISTER_TEMPLATE_COLUMNS,
    _as_payload_dict,
    _asset_key,
    _capture_seconds,
    _map_manifest,
    _norm_point,
    _register_csv,
    _root_filter,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_DIR = _REPO_ROOT / "scanners" / "vendor" / "mqtt-discovery"
_SERVER_JS = (_VENDOR_DIR / "server.js").read_text(encoding="utf-8")
_UDMI_JS = (_VENDOR_DIR / "udmi.js").read_text(encoding="utf-8")
_TEMPLATE_CSV = (_VENDOR_DIR / "template" / "mqtt-register-template.csv").read_text(encoding="utf-8")

# --- Golden constants copied verbatim from the driving contract -------------

GOLDEN_HEALTH_VERSION = "2.0.0"

# _manifest.json top-level keys (buildExportZip, server.js).
GOLDEN_MANIFEST_KEYS = {"tool", "version", "exportedAt", "broker", "assetCount", "topicCount", "assets"}
# per-asset keys in manifest.assets[].
GOLDEN_MANIFEST_ASSET_KEYS = {"asset", "matched", "schema", "site", "room", "gatewayId", "topics", "points"}

GOLDEN_REGISTER_COLUMNS = (
    "Asset", "Topic", "Type", "Point", "Unit",
    "Data Type", "Schema", "Site", "Location", "Description",
)

# Endpoints the bounded read run drives (server.js router).
GOLDEN_ENDPOINTS = {"/api/health", "/api/connect", "/api/status", "/api/export-archive", "/api/disconnect", "/api/register"}


class HealthContractTest(unittest.TestCase):
    def test_health_version_literal(self) -> None:
        self.assertIn(f"version: '{GOLDEN_HEALTH_VERSION}'", _SERVER_JS)


class ManifestContractTest(unittest.TestCase):
    def test_manifest_top_level_keys_present_in_source(self) -> None:
        # The buildExportZip manifest literal must carry every key the adapter reads.
        for key in GOLDEN_MANIFEST_KEYS:
            self.assertIn(f"{key}:", _SERVER_JS, f"manifest key '{key}' missing from server.js")

    def test_manifest_asset_keys_present_in_source(self) -> None:
        for key in GOLDEN_MANIFEST_ASSET_KEYS:
            self.assertIn(f"{key}:", _SERVER_JS, f"manifest asset key '{key}' missing from server.js")

    def test_adapter_maps_manifest_asset_to_topic_record(self) -> None:
        manifest = {
            "assetCount": 1, "topicCount": 1,
            "assets": [{
                "asset": "AHU-01", "matched": True, "schema": "udmi-v2",
                "site": "S", "room": "Plant", "gatewayId": "GW",
                "topics": ["udmi/site/x/ahu/01/events/pointset"],
                "points": [{"name": "supply_air_temp", "value": 14.2, "unit": "degC"}],
            }],
        }
        payloads = {"udmi/site/x/ahu/01/events/pointset": {"raw": '{"version":"1"}', "history_count": 2}}
        result = _map_manifest(manifest, payloads, [], {"project_id": "p", "site_id": "s"})
        self.assertEqual(len(result.structured_records), 1)
        record = result.structured_records[0]
        self.assertEqual(set(record), {"topic", "message_count", "last_payload", "attributes"})
        self.assertEqual(record["topic"], "udmi/site/x/ahu/01/events/pointset")
        self.assertEqual(record["attributes"]["device_ref"], "AHU-01")
        self.assertEqual(record["attributes"]["schema"], "udmi-v2")


class LastPayloadDictContractTest(unittest.TestCase):
    def test_last_payload_is_always_a_dict(self) -> None:
        self.assertEqual(_as_payload_dict('{"a":1}'), {"a": 1})
        self.assertEqual(_as_payload_dict("42"), {"_value": 42})       # JSON scalar wrapped
        self.assertEqual(_as_payload_dict("[1,2]"), {"_value": [1, 2]})  # JSON list wrapped
        self.assertEqual(_as_payload_dict("not-json"), {"_raw_present": True})
        self.assertEqual(_as_payload_dict(None), {"_raw_present": True})


class ComparisonSemanticsContractTest(unittest.TestCase):
    def test_compare_functions_present_in_source(self) -> None:
        # The adapter recomputes matched/missing/extra; the vendored comparePoints
        # is the reference for that semantics + normPoint key.
        self.assertIn("function comparePoints", _UDMI_JS)
        self.assertIn("function normPoint", _UDMI_JS)
        for token in ("matched", "missing", "extra"):
            self.assertIn(token, _UDMI_JS, f"comparison token '{token}' missing from udmi.js")

    def test_matched_missing_extra_severities(self) -> None:
        manifest = {"assets": [{
            "asset": "AHU-01", "matched": True, "schema": "udmi-v2",
            "topics": ["t"], "points": [
                {"name": "supply_air_temp"},   # matched
                {"name": "rogue_point"},       # extra -> low
            ],
        }]}
        register_rows = [
            {"Asset": "AHU-01", "Point": "Supply Air Temp"},  # matched (normPoint)
            {"Asset": "AHU-01", "Point": "return_air_temp"},  # missing -> medium
            {"Asset": "PMP-07", "Point": "pump_speed"},       # asset absent -> high
        ]
        result = _map_manifest(manifest, {"t": {"raw": "{}", "history_count": 1}}, register_rows, {})
        by_type = {i.issue_type: i for i in result.issues}
        self.assertEqual(by_type["mqtt_scanner_missing_asset"].severity, "high")
        self.assertEqual(by_type["mqtt_scanner_missing_point"].severity, "medium")
        self.assertEqual(by_type["mqtt_scanner_extra_point"].severity, "low")


class MatchKeyContractTest(unittest.TestCase):
    def test_asset_key_is_uppercase_exact(self) -> None:
        # server.js: assetKey(a) = String(a||'').toUpperCase()
        self.assertIn("toUpperCase()", _SERVER_JS)
        self.assertEqual(_asset_key("ahu-01"), "AHU-01")

    def test_norm_point_matches_vendored_regex(self) -> None:
        # udmi.js normPoint: trim().toLowerCase().replace(/[\s\-]+/g,'_')
        self.assertIn("toLowerCase().replace(/[\\s\\-]+/g, '_')", _UDMI_JS)
        self.assertEqual(_norm_point("Supply Air Temp"), "supply_air_temp")
        self.assertEqual(_norm_point("supply-air-temp"), "supply_air_temp")


class RegisterTemplateContractTest(unittest.TestCase):
    def test_template_header_matches_golden_ten_columns(self) -> None:
        header = _TEMPLATE_CSV.splitlines()[0].split(",")
        self.assertEqual(tuple(h.strip() for h in header), GOLDEN_REGISTER_COLUMNS)

    def test_adapter_constant_matches_golden(self) -> None:
        self.assertEqual(REGISTER_TEMPLATE_COLUMNS, GOLDEN_REGISTER_COLUMNS)
        self.assertEqual(len(REGISTER_TEMPLATE_COLUMNS), 10)

    def test_generator_emits_the_same_ten_columns(self) -> None:
        # generateRegisterCsv (server.js) writes this exact header order.
        header_literal = "['Asset', 'Topic', 'Type', 'Point', 'Unit', 'Data Type', 'Schema', 'Site', 'Location', 'Description']"
        self.assertIn(header_literal, _SERVER_JS)

    def test_register_csv_roundtrips_columns(self) -> None:
        csv_text = _register_csv([{"Asset": "AHU-01", "Point": "supply_air_temp"}])
        self.assertEqual(csv_text.splitlines()[0], ",".join(GOLDEN_REGISTER_COLUMNS))
        self.assertIn("AHU-01", csv_text)


class EndpointVocabularyContractTest(unittest.TestCase):
    def test_every_driven_endpoint_present_in_source(self) -> None:
        for endpoint in GOLDEN_ENDPOINTS:
            self.assertIn(f"'{endpoint}'", _SERVER_JS, f"endpoint '{endpoint}' missing from server.js")

    def test_status_exposes_capture_counters(self) -> None:
        # The adapter polls stats during the window; these counters must exist.
        for counter in ("topicsDiscovered", "liveAssets", "totalMessages"):
            self.assertIn(counter, _SERVER_JS, f"status counter '{counter}' missing from server.js")

    def test_export_archive_409_on_empty(self) -> None:
        # An empty capture returns 409 "Nothing discovered yet" — the adapter reads
        # that as an honest empty capture, not a transport error.
        self.assertIn("Nothing discovered yet", _SERVER_JS)


class RunParameterSeamTest(unittest.TestCase):
    """Pin the frontend->adapter parameter spellings for the sidecar capture lane.

    The /mqtt-scanner capture panel sends ``topic_filter`` and ``capture_seconds``;
    this adapter's alias list and bounds are the other half of that seam. If either
    side renames or re-bounds, this fails instead of silently capturing '#'/60s.
    No HTTP, no DB.
    """

    def test_topic_filter_key_is_a_root_filter_alias(self) -> None:
        self.assertEqual(_root_filter({"topic_filter": "udmi/site/example/#"}), "udmi/site/example/#")

    def test_blank_or_absent_filter_captures_all_topics(self) -> None:
        self.assertEqual(_root_filter({}), "#")
        self.assertEqual(_root_filter({"topic_filter": "  "}), "#")

    def test_capture_seconds_bounds_are_the_sidecar_lane_contract(self) -> None:
        self.assertEqual(_capture_seconds({}), 60.0)  # absent -> default
        self.assertEqual(_capture_seconds({"capture_seconds": 300}), 300.0)
        # 0 is NOT an indefinite sentinel on this lane (unlike mqtt_discovery).
        self.assertEqual(_capture_seconds({"capture_seconds": 0}), 60.0)
        # The hard cap is the sidecar-lane safety ceiling.
        self.assertEqual(_capture_seconds({"capture_seconds": 3600}), MAX_CAPTURE_SECONDS)


if __name__ == "__main__":
    unittest.main()
