"""CSV import parser hardening (v0.1.11, handoff §3b).

field engineer's 2026-07-15 walkthrough: real registers come out of Excel, and Excel has
several perfectly ordinary saves that this parser used to answer with a 500, a
raw codec message, or "all 8 required columns are missing". These pin the
tolerant decode, the CR-only line endings, the delimiter/binary diagnostics,
and the two error messages an operator actually has to act on.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import import_service as import_service_module
from app.services.import_service import PROFILES, ImportService
from harness import ApiTestCase
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from sqlalchemy.engine import Engine

_HEADER = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol"
)
_ROW = "Site A,BMS,FCU-04,site/b1/fcu-04/#,1.5.2,supply_air_temp,degrees-celsius,60,MQTT"


def _temporary_engine(temp_dir: str) -> Engine:
    """Engine for a per-test SQLite database with the schema created."""
    engine = create_engine_from_url(default_sqlite_url(Path(temp_dir)))
    Base.metadata.create_all(engine)
    return engine


class ImportCsvDecodeTests(unittest.TestCase):
    """Service-level: which uploaded bytes parse, and how the rest are refused."""

    def _create(self, file_bytes: bytes) -> object:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = _temporary_engine(temp_dir)
            try:
                with mock.patch.object(import_service_module, "IMPORT_FILES_ROOT", Path(temp_dir)):
                    summary, _ = ImportService(engine=engine).create_import(
                        import_type="mqtt_register",
                        file_name="register.csv",
                        file_bytes=file_bytes,
                        project_id=None,
                        site_id=None,
                    )
            finally:
                engine.dispose()
        return summary

    def test_cr_only_line_endings_import(self) -> None:
        # Regression: classic Mac Excel saves CR-only. io.StringIO's universal
        # newline translation made csv raise csv.Error ("new-line character seen
        # in unquoted field"), which is not a ValueError, so the route's 400
        # handler missed it and the operator got a 500.
        summary = self._create((_HEADER + "\r" + _ROW + "\r").encode())

        self.assertEqual(summary.status, "accepted")
        self.assertEqual((summary.accepted_rows, summary.rejected_rows), (1, 0))

    def test_cp1252_save_imports(self) -> None:
        # Excel's plain "CSV (comma delimited)" writes Windows-1252, so any
        # accented character or en-dash used to fail the utf-8-sig decode and
        # surface the raw codec jargon as the 400 detail.
        row = _ROW.replace("BMS", "Façade – Plant")
        summary = self._create((_HEADER + "\n" + row + "\n").encode("cp1252"))

        self.assertEqual(summary.status, "accepted")
        self.assertEqual((summary.accepted_rows, summary.rejected_rows), (1, 0))

    def test_utf16_with_bom_imports(self) -> None:
        # Excel's "Unicode Text" save.
        summary = self._create((_HEADER + "\n" + _ROW + "\n").encode("utf-16"))

        self.assertEqual(summary.status, "accepted")
        self.assertEqual((summary.accepted_rows, summary.rejected_rows), (1, 0))

    def test_semicolon_delimited_save_names_the_real_problem(self) -> None:
        # Regional Excel. Previously the header parsed as ONE column and all 8
        # required columns were reported missing, which never pointed at the
        # delimiter.
        data = (_HEADER.replace(",", ";") + "\n" + _ROW.replace(",", ";") + "\n").encode()

        with self.assertRaises(ValueError) as caught:
            self._create(data)
        self.assertIn("comma-delimited", str(caught.exception))

    def test_tab_delimited_save_names_the_real_problem(self) -> None:
        data = (_HEADER.replace(",", "\t") + "\n" + _ROW.replace(",", "\t") + "\n").encode()

        with self.assertRaises(ValueError) as caught:
            self._create(data)
        self.assertIn("comma-delimited", str(caught.exception))

    def test_undecodable_bytes_are_refused_with_an_instruction(self) -> None:
        # 0x81/0x8D/0x8F/0x90/0x9D are unmapped in cp1252, so the fallback fails
        # too and we stop guessing.
        with self.assertRaises(ValueError) as caught:
            self._create(b"Project/site,System\n\x81\x8d\x8f,\x90\x9d\n")
        self.assertIn("Re-save", str(caught.exception))

    def test_bomless_utf16_is_refused_rather_than_parsed_as_garbage(self) -> None:
        # BOM-less UTF-16-LE ASCII is valid UTF-8 full of NULs: it decodes
        # "successfully" into garbage columns unless the NUL guard catches it.
        with self.assertRaises(ValueError) as caught:
            self._create((_HEADER + "\n" + _ROW + "\n").encode("utf-16-le"))
        self.assertIn("byte-order mark", str(caught.exception))

    def test_xlsx_renamed_to_csv_says_so(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._create(b"PK\x03\x04" + b"\x00" * 16)
        self.assertIn("XLSX", str(caught.exception))

    def test_oversize_field_is_a_value_error_not_a_500(self) -> None:
        # csv.Error (field larger than csv.field_size_limit, 131072) is not a
        # ValueError, so it has to be converted or it escapes as a 500.
        row = _ROW.replace("supply_air_temp", "x" * 140000)

        with self.assertRaises(ValueError) as caught:
            self._create((_HEADER + "\n" + row + "\n").encode())
        self.assertIn("could not be parsed", str(caught.exception))


class MqttRegisterMessageTests(unittest.TestCase):
    """Validator-level: the two rejections the reviewer could not act on.

    The reviewer spent much of the 2026-07-15 call editing GUID and Serial —
    neither of which is uniqueness-checked or validated at all — because no row
    ever said why it was rejected. These messages are what item 9a surfaces.
    """

    _BASE = {
        "Project/site": "Site A",
        "System": "BMS",
        "Asset ID": "FCU-04",
        "Expected topic": "site/b1/fcu-04/#",
        "Expected schema version": "1.5.2",
        "Expected points": "supply_air_temp",
        "Expected units": "degrees-celsius",
        "Expected reporting interval": "60",
        "Source protocol": "MQTT",
    }

    def _mqtt(self, **overrides: str) -> list:
        return PROFILES["mqtt_register"].validate_row({**self._BASE, **overrides}, 2)

    def test_excel_integral_decimal_interval_is_accepted(self) -> None:
        # "60.0" and "60" assert the same cadence; Excel decimal-formats numeric
        # columns on save, so rejecting it was misleading, not protective.
        for value in ("60", "60.0", "60.00"):
            with self.subTest(interval=value):
                self.assertEqual(self._mqtt(**{"Expected reporting interval": value}), [])

    def test_fractional_and_non_numeric_intervals_stay_rejected(self) -> None:
        for value in ("60.5", "abc", "1e2", "+60", "inf"):
            with self.subTest(interval=value):
                errors = self._mqtt(**{"Expected reporting interval": value})
                self.assertEqual([error.code for error in errors], ["invalid_numeric"])
                self.assertIn("whole number", errors[0].message)

    def test_zero_interval_keeps_its_own_code(self) -> None:
        for value in ("0", "0.0"):
            with self.subTest(interval=value):
                errors = self._mqtt(**{"Expected reporting interval": value})
                self.assertEqual([error.code for error in errors], ["invalid_number"])

    def test_topic_rejection_names_every_allowed_suffix(self) -> None:
        errors = self._mqtt(**{"Expected topic": "site/b1/fcu-04"})

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "invalid_topic")
        # Read off the module's own tuple, so the message and the rule it
        # explains can never drift apart unnoticed.
        for suffix in import_service_module._ASSET_TOPIC_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, errors[0].message)

    def test_the_suffix_the_message_suggests_actually_passes(self) -> None:
        self.assertEqual(self._mqtt(**{"Expected topic": "site/b1/fcu-04/#"}), [])


_API_KEY = "test-import-csv-hardening-key"

# Distinct project/site so the shared per-process database never leaks this
# register into other test classes' runs (or theirs into ours).
_PROJECT = "import-csv-hardening-project"
_SITE = "import-csv-hardening-site"


class ImportCsvHttpContractTests(ApiTestCase):
    """The HTTP contract that motivated the item: the service only ever raises
    ValueError, so imports.py's existing `except ValueError -> 400` is complete
    and nothing reaches the operator as a 500."""

    env = {"AUTH_MODE": "api_key", "API_KEY": _API_KEY}
    client_headers = {"X-API-Key": _API_KEY}

    def _upload(self, data: bytes) -> object:
        return self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": _PROJECT, "site_id": _SITE},
            files={"file": ("register.csv", io.BytesIO(data), "text/csv")},
        )

    def test_semicolon_save_returns_400_naming_the_delimiter(self) -> None:
        response = self._upload(
            (_HEADER.replace(",", ";") + "\n" + _ROW.replace(",", ";") + "\n").encode()
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("comma-delimited", response.json()["detail"])

    def test_cr_only_save_imports_instead_of_500ing(self) -> None:
        response = self._upload((_HEADER + "\r" + _ROW + "\r").encode())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "accepted", response.text)
