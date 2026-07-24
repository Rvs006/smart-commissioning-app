"""Report artifact filename and download-header safety tests."""

import unittest

from app.services.report_naming import (
    build_report_file_name,
    report_content_disposition,
)


class ReportNamingTests(unittest.TestCase):
    def test_custom_title_is_readable_safe_unique_and_keeps_extension(self) -> None:
        file_name = build_report_file_name(
            "udmi_validation",
            "run-123",
            "pdf",
            report_title="  Café / 東京: Phase * 1?  ",
            custom_title=True,
        )

        self.assertEqual(file_name, "Café_東京_Phase_1_run-123.pdf")
        self.assertNotIn("/", file_name)
        self.assertTrue(file_name.endswith("_run-123.pdf"))

    def test_legacy_default_and_blank_custom_titles_use_type_based_name(self) -> None:
        legacy = build_report_file_name(
            "udmi_validation",
            "run-456",
            "xlsx",
            report_title="A title persisted by an older version",
            custom_title=False,
        )
        blank = build_report_file_name(
            "udmi_validation",
            "run-456",
            "xlsx",
            report_title="   ",
            custom_title=True,
        )

        self.assertEqual(legacy, "udmi_validation_run-456.xlsx")
        self.assertEqual(blank, legacy)

    def test_unicode_download_header_has_ascii_fallback_and_utf8_name(self) -> None:
        file_name = "Café_東京_Phase_1_run-123.pdf"

        header = report_content_disposition(file_name)

        self.assertIn('filename="Cafe_Phase_1_run-123.pdf"', header)
        self.assertIn(
            "filename*=UTF-8''Caf%C3%A9_%E6%9D%B1%E4%BA%AC_Phase_1_run-123.pdf",
            header,
        )
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)

    def test_long_or_hostile_title_cannot_escape_one_filename_component(self) -> None:
        file_name = build_report_file_name(
            "udmi_validation",
            "run-789",
            "docx",
            report_title=("\r\nContent-Type: text/plain\\..\\" + "数" * 200),
            custom_title=True,
        )

        self.assertNotIn("\r", file_name)
        self.assertNotIn("\n", file_name)
        self.assertNotIn("/", file_name)
        self.assertNotIn("\\", file_name)
        self.assertLess(len(file_name.encode("utf-8")), 180)
        self.assertTrue(file_name.endswith("_run-789.docx"))


if __name__ == "__main__":
    unittest.main()
