"""Golden-fixture contract test for the ip_scanner sidecar adapter.

This is the Pete re-import gate: it pins the driving contract between the SCT
adapter and the vendored ``network-ip-scanner`` Node app. If the vendored app
changes its health version, device-record fields, RAG/register vocabulary, SSE
event names, or its 9-column register template — or if the adapter drifts from
them — this suite fails, forcing a deliberate re-import rather than a silent
break.

stdlib ``unittest`` only (CI runs unittest, not pytest). No live Node process:
the vendored source files and template are read from disk as the golden truth.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from smart_commissioning_core.engines.ip_scanner_sidecar import (
    _RAG_SEVERITY,
    REGISTER_TEMPLATE_COLUMNS,
    _map_result,
    _register_csv,
    _scan_query,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_DIR = _REPO_ROOT / "scanners" / "vendor" / "network-ip-scanner"
_SERVER_JS = (_VENDOR_DIR / "server.js").read_text(encoding="utf-8")
_SCANNER_JS = (_VENDOR_DIR / "scanner.js").read_text(encoding="utf-8")
_TEMPLATE_CSV = (_VENDOR_DIR / "template" / "ip-device-register-template.csv").read_text(encoding="utf-8")

# --- Golden constants copied verbatim from the driving contract -------------

GOLDEN_HEALTH_VERSION = "1.0.0"

GOLDEN_DEVICE_FIELDS = {
    "ip", "hostname", "hostnameSrc", "mac", "vendor", "alive", "latency",
    "openPorts", "openTcp", "openUdp", "services", "banner", "discoveredBy",
}

GOLDEN_RAG = {"green", "amber", "red", "none"}
GOLDEN_REGISTER = {"match", "partial", "missing", "rogue", "none"}

GOLDEN_SSE_TYPES = {"start", "progress", "discovered", "device", "result", "complete", "error"}

GOLDEN_REGISTER_COLUMNS = (
    "IP Address", "Hostname", "Type", "Vendor", "Model",
    "Expected Ports", "Project", "Location", "Description",
)


class HealthContractTest(unittest.TestCase):
    def test_health_version_literal(self) -> None:
        # The vendored /api/health returns a hardcoded version string.
        self.assertIn(f"version: '{GOLDEN_HEALTH_VERSION}'", _SERVER_JS)


class DeviceRecordContractTest(unittest.TestCase):
    def test_every_golden_device_field_present_in_source(self) -> None:
        for field in GOLDEN_DEVICE_FIELDS:
            self.assertIn(field, _SCANNER_JS, f"device field '{field}' missing from scanner.js")

    def test_adapter_maps_reachable_row_to_device(self) -> None:
        rows = [{
            "ip": "192.0.2.10", "register": "match", "rag": "green",
            "status": "reachable", "hostname": "h", "openPorts": [80], "expectedPorts": [80],
        }]
        result = _map_result(rows, {}, {"project_id": "p", "site_id": "s"})
        self.assertEqual(len(result.structured_records), 1)
        record = result.structured_records[0]
        # Only the fixed device columns + attributes may appear as top-level keys.
        allowed = {"project_id", "site_id", "address", "device_type", "name", "vendor", "model", "attributes"}
        self.assertLessEqual(set(record), allowed)
        self.assertEqual(record["address"], "192.0.2.10")


class RagVocabularyContractTest(unittest.TestCase):
    def test_rag_tokens_present_in_source(self) -> None:
        for token in GOLDEN_RAG:
            self.assertIn(f"'{token}'", _SERVER_JS, f"rag token '{token}' missing from server.js")

    def test_register_tokens_present_in_source(self) -> None:
        for token in GOLDEN_REGISTER:
            self.assertIn(f"'{token}'", _SERVER_JS, f"register token '{token}' missing from server.js")

    def test_adapter_severity_keys_are_rag_tokens(self) -> None:
        # The adapter only raises issues for known RAG tokens.
        self.assertTrue(set(_RAG_SEVERITY).issubset(GOLDEN_RAG))
        self.assertEqual(set(_RAG_SEVERITY), {"red", "amber"})

    def test_adapter_projects_full_vocabulary(self) -> None:
        rows = [
            {"ip": "10.0.0.1", "register": "match", "rag": "green", "status": "reachable", "openPorts": []},
            {"ip": "10.0.0.2", "register": "partial", "rag": "amber", "status": "reachable", "openPorts": []},
            {"ip": "10.0.0.3", "register": "missing", "rag": "red", "status": "unreachable", "openPorts": []},
            {"ip": "10.0.0.4", "register": "rogue", "rag": "red", "status": "rogue", "openPorts": [23]},
        ]
        result = _map_result(rows, {}, {})
        # missing (unreachable) -> issue only; the other three -> devices.
        self.assertEqual({r["address"] for r in result.structured_records}, {"10.0.0.1", "10.0.0.2", "10.0.0.4"})
        # green raises no issue; amber + two reds do.
        self.assertEqual(len(result.issues), 3)


class SseEventContractTest(unittest.TestCase):
    def test_every_golden_sse_type_emitted_in_source(self) -> None:
        combined = _SERVER_JS + _SCANNER_JS
        for name in GOLDEN_SSE_TYPES:
            self.assertTrue(
                f"type: '{name}'" in combined or f"type:'{name}'" in combined,
                f"SSE event type '{name}' is not emitted by the vendored app",
            )


class RegisterTemplateContractTest(unittest.TestCase):
    def test_template_header_matches_golden_nine_columns(self) -> None:
        header = _TEMPLATE_CSV.splitlines()[0].split(",")
        self.assertEqual(tuple(h.strip() for h in header), GOLDEN_REGISTER_COLUMNS)

    def test_adapter_constant_matches_golden(self) -> None:
        self.assertEqual(REGISTER_TEMPLATE_COLUMNS, GOLDEN_REGISTER_COLUMNS)
        self.assertEqual(len(REGISTER_TEMPLATE_COLUMNS), 9)

    def test_import_profile_columns_match_template(self) -> None:
        # The SCT import profile must expose exactly the sidecar's 9 columns so
        # accepted rows re-serialize with no field translation.
        from app.services.import_service import PROFILES

        profile = PROFILES["ip_scanner_register"]
        columns = tuple(profile.required_columns) + tuple(profile.optional_columns)
        self.assertEqual(columns, GOLDEN_REGISTER_COLUMNS)

    def test_register_csv_roundtrips_columns(self) -> None:
        csv_text = _register_csv([{"IP Address": "192.0.2.1", "Expected Ports": "80;443"}])
        self.assertEqual(csv_text.splitlines()[0], ",".join(GOLDEN_REGISTER_COLUMNS))
        self.assertIn("192.0.2.1", csv_text)


class ScanQueryContractTest(unittest.TestCase):
    def test_start_ip_conventions(self) -> None:
        self.assertEqual(_scan_query({"start_ip": "10.0.0.1"})["start"], "10.0.0.1")
        self.assertEqual(_scan_query({"start": "10.0.0.9"})["start"], "10.0.0.9")

    def test_end_optional(self) -> None:
        self.assertNotIn("end", _scan_query({"start": "10.0.0.1"}))


if __name__ == "__main__":
    unittest.main()
