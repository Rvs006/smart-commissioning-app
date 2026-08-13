from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from check_nmap_distribution_absence import scan_distribution


class NmapDistributionAbsenceTests(unittest.TestCase):
    def test_portable_build_and_workflow_run_the_scan_before_release(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        build = (repository / "packaging/windows_portable/build.ps1").read_text(
            encoding="utf-8-sig"
        )
        workflow = (repository / ".github/workflows/windows-portable.yml").read_text(
            encoding="utf-8"
        )
        command = "scripts/check_nmap_distribution_absence.py"

        self.assertIn(command, build)
        self.assertLess(build.index(command), build.index('Write-Step "DONE"'))
        self.assertIn(command, workflow)
        self.assertLess(workflow.index(command), workflow.index("name: Upload bundle artifact"))
        self.assertIn("Build and scan the exact portable release ZIP", workflow)
        self.assertIn("Compress-Archive", workflow)
        self.assertIn("SmartCommissioningApp-windows-portable.zip", workflow)
        self.assertIn("--sbom $pythonSbom", workflow)
        self.assertIn("--sbom $npmSbom", workflow)

    def test_clean_bundle_and_sbom_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SmartCommissioningApp.exe").write_bytes(b"portable application")
            (root / "README_FIRST.txt").write_text(
                "Operator-managed network tools are not included.",
                encoding="utf-8",
            )
            sbom = root / "SBOM.python.cdx.json"
            sbom.write_text(
                json.dumps({"components": [{"name": "smart-commissioning-app"}]}),
                encoding="utf-8",
            )

            self.assertEqual(scan_distribution(root, sbom_paths=(sbom,)), ())

    def test_component_names_and_nested_archive_entries_are_rejected(self) -> None:
        forbidden_names = (
            "nmap.exe",
            "nmap-services",
            "npcap-oem.exe",
            "Packet.dll",
            "wpcap.dll",
            "npf.sys",
        )
        for name in forbidden_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / name).write_bytes(b"component")
                self.assertTrue(scan_distribution(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "nested.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("vendor/Nmap.exe", b"component")
            self.assertTrue(scan_distribution(root))

    def test_sbom_and_automatic_acquisition_or_elevation_markers_are_rejected(self) -> None:
        payloads = (
            b"https://nmap.org/dist/nmap-7.95-setup.exe",
            b"choco install nmap",
            b"winget install Insecure.Nmap",
            b"Start-Process installer.exe -Verb RunAs",
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "payload.bin").write_bytes(payload)
                self.assertTrue(scan_distribution(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sbom = root / "SBOM.python.cdx.json"
            sbom.write_text(
                json.dumps({"components": [{"name": "python-nmap", "purl": "pkg:pypi/python-nmap"}]}),
                encoding="utf-8",
            )
            self.assertTrue(scan_distribution(root, sbom_paths=(sbom,)))

    def test_utf16_acquisition_marker_split_across_read_chunks_is_rejected(self) -> None:
        marker = "https://nmap.org/dist/".encode("utf-16le")
        read_chunk_bytes = 1024 * 1024
        first_chunk_marker_bytes = 40

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = (
                b"x" * (read_chunk_bytes - first_chunk_marker_bytes)
                + marker
                + b"trailing"
            )
            (root / "payload.bin").write_bytes(payload)

            self.assertTrue(scan_distribution(root))


if __name__ == "__main__":
    unittest.main()
