from __future__ import annotations

import hashlib
import threading
import unittest

from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    NmapReviewedScriptV1,
    NmapScanPlanV1,
    build_nmap_scan_plan,
    nmap_profile_fingerprint,
)
from smart_commissioning_core.engines.ip.nmap_runner import (
    NmapCapabilityState,
    NmapProcessReason,
    NmapRawXmlArtifactV1,
    NmapRuntimeCapabilitySnapshotV1,
    NmapVisibleInterfaceV1,
    preflight_nmap_runtime,
    run_nmap_scan,
)
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapTrustReason,
    NmapTrustResultV1,
)
from smart_commissioning_core.run_context import canonical_sha256


def _fingerprint() -> NmapInstallationFingerprintV1:
    payload = {
        "install_root": r"C:\Program Files\Nmap",
        "executable_path": r"C:\Program Files\Nmap\nmap.exe",
        "data_directory": r"C:\Program Files\Nmap",
        "publisher": "Insecure.Com LLC",
        "signer_sha256": "1" * 64,
        "version": "7.98",
        "executable_sha256": "2" * 64,
        "data_manifest_sha256": "3" * 64,
        "data_file_count": 100,
        "data_total_bytes": 1_000_000,
        "licence_relative_path": "license",
        "licence_sha256": "4" * 64,
        "npsl_version": "0.95",
        "reviewed_scripts": (),
    }
    return NmapInstallationFingerprintV1(
        **payload,
        fingerprint_sha256=canonical_sha256(payload),
    )


def _plan(*, output_max_bytes: int = 4096) -> NmapScanPlanV1:
    return NmapScanPlanV1(
        profile=NmapProfileName.TCP_CONNECT_INVENTORY,
        profile_fingerprint=nmap_profile_fingerprint(NmapProfileName.TCP_CONNECT_INVENTORY),
        packet_plan_sha256="6" * 64,
        targets=("192.0.2.10",),
        tcp_ports=(80, 443),
        source_ip="192.0.2.1",
        interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
        interface_name="Building Controls",
        profile_arguments=("-sT", "-Pn", "--disable-arp-ping"),
        raw_capability_required=False,
        max_rate_per_second=2,
        retries=0,
        host_timeout_seconds=3,
        parent_deadline_seconds=30,
        output_max_bytes=output_max_bytes,
        planned_attempts=2,
        max_parallelism=4,
        max_hostgroup=16,
        scan_delay_ms=125,
    )


def _raw_plan() -> NmapScanPlanV1:
    return build_nmap_scan_plan(
        profile=NmapProfileName.TCP_SYN_INVENTORY,
        targets=["192.0.2.10"],
        tcp_ports=[80, 443],
        source_ip="192.0.2.1",
        interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
        interface_name="Building Controls",
        packet_plan_sha256="6" * 64,
        max_rate_per_second=2,
        retries=0,
        host_timeout_seconds=3,
        parent_deadline_seconds=30,
        output_max_bytes=4096,
    )


class _Spool:
    def __init__(self) -> None:
        self.payload = bytearray()
        self.finalized: bool | None = None

    def write(self, payload: bytes) -> None:
        self.payload.extend(payload)

    def finalize(self, *, capture_complete: bool) -> NmapRawXmlArtifactV1:
        self.finalized = capture_complete
        payload = bytes(self.payload)
        return NmapRawXmlArtifactV1(
            artifact_id="artifact:nmap-xml:test",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            capture_complete=capture_complete,
        )


class _Artifacts:
    def __init__(self) -> None:
        self.target_payload: bytes | None = None
        self.target_removed = False
        self.spool = _Spool()
        self.stderr_spool = _Spool()

    def create_owner_only_target_list(self, payload: bytes) -> str:
        self.target_payload = payload
        return r"C:\ProgramData\SmartCommissioning\private\targets-123.txt"

    def create_owner_only_xml_spool(self, *, max_bytes: int) -> _Spool:
        self.max_bytes = max_bytes
        return self.spool

    def create_owner_only_stderr_spool(self, *, max_bytes: int) -> _Spool:
        self.stderr_max_bytes = max_bytes
        return self.stderr_spool

    def private_temp_directory(self) -> str:
        return r"C:\ProgramData\SmartCommissioning\private"

    def remove_target_list(self, path: str) -> None:
        self.target_removed = path.endswith("targets-123.txt")


class _WindowsProcessApi:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.stdout = [b"<nmaprun><runstats><finished exit='success'/></runstats></nmaprun>", b""]
        self.stderr = [b"", b""]
        self.waits = [None, 0]
        self.time = 100.0
        self._lock = threading.Lock()

    def system_root(self) -> str:
        return r"C:\Windows"

    def monotonic(self) -> float:
        return self.time

    def create_kill_on_close_job(self, limits: object) -> str:
        self.calls.append(("create_job", limits))
        return "job"

    def create_process_suspended(self, launch: object) -> str:
        self.calls.append(("create_process_suspended", launch))
        return "process"

    def assign_process_to_job(self, job: object, process: object) -> None:
        self.calls.append(("assign", job, process))

    def resume_process(self, process: object) -> None:
        self.calls.append(("resume", process))

    def read_pipe(self, _process: object, stream: str, _max_bytes: int) -> bytes:
        with self._lock:
            chunks = self.stdout if stream == "stdout" else self.stderr
            return chunks.pop(0) if chunks else b""

    def wait_process(self, _process: object, timeout_seconds: float) -> int | None:
        self.calls.append(("wait", timeout_seconds))
        self.time += timeout_seconds
        return self.waits.pop(0) if self.waits else 0

    def terminate_job(self, job: object, exit_code: int) -> None:
        self.calls.append(("terminate_job", job, exit_code))

    def terminate_suspended_process(
        self,
        process: object,
        exit_code: int,
        timeout_seconds: float,
    ) -> bool:
        self.calls.append(("terminate_suspended", process, exit_code, timeout_seconds))
        return True

    def wait_job_empty(self, job: object, timeout_seconds: float) -> bool:
        self.calls.append(("wait_job_empty", job, timeout_seconds))
        return True

    def close_process(self, process: object) -> None:
        self.calls.append(("close_process", process))

    def close_job(self, job: object) -> None:
        self.calls.append(("close_job", job))


class _CapabilityProbe:
    def __init__(
        self,
        *,
        npcap_installed: bool = False,
        npcap_running: bool = False,
        npcap_admin_only: bool = False,
        token_is_administrator: bool = False,
        token_has_raw_rights: bool = False,
        npcap_version: str | None = None,
        executor_identity: str = "machine-guid:11111111-1111-1111-1111-111111111111",
    ) -> None:
        self.calls = 0
        self.snapshot_value = NmapRuntimeCapabilitySnapshotV1(
            executor_identity=executor_identity,
            interfaces=(
                NmapVisibleInterfaceV1(
                    interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
                    interface_name="Building Controls",
                    source_ip="192.0.2.1",
                    nmap_device_name=(
                        r"\Device\NPF_{00000000-0000-0000-0000-000000000001}"
                        if npcap_running
                        else None
                    ),
                ),
            ),
            npcap_installed=npcap_installed,
            npcap_version=(npcap_version or "1.80") if npcap_installed else None,
            npcap_service_running=npcap_running,
            npcap_admin_only=npcap_admin_only,
            current_token_is_administrator=token_is_administrator,
            current_token_has_raw_rights=token_has_raw_rights,
        )

    def snapshot(self) -> NmapRuntimeCapabilitySnapshotV1:
        self.calls += 1
        return self.snapshot_value


_EXECUTOR_ID = "machine-guid:11111111-1111-1111-1111-111111111111"


def _runtime_arguments(*, probe: _CapabilityProbe | None = None) -> dict[str, object]:
    return {
        "deployment_mode": "internal_operator_managed",
        "expected_executor_identity": _EXECUTOR_ID,
        "runtime_probe": probe or _CapabilityProbe(),
    }


class NmapRunnerTests(unittest.TestCase):
    def test_success_uses_suspended_createprocess_and_job_before_resume(self) -> None:
        artifacts = _Artifacts()
        windows = _WindowsProcessApi()
        control_checks = 0

        def control_guard() -> None:
            nonlocal control_checks
            control_checks += 1

        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=control_guard,
            artifacts=artifacts,
            windows=windows,
            poll_interval_seconds=0.05,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.COMPLETED)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.process_tree_gone)
        self.assertEqual(artifacts.target_payload, b"192.0.2.10\n")
        self.assertTrue(artifacts.target_removed)
        self.assertTrue(artifacts.spool.finalized)
        self.assertGreaterEqual(control_checks, 3)
        operations = [call[0] for call in windows.calls if isinstance(call, tuple)]
        self.assertLess(operations.index("create_job"), operations.index("create_process_suspended"))
        self.assertLess(operations.index("create_process_suspended"), operations.index("assign"))
        self.assertLess(operations.index("assign"), operations.index("resume"))
        launch = next(call[1] for call in windows.calls if call[0] == "create_process_suspended")
        self.assertEqual(launch.application_name, r"C:\Program Files\Nmap\nmap.exe")
        self.assertIsInstance(launch.arguments, tuple)
        self.assertFalse(launch.shell)
        self.assertTrue(launch.suspended)
        self.assertEqual(launch.inherited_handle_roles, ("stdout", "stderr"))
        self.assertEqual(
            dict(launch.environment),
            {
                "SystemRoot": r"C:\Windows",
                "TEMP": r"C:\ProgramData\SmartCommissioning\private",
                "TMP": r"C:\ProgramData\SmartCommissioning\private",
                "WINDIR": r"C:\Windows",
            },
        )
        self.assertNotIn("PATH", dict(launch.environment))
        self.assertIn("--datadir", launch.arguments)
        self.assertIn("-iL", launch.arguments)

    def test_job_assignment_failure_terminates_the_still_suspended_process(self) -> None:
        class AssignmentFailure(_WindowsProcessApi):
            def assign_process_to_job(self, job: object, process: object) -> None:
                super().assign_process_to_job(job, process)
                raise OSError("AssignProcessToJobObject failed")

        windows = AssignmentFailure()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=_Artifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.JOB_ASSIGNMENT_FAILED)
        self.assertTrue(result.process_tree_gone)
        operations = [call[0] for call in windows.calls if isinstance(call, tuple)]
        self.assertIn("terminate_suspended", operations)
        self.assertNotIn("resume", operations)

    def test_persisted_stop_terminates_the_job_tree_and_cannot_succeed(self) -> None:
        class StopRequested(RuntimeError):
            reason = "stop_requested"

        windows = _WindowsProcessApi()
        windows.waits = [None] * 20
        checks = 0

        def control_guard() -> None:
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise StopRequested("persisted stop requested")

        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=control_guard,
            artifacts=_Artifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.STOP_REQUESTED)
        self.assertNotEqual(result.status.value, "succeeded")
        self.assertTrue(result.process_tree_gone)
        operations = [call[0] for call in windows.calls if isinstance(call, tuple)]
        self.assertIn("terminate_job", operations)
        self.assertIn("wait_job_empty", operations)

    def test_cleanup_waits_share_one_five_second_deadline(self) -> None:
        class StopRequested(RuntimeError):
            reason = "stop_requested"

        class DeadlineProcess(_WindowsProcessApi):
            def __init__(self, *, job_becomes_empty: bool) -> None:
                super().__init__()
                self.waits = [None] * 20
                self.job_becomes_empty = job_becomes_empty
                self.cleanup_started_at: float | None = None

            def terminate_job(self, job: object, exit_code: int) -> None:
                if self.cleanup_started_at is None:
                    self.cleanup_started_at = self.time
                super().terminate_job(job, exit_code)

            def wait_job_empty(self, job: object, timeout_seconds: float) -> bool:
                super().wait_job_empty(job, timeout_seconds)
                self.time += timeout_seconds
                return self.job_becomes_empty

        for job_becomes_empty in (True, False):
            with self.subTest(job_becomes_empty=job_becomes_empty):
                windows = DeadlineProcess(job_becomes_empty=job_becomes_empty)
                checks = 0

                def control_guard() -> None:
                    nonlocal checks
                    checks += 1
                    if checks >= 4:
                        raise StopRequested("persisted stop requested")

                result = run_nmap_scan(
                    plan=_plan(),
                    revalidate_trust=lambda: NmapTrustResultV1(
                        available=True,
                        reason=NmapTrustReason.AVAILABLE,
                        fingerprint=_fingerprint(),
                    ),
                    control_guard=control_guard,
                    artifacts=_Artifacts(),
                    windows=windows,
                    **_runtime_arguments(),
                )

                assert windows.cleanup_started_at is not None
                self.assertLessEqual(windows.time - windows.cleanup_started_at, 5.0)
                expected = (
                    NmapProcessReason.STOP_REQUESTED
                    if job_becomes_empty
                    else NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
                )
                self.assertEqual(result.reason, expected)

    def test_stdout_overflow_terminates_tree_and_marks_raw_artifact_incomplete(self) -> None:
        class OverflowProcess(_WindowsProcessApi):
            def __init__(self) -> None:
                super().__init__()
                self.stdout = [b"x" * 2048, b""]
                self.waits = [None] * 20
                self.overflow_read = threading.Event()

            def read_pipe(self, process: object, stream: str, max_bytes: int) -> bytes:
                chunk = super().read_pipe(process, stream, max_bytes)
                if stream == "stdout" and chunk:
                    self.overflow_read.set()
                return chunk

            def wait_process(self, process: object, timeout_seconds: float) -> int | None:
                self.overflow_read.wait(timeout=1)
                return super().wait_process(process, timeout_seconds)

        artifacts = _Artifacts()
        windows = OverflowProcess()
        result = run_nmap_scan(
            plan=_plan(output_max_bytes=1024),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.STDOUT_LIMIT_EXCEEDED)
        assert result.xml_artifact is not None
        self.assertEqual(result.xml_artifact.size_bytes, 1024)
        self.assertFalse(result.xml_artifact.capture_complete)
        self.assertTrue(result.process_tree_gone)
        self.assertTrue(any(call[0] == "terminate_job" for call in windows.calls))

    def test_unavailable_trust_starts_no_child(self) -> None:
        artifacts = _Artifacts()
        windows = _WindowsProcessApi()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.FINGERPRINT_DRIFT,
                fingerprint=None,
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.TRUST_UNAVAILABLE)
        self.assertTrue(result.process_tree_gone)
        self.assertFalse(any(call[0] == "create_process_suspended" for call in windows.calls))

    def test_nonzero_exit_retains_complete_capture_but_cannot_succeed(self) -> None:
        windows = _WindowsProcessApi()
        windows.waits = [7]
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=_Artifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.PROCESS_NONZERO)
        self.assertEqual(result.exit_code, 7)
        self.assertNotEqual(result.status.value, "succeeded")
        assert result.xml_artifact is not None
        self.assertTrue(result.xml_artifact.capture_complete)

    def test_capability_preflight_distinguishes_connect_raw_and_admin_only(self) -> None:
        def trust() -> NmapTrustResultV1:
            return NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            )

        connect_only = preflight_nmap_runtime(
            plan=_plan(),
            revalidate_trust=trust,
            **_runtime_arguments(probe=_CapabilityProbe()),
        )
        self.assertEqual(connect_only.capability.state, NmapCapabilityState.NPCAP_MISSING)
        self.assertTrue(connect_only.capability.connect_available)
        self.assertFalse(connect_only.capability.raw_available)
        self.assertTrue(connect_only.capability.launch_allowed)

        raw_plan = _raw_plan()
        raw = preflight_nmap_runtime(
            plan=raw_plan,
            revalidate_trust=trust,
            **_runtime_arguments(
                probe=_CapabilityProbe(
                    npcap_installed=True,
                    npcap_running=True,
                    token_is_administrator=True,
                    token_has_raw_rights=True,
                )
            ),
        )
        self.assertEqual(raw.capability.state, NmapCapabilityState.RAW_CAPABLE)
        self.assertTrue(raw.capability.launch_allowed)

        blocked = preflight_nmap_runtime(
            plan=raw_plan,
            revalidate_trust=trust,
            **_runtime_arguments(
                probe=_CapabilityProbe(
                    npcap_installed=True,
                    npcap_running=True,
                    npcap_admin_only=True,
                    token_is_administrator=False,
                    token_has_raw_rights=False,
                )
            ),
        )
        self.assertEqual(blocked.capability.state, NmapCapabilityState.NPCAP_ADMIN_ONLY)
        self.assertFalse(blocked.capability.launch_allowed)

    def test_external_mode_and_executor_mismatch_fail_before_child_or_uac_path(self) -> None:
        trust_calls = 0

        def trust() -> NmapTrustResultV1:
            nonlocal trust_calls
            trust_calls += 1
            return NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            )

        probe = _CapabilityProbe()
        external = preflight_nmap_runtime(
            plan=_plan(),
            deployment_mode="external_customer",
            expected_executor_identity=_EXECUTOR_ID,
            revalidate_trust=trust,
            runtime_probe=probe,
        )
        self.assertEqual(external.capability.state, NmapCapabilityState.EXTERNAL_MODE_BLOCKED)
        self.assertEqual(trust_calls, 0)
        self.assertEqual(probe.calls, 0)

        mismatch = preflight_nmap_runtime(
            plan=_plan(),
            deployment_mode="internal_operator_managed",
            expected_executor_identity="machine-guid:22222222-2222-2222-2222-222222222222",
            revalidate_trust=trust,
            runtime_probe=probe,
        )
        self.assertEqual(mismatch.capability.state, NmapCapabilityState.EXECUTOR_MISMATCH)
        self.assertFalse(mismatch.capability.launch_allowed)

    def test_success_returns_complete_protected_metadata_for_both_pipes(self) -> None:
        artifacts = _Artifacts()
        windows = _WindowsProcessApi()
        windows.stderr = [b"warning bytes", b""]
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(),
        )

        assert result.xml_artifact is not None
        assert result.stderr_artifact is not None
        self.assertTrue(result.xml_artifact.capture_complete)
        self.assertTrue(result.stderr_artifact.capture_complete)
        self.assertEqual(result.stderr_artifact.size_bytes, len(b"warning bytes"))
        self.assertEqual(
            result.stderr_artifact.sha256,
            hashlib.sha256(b"warning bytes").hexdigest(),
        )
        assert result.provenance is not None
        self.assertEqual(result.provenance.nmap_version, "7.98")
        self.assertEqual(result.provenance.publisher, "Insecure.Com LLC")
        self.assertEqual(result.provenance.packet_plan_sha256, "6" * 64)
        self.assertEqual(result.provenance.executor_identity, _EXECUTOR_ID)
        self.assertNotIn("\\", result.provenance.model_dump_json())

    def test_raw_profile_without_capability_creates_no_artifact_or_child(self) -> None:
        raw_plan = _raw_plan()
        artifacts = _Artifacts()
        windows = _WindowsProcessApi()
        result = run_nmap_scan(
            plan=raw_plan,
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(
                probe=_CapabilityProbe(
                    npcap_installed=True,
                    npcap_running=True,
                    npcap_admin_only=True,
                )
            ),
        )

        self.assertEqual(result.reason, NmapProcessReason.CAPABILITY_UNAVAILABLE)
        self.assertEqual(result.status.value, "unavailable")
        self.assertIsNone(artifacts.target_payload)
        self.assertFalse(any(call[0] == "create_process_suspended" for call in windows.calls))

    def test_raw_device_mismatch_is_rejected_before_artifacts_or_process(self) -> None:
        raw_plan = build_nmap_scan_plan(
            profile=NmapProfileName.TCP_SYN_INVENTORY,
            targets=["192.0.2.10"],
            tcp_ports=[443],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Building Controls",
            packet_plan_sha256="6" * 64,
            max_rate_per_second=2,
            retries=0,
            host_timeout_seconds=3,
            parent_deadline_seconds=30,
            output_max_bytes=4096,
        )
        mismatched_probe = _CapabilityProbe(
            npcap_installed=True,
            npcap_running=True,
            token_is_administrator=True,
            token_has_raw_rights=True,
        )
        mismatched_probe.snapshot_value = mismatched_probe.snapshot_value.model_copy(
            update={
                "interfaces": (
                    NmapVisibleInterfaceV1(
                        interface_id=raw_plan.interface_id,
                        interface_name=raw_plan.interface_name,
                        source_ip=raw_plan.source_ip,
                        nmap_device_name=(
                            r"\Device\NPF_{00000000-0000-0000-0000-000000000099}"
                        ),
                    ),
                )
            }
        )
        artifacts = _Artifacts()
        windows = _WindowsProcessApi()

        result = run_nmap_scan(
            plan=raw_plan,
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(probe=mismatched_probe),
        )

        self.assertEqual(result.reason, NmapProcessReason.CAPABILITY_UNAVAILABLE)
        assert result.runtime_capability is not None
        self.assertEqual(
            result.runtime_capability.state,
            NmapCapabilityState.INTERFACE_UNAVAILABLE,
        )
        self.assertIsNone(artifacts.target_payload)
        self.assertFalse(any(call[0] == "create_job" for call in windows.calls))

    def test_incomplete_artifact_adapter_cannot_turn_zero_exit_into_success(self) -> None:
        class IncompleteSpool(_Spool):
            def finalize(self, *, capture_complete: bool) -> NmapRawXmlArtifactV1:
                return super().finalize(capture_complete=False)

        artifacts = _Artifacts()
        artifacts.spool = IncompleteSpool()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=_WindowsProcessApi(),
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.PIPE_FAILURE)
        self.assertEqual(result.status.value, "failed")
        assert result.xml_artifact is not None
        self.assertFalse(result.xml_artifact.capture_complete)

    def test_lingering_job_children_are_terminated_and_cannot_succeed(self) -> None:
        class LingeringChildProcess(_WindowsProcessApi):
            def __init__(self) -> None:
                super().__init__()
                self.job_waits = 0

            def wait_job_empty(self, job: object, timeout_seconds: float) -> bool:
                super().wait_job_empty(job, timeout_seconds)
                self.job_waits += 1
                return self.job_waits >= 2

        windows = LingeringChildProcess()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=_Artifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED)
        self.assertEqual(result.status.value, "failed")
        self.assertTrue(result.process_tree_gone)
        self.assertTrue(any(call[0] == "terminate_job" for call in windows.calls))

    def test_fingerprint_drift_after_artifact_creation_is_caught_before_createprocess(self) -> None:
        calls = 0

        def trust() -> NmapTrustResultV1:
            nonlocal calls
            calls += 1
            if calls == 1:
                return NmapTrustResultV1(
                    available=True,
                    reason=NmapTrustReason.AVAILABLE,
                    fingerprint=_fingerprint(),
                )
            return NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.FINGERPRINT_DRIFT,
                fingerprint=None,
            )

        artifacts = _Artifacts()
        windows = _WindowsProcessApi()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=trust,
            control_guard=lambda: None,
            artifacts=artifacts,
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(calls, 2)
        self.assertEqual(result.reason, NmapProcessReason.TRUST_UNAVAILABLE)
        self.assertTrue(artifacts.target_removed)
        self.assertFalse(any(call[0] == "create_process_suspended" for call in windows.calls))

    def test_parent_deadline_and_control_store_failure_both_kill_the_job(self) -> None:
        deadline_windows = _WindowsProcessApi()
        deadline_windows.waits = [None] * 20
        deadline_plan = _plan().model_copy(update={"parent_deadline_seconds": 0.05})
        deadline_result = run_nmap_scan(
            plan=deadline_plan,
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=_Artifacts(),
            windows=deadline_windows,
            poll_interval_seconds=0.05,
            **_runtime_arguments(),
        )
        self.assertEqual(deadline_result.reason, NmapProcessReason.DEADLINE_EXCEEDED)
        self.assertTrue(any(call[0] == "terminate_job" for call in deadline_windows.calls))

        control_windows = _WindowsProcessApi()
        control_windows.waits = [None] * 20
        checks = 0

        def failed_control_store() -> None:
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise OSError("database unavailable")

        control_result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=failed_control_store,
            artifacts=_Artifacts(),
            windows=control_windows,
            **_runtime_arguments(),
        )
        self.assertEqual(
            control_result.reason,
            NmapProcessReason.CONTROL_STORE_FAILURE,
        )
        self.assertTrue(any(call[0] == "terminate_job" for call in control_windows.calls))

    def test_stderr_overflow_is_bounded_retained_and_forces_failure(self) -> None:
        class StderrOverflow(_WindowsProcessApi):
            def __init__(self) -> None:
                super().__init__()
                self.stderr = [b"e" * (1024 * 1024 + 1), b""]
                self.waits = [None] * 20
                self.stderr_read = threading.Event()

            def read_pipe(self, process: object, stream: str, max_bytes: int) -> bytes:
                chunk = super().read_pipe(process, stream, max_bytes)
                if stream == "stderr" and chunk:
                    self.stderr_read.set()
                return chunk

            def wait_process(self, process: object, timeout_seconds: float) -> int | None:
                self.stderr_read.wait(timeout=1)
                return super().wait_process(process, timeout_seconds)

        windows = StderrOverflow()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=_Artifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.STDERR_LIMIT_EXCEEDED)
        assert result.stderr_artifact is not None
        self.assertEqual(result.stderr_artifact.size_bytes, 1024 * 1024)
        self.assertFalse(result.stderr_artifact.capture_complete)
        self.assertTrue(any(call[0] == "terminate_job" for call in windows.calls))

    def test_selected_script_must_match_the_confirmed_installation_identity(self) -> None:
        selected_script = NmapReviewedScriptV1(name="http-title", sha256="7" * 64)
        script_plan = build_nmap_scan_plan(
            profile=NmapProfileName.REVIEWED_SCRIPT_INVENTORY,
            targets=["192.0.2.10"],
            tcp_ports=[80],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Building Controls",
            packet_plan_sha256="6" * 64,
            max_rate_per_second=2,
            retries=0,
            host_timeout_seconds=15,
            parent_deadline_seconds=30,
            output_max_bytes=4096,
            reviewed_scripts=(selected_script,),
        )
        preflight = preflight_nmap_runtime(
            plan=script_plan,
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            **_runtime_arguments(),
        )

        self.assertEqual(preflight.capability.state, NmapCapabilityState.TRUST_REJECTED)
        self.assertEqual(
            preflight.capability.trust_reason,
            NmapTrustReason.REVIEWED_SCRIPT_REJECTED,
        )
        self.assertFalse(preflight.capability.launch_allowed)

    def test_artifact_adapter_cannot_redirect_the_internal_target_file(self) -> None:
        class EscapedTargetArtifacts(_Artifacts):
            def create_owner_only_target_list(self, payload: bytes) -> str:
                self.target_payload = payload
                return r"C:\Users\operator\targets.txt"

        windows = _WindowsProcessApi()
        result = run_nmap_scan(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            control_guard=lambda: None,
            artifacts=EscapedTargetArtifacts(),
            windows=windows,
            **_runtime_arguments(),
        )

        self.assertEqual(result.reason, NmapProcessReason.LAUNCH_FAILED)
        self.assertFalse(any(call[0] == "create_process_suspended" for call in windows.calls))

    def test_malformed_trust_or_capability_adapter_output_fails_closed(self) -> None:
        malformed_trust = preflight_nmap_runtime(
            plan=_plan(),
            revalidate_trust=lambda: {"available": True},  # type: ignore[return-value]
            **_runtime_arguments(),
        )
        self.assertEqual(
            malformed_trust.capability.state,
            NmapCapabilityState.TRUST_REJECTED,
        )
        self.assertFalse(malformed_trust.capability.launch_allowed)

        class MalformedProbe:
            def snapshot(self) -> object:
                return {"executor_identity": _EXECUTOR_ID}

        malformed_capability = preflight_nmap_runtime(
            plan=_plan(),
            revalidate_trust=lambda: NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=_fingerprint(),
            ),
            deployment_mode="internal_operator_managed",
            expected_executor_identity=_EXECUTOR_ID,
            runtime_probe=MalformedProbe(),  # type: ignore[arg-type]
        )
        self.assertEqual(
            malformed_capability.capability.state,
            NmapCapabilityState.CAPABILITY_PROBE_FAILED,
        )
        self.assertFalse(malformed_capability.capability.launch_allowed)


if __name__ == "__main__":
    unittest.main()
