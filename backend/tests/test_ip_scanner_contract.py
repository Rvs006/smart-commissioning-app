"""Golden-fixture contract test for the ip_scanner sidecar adapter.

This is the upstream re-import gate: it pins the driving contract between the SCT
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

from app.schemas.jobs import (
    DiscoveryAssetObservation,
    DiscoveryResultsResponse,
)
from smart_commissioning_core.engines.ip_scanner_sidecar import (
    _RAG_SEVERITY,
    REGISTER_TEMPLATE_COLUMNS,
    _map_result,
    _register_csv,
    _scan_query,
    register_rows_from_devices,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENDOR_DIR = _REPO_ROOT / "scanners" / "vendor" / "network-ip-scanner"
_SERVER_JS = (_VENDOR_DIR / "server.js").read_text(encoding="utf-8")
_SCANNER_JS = (_VENDOR_DIR / "scanner.js").read_text(encoding="utf-8")
_APP_JS = (_VENDOR_DIR / "public" / "app.js").read_text(encoding="utf-8")
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
        # missing (unreachable) -> issue + an observation row; the other three
        # additionally become devices.
        self.assertEqual({r["address"] for r in result.structured_records}, {"10.0.0.1", "10.0.0.2", "10.0.0.4"})
        # green raises no issue; amber + two reds do.
        self.assertEqual(len(result.issues), 3)


class RegisterRowVisibilityTest(unittest.TestCase):
    """Field report: after uploading a register the results table showed no RAG
    at all. The verdict was persisted on the structured device only, and an
    expected-but-silent device was an issue with no row anywhere, so an operator
    could not see it. Every compare() row must reach discovered_assets."""

    ROWS = [
        {"ip": "10.0.0.1", "register": "match", "rag": "green", "status": "reachable",
         "hostname": "ok-host", "openPorts": [80], "expectedPorts": [80]},
        {"ip": "10.0.0.2", "register": "partial", "rag": "amber", "status": "reachable",
         "openPorts": [80], "expectedPorts": [80, 443]},
        {"ip": "10.0.0.3", "register": "missing", "rag": "red", "status": "unreachable",
         "hostname": "expected-host", "expectedHostname": "expected-host",
         "openPorts": [], "expectedPorts": [443]},
        {"ip": "10.0.0.4", "register": "rogue", "rag": "red", "status": "rogue", "openPorts": [23]},
    ]

    def _assets(self) -> dict[str, object]:
        result = _map_result(self.ROWS, {}, {"project_id": "p", "site_id": "s"})
        return {a["ip_address"]: a for a in result.discovered_assets}

    def test_every_compare_row_reaches_discovered_assets(self) -> None:
        self.assertEqual(
            set(self._assets()), {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
        )

    def test_rag_and_register_stamped_on_every_asset(self) -> None:
        assets = self._assets()
        for row in self.ROWS:
            asset = assets[row["ip"]]
            self.assertEqual(asset["rag"], row["rag"])
            self.assertEqual(asset["register"], row["register"])

    def test_missing_asset_claims_nothing_it_did_not_observe(self) -> None:
        missing = self._assets()["10.0.0.3"]
        self.assertEqual(missing["register"], "missing")
        self.assertEqual(missing["rag"], "red")
        self.assertEqual(missing["observed_ports"], [])
        self.assertEqual(missing["match_basis"], "register")
        self.assertEqual(missing["status_detail"], "unreachable/missing")
        # Never seen, so no MAC, no asset id, and no last-seen timestamp.
        self.assertIsNone(missing["last_seen_at"])
        self.assertIsNone(missing["mac_address"])
        self.assertIsNone(missing["asset_id"])
        # The register's expected hostname must NOT be laundered into the
        # observed field: nothing resolved it. It rides as expected_hostname.
        self.assertIsNone(missing["hostname"])
        self.assertEqual(missing["expected_hostname"], "expected-host")

    def test_silent_rows_are_stamped_on_the_run_summary(self) -> None:
        extra = _map_result(
            self.ROWS, {}, {"start_ip": "10.0.0.1", "end_ip": "10.0.0.50"}
        ).result_summary_extra
        self.assertEqual(
            extra["expected_not_responding"],
            [
                {
                    "asset_id": None,
                    "asset_name": "expected-host",
                    "address": "10.0.0.3",
                    "expected_ports": [443],
                    # Inside the swept range, so the sweep really pinged it.
                    "directed_probe_sent": True,
                }
            ],
        )

    def test_probe_claim_is_computed_from_the_swept_range(self) -> None:
        # The vendored sweep pings every address between start and end
        # (scanner.js "Pass 1: ping sweep"), so a register host outside that
        # range was never probed and the report must not claim it was.
        rows = [
            {"ip": "10.0.0.3", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
            {"ip": "198.51.100.7", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
            # A register row with no usable address: unknowable, never guessed.
            {"ip": "\u2014", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
        ]
        extra = _map_result(
            rows, {}, {"start_ip": "10.0.0.1", "end_ip": "10.0.0.50"}
        ).result_summary_extra
        self.assertEqual(
            [entry["directed_probe_sent"] for entry in extra["expected_not_responding"]],
            [True, False, None],
        )

    def test_probe_claim_is_none_without_a_recorded_range(self) -> None:
        extra = _map_result(self.ROWS, {}, {}).result_summary_extra
        self.assertIsNone(extra["expected_not_responding"][0]["directed_probe_sent"])

    def test_single_address_scan_counts_only_that_address_as_probed(self) -> None:
        # No end bound: the sidecar sweeps the start address alone.
        rows = [
            {"ip": "10.0.0.1", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
            {"ip": "10.0.0.2", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
        ]
        extra = _map_result(rows, {}, {"start_ip": "10.0.0.1"}).result_summary_extra
        self.assertEqual(
            [entry["directed_probe_sent"] for entry in extra["expected_not_responding"]],
            [True, False],
        )

    def test_expected_not_responding_is_always_stamped(self) -> None:
        # Empty list, never an absent key: a consumer must be able to tell "no
        # expected host was silent" from "this run predates the field".
        reachable_only = [
            {"ip": "10.0.0.1", "register": "match", "rag": "green",
             "status": "reachable", "openPorts": []},
        ]
        extra = _map_result(reachable_only, {}, {}).result_summary_extra
        self.assertEqual(extra["expected_not_responding"], [])

    def test_missing_asset_parses_against_the_readback_schema(self) -> None:
        DiscoveryAssetObservation(**self._assets()["10.0.0.3"])

    def test_the_observation_row_carries_the_same_probe_claim_as_the_report(self) -> None:
        # The screen reads directed_probe_sent off the OBSERVATION; the signed
        # report reads it off result_summary.expected_not_responding. Stamping
        # only the report left every silent host on screen saying "Probe sent:
        # Not recorded for this run" while the engine had already worked it out.
        result = _map_result(self.ROWS, {}, {"start_ip": "10.0.0.1", "end_ip": "10.0.0.50"})
        missing = next(a for a in result.discovered_assets if a["register"] == "missing")
        reported = result.result_summary_extra["expected_not_responding"][0]
        self.assertIs(missing["directed_probe_sent"], True)
        self.assertIs(missing["directed_probe_sent"], reported["directed_probe_sent"])
        # extra="allow" has to carry it through the readback schema, or the field
        # is dropped between the engine and the page.
        parsed = DiscoveryAssetObservation(**missing)
        self.assertIs(parsed.model_dump()["directed_probe_sent"], True)

    def test_the_observation_row_reports_false_and_none_too(self) -> None:
        # All three answers travel, not just the true one: False is "we swept a
        # range and this address was not in it", None is "we cannot tell".
        rows = [
            {"ip": "10.0.0.3", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
            {"ip": "198.51.100.7", "register": "missing", "rag": "red",
             "status": "unreachable", "openPorts": []},
        ]
        in_range = _map_result(rows, {}, {"start_ip": "10.0.0.1", "end_ip": "10.0.0.50"})
        self.assertEqual(
            [a["directed_probe_sent"] for a in in_range.discovered_assets],
            [True, False],
        )
        no_range = _map_result(rows, {}, {})
        self.assertEqual(
            [a["directed_probe_sent"] for a in no_range.discovered_assets],
            [None, None],
        )
        for asset in in_range.discovered_assets + no_range.discovered_assets:
            DiscoveryAssetObservation(**asset)

    def test_summary_carries_all_six_register_counters(self) -> None:
        summary = {"expected": 3, "reachable": 3, "expectedReachable": 2,
                   "matches": 1, "partial": 1, "missing": 1, "rogue": 1}
        extra = _map_result(self.ROWS, summary, {}).result_summary_extra
        self.assertEqual(extra["register_expected"], 3)
        self.assertEqual(extra["hosts_scanned"], 3)
        self.assertEqual(extra["register_matches"], 1)
        self.assertEqual(extra["register_partial"], 1)
        self.assertEqual(extra["register_missing"], 1)
        self.assertEqual(extra["register_rogue"], 1)


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


class SaveAsRegisterContractTest(unittest.TestCase):
    def test_vendored_save_as_register_logic_pinned(self) -> None:
        # The parity source: if upstream changes the filter or the ports join,
        # our projection must be revisited -> pin the exact literals.
        self.assertIn("function saveAsRegister", _APP_JS)
        self.assertIn("r.status === 'reachable' || r.status === 'rogue'", _APP_JS)
        self.assertIn("(r.openPorts || []).join(';')", _APP_JS)

    def test_projection_rows_match_golden_columns_and_join_ports(self) -> None:
        devices = [
            {"address": "192.0.2.10", "name": "h-a", "vendor": "Acme", "model": "X1",
             "device_type": "server", "attributes": {"open_ports": [80, 443], "hostname_status": "match",
                                                      "project": "P1", "location": "Rack 1", "description": "web"}},
            {"address": "192.0.2.99", "name": None, "device_type": "ip_host",
             "attributes": {"open_ports": [23], "register": "rogue", "status": "rogue"}},
        ]
        rows = register_rows_from_devices(devices)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), set(GOLDEN_REGISTER_COLUMNS))
        self.assertEqual(rows[0]["Expected Ports"], "80;443")
        self.assertEqual(rows[0]["Type"], "server")
        # A rogue/plain host carries the neutral ip_host type -> blank Type.
        self.assertEqual(rows[1]["Type"], "")
        self.assertEqual(rows[1]["IP Address"], "192.0.2.99")

    def test_projection_drops_unreachable_and_blanks_unverified_hostname(self) -> None:
        devices = [
            {"address": "192.0.2.11", "name": "expected-b", "device_type": "ip_host",
             "attributes": {"open_ports": [], "hostname_status": "unverified"}},
            {"address": "192.0.2.12", "name": None, "device_type": "ip_host",
             "attributes": {"register": "missing", "status": "unreachable", "open_ports": []}},
        ]
        rows = register_rows_from_devices(devices)
        self.assertEqual(len(rows), 1)  # unreachable/missing dropped
        # An unverified (never-observed) hostname is blanked, never laundered in.
        self.assertEqual(rows[0]["Hostname"], "")
        self.assertEqual(rows[0]["Expected Ports"], "")

    def test_projection_rows_accepted_by_import_profile(self) -> None:
        # Deferred import (module-level would bind reports/discovery engines under
        # unittest); PROFILES itself is pure. Proves create_import accepts 100% of
        # projected rows, so the saved register round-trips.
        from app.services.import_service import PROFILES

        devices = [
            {"address": "192.0.2.10", "name": "h-a", "vendor": "Acme", "model": "X1",
             "device_type": "server", "attributes": {"open_ports": [80, 443], "hostname_status": "match"}},
        ]
        profile = PROFILES["ip_scanner_register"]
        for number, row in enumerate(register_rows_from_devices(devices), start=2):
            self.assertEqual(profile.validate_row(row, number), [])

    def test_projection_roundtrips_through_register_csv(self) -> None:
        devices = [{"address": "10.0.0.5", "name": "h", "device_type": "ip_host",
                    "attributes": {"open_ports": [502]}}]
        csv_text = _register_csv(register_rows_from_devices(devices))
        self.assertEqual(csv_text.splitlines()[0], ",".join(GOLDEN_REGISTER_COLUMNS))
        self.assertIn("10.0.0.5", csv_text)
        self.assertIn("502", csv_text)


class ObservedPortReadbackContractTest(unittest.TestCase):
    """The readback gap that shipped: the mapper emitted raw int ports, so
    GET /runs/{id}/results (response_model=DiscoveryResultsResponse) raised a
    ValidationError coercing ints into ObservedPort. Pin the object shape at the
    exact seam that broke."""

    def _sidecar_assets(self) -> list[dict[str, object]]:
        rows = [
            {"ip": "192.0.2.10", "register": "match", "rag": "green", "status": "reachable",
             "hostname": "h-a", "openPorts": [80, 443], "expectedPorts": [80, 443]},
            {"ip": "192.0.2.20", "register": "rogue", "rag": "red", "status": "rogue",
             "openPorts": [47808], "openUdp": [47808]},
        ]
        return _map_result(rows, {}, {"project_id": "p", "site_id": "s"}).discovered_assets

    def test_every_asset_parses_against_readback_schema(self) -> None:
        for asset in self._sidecar_assets():
            DiscoveryAssetObservation(**asset)  # would raise ValidationError pre-fix

    def test_observed_ports_are_port_protocol_objects(self) -> None:
        assets = {a["ip_address"]: a for a in self._sidecar_assets()}
        self.assertEqual(
            assets["192.0.2.10"]["observed_ports"][0], {"port": 80, "protocol": "tcp"}
        )
        udp = assets["192.0.2.20"]["observed_ports"]
        self.assertEqual(udp, [{"port": 47808, "protocol": "udp"}])

    def test_full_results_response_accepts_sidecar_output(self) -> None:
        # The literal readback path: coerce the persisted summary into the
        # GET-results response model. Raised 500 pre-fix; must be clean now.
        response = DiscoveryResultsResponse(
            run_id="run_1",
            job_type="ip_scanner",
            status="succeeded",
            discovered_assets=self._sidecar_assets(),
        )
        self.assertEqual(response.discovered_assets[0].observed_ports[0].protocol, "tcp")


class ScanQueryContractTest(unittest.TestCase):
    def test_start_ip_conventions(self) -> None:
        self.assertEqual(_scan_query({"start_ip": "10.0.0.1"})["start"], "10.0.0.1")
        self.assertEqual(_scan_query({"start": "10.0.0.9"})["start"], "10.0.0.9")

    def test_end_optional(self) -> None:
        self.assertNotIn("end", _scan_query({"start": "10.0.0.1"}))


if __name__ == "__main__":
    unittest.main()
