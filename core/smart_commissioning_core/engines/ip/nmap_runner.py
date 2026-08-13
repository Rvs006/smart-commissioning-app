"""Bounded Windows Nmap process execution behind injected Win32 primitives.

This module never uses ``subprocess``, shell command text, executable search, or
environment inheritance.  A production adapter maps the explicit operations to
CreateProcessW, Windows pipes, and a kill-on-close Job Object.
"""

from __future__ import annotations

import hashlib
import ipaddress
import ntpath
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    NmapScanPlanV1,
    build_nmap_subprocess_arguments,
    nmap_device_name_for_interface_id,
    render_nmap_target_list,
)
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapTrustReason,
    NmapTrustResultV1,
)

_PIPE_CHUNK_BYTES = 65_536
_STDERR_MAX_BYTES = 1_048_576
_JOB_CLEANUP_SECONDS = 5.0
_JOB_TERMINATION_EXIT_CODE = 0xC000013A


class NmapProcessStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class NmapProcessReason(StrEnum):
    COMPLETED = "completed"
    TRUST_UNAVAILABLE = "trust_unavailable"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    STOP_REQUESTED = "stop_requested"
    AUTHORIZATION_LOST = "authorization_lost"
    OWNERSHIP_LOST = "ownership_lost"
    CONTROL_STORE_FAILURE = "control_store_failure"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STDOUT_LIMIT_EXCEEDED = "stdout_limit_exceeded"
    STDERR_LIMIT_EXCEEDED = "stderr_limit_exceeded"
    PIPE_FAILURE = "pipe_failure"
    LAUNCH_FAILED = "launch_failed"
    JOB_ASSIGNMENT_FAILED = "job_assignment_failed"
    PROCESS_NONZERO = "process_nonzero"
    PROCESS_TREE_CLEANUP_FAILED = "process_tree_cleanup_failed"


class NmapCapabilityState(StrEnum):
    INTERNAL_CONNECT_ONLY = "internal_connect_only"
    RAW_CAPABLE = "raw_capable"
    NPCAP_MISSING = "npcap_missing"
    NPCAP_STOPPED = "npcap_stopped"
    NPCAP_ADMIN_ONLY = "npcap_admin_only"
    INSUFFICIENT_RAW_RIGHTS = "insufficient_raw_rights"
    INTERFACE_UNAVAILABLE = "interface_unavailable"
    EXECUTOR_MISMATCH = "executor_mismatch"
    VERSION_REJECTED = "version_rejected"
    PATH_OR_DATA_REJECTED = "path_or_data_rejected"
    TRUST_REJECTED = "trust_rejected"
    EXTERNAL_MODE_BLOCKED = "external_mode_blocked"
    CAPABILITY_PROBE_FAILED = "capability_probe_failed"


class NmapVisibleInterfaceV1(BaseModel):
    """One interface visible to Nmap on the current Windows executor."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    interface_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9:-]+$")
    interface_name: str = Field(min_length=1, max_length=255)
    source_ip: str
    nmap_device_name: str | None = Field(default=None, max_length=255)

    @field_validator("source_ip")
    @classmethod
    def _numeric_ipv4(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("Nmap visible interface source_ip must be IPv4") from error
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError("Nmap visible interface source_ip must be IPv4")
        return str(address)

    @field_validator("nmap_device_name")
    @classmethod
    def _canonical_npcap_device(cls, value: str | None) -> str | None:
        if value is None:
            return None
        match = re.fullmatch(
            r"\\Device\\NPF_\{(?P<guid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}",
            value,
        )
        if match is None:
            raise ValueError("Nmap device name must be a canonical Windows Npcap GUID")
        return rf"\Device\NPF_{{{match.group('guid').upper()}}}"


class NmapRuntimeCapabilitySnapshotV1(BaseModel):
    """Atomic, network-free Windows registry/service/token capability snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    executor_identity: str = Field(min_length=1, max_length=255)
    interfaces: tuple[NmapVisibleInterfaceV1, ...] = Field(max_length=256)
    npcap_installed: bool
    npcap_version: str | None = Field(default=None, max_length=128)
    npcap_service_running: bool
    npcap_admin_only: bool
    current_token_is_administrator: bool
    current_token_has_raw_rights: bool

    @model_validator(mode="after")
    def _coherent_npcap_snapshot(self) -> NmapRuntimeCapabilitySnapshotV1:
        if not self.npcap_installed and (
            self.npcap_version is not None or self.npcap_service_running or self.npcap_admin_only
        ):
            raise ValueError("Npcap cannot be running or admin-only when it is not installed")
        if self.npcap_installed and not self.npcap_version:
            raise ValueError("installed Npcap capability must include its version")
        identities = {
            (
                item.interface_id,
                item.interface_name,
                item.source_ip,
                item.nmap_device_name,
            )
            for item in self.interfaces
        }
        if len(identities) != len(self.interfaces):
            raise ValueError("Nmap interface capability rows must be unique")
        return self


class NmapRuntimeCapabilityV1(BaseModel):
    """Sanitized preflight state used to gate process creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    state: NmapCapabilityState
    executor_identity: str | None = Field(default=None, max_length=255)
    trust_reason: NmapTrustReason | None = None
    connect_available: bool
    raw_available: bool
    launch_allowed: bool
    interface_available: bool
    npcap_installed: bool | None = None
    npcap_version: str | None = Field(default=None, max_length=128)
    npcap_service_running: bool | None = None
    npcap_admin_only: bool | None = None
    current_token_is_administrator: bool | None = None

    @model_validator(mode="after")
    def _consistent_capability(self) -> NmapRuntimeCapabilityV1:
        if self.raw_available and not self.connect_available:
            raise ValueError("raw Nmap capability requires connect capability")
        if self.launch_allowed and (not self.connect_available or not self.interface_available):
            raise ValueError("Nmap launch requires connect and interface capability")
        return self


class NmapRuntimePreflightV1(BaseModel):
    """Protected prelaunch result, including the exact revalidated fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    capability: NmapRuntimeCapabilityV1
    fingerprint: NmapInstallationFingerprintV1 | None = None

    @model_validator(mode="after")
    def _consistent_preflight(self) -> NmapRuntimePreflightV1:
        if self.capability.launch_allowed != (self.fingerprint is not None):
            raise ValueError("launchable Nmap preflight requires an exact fingerprint")
        return self


class NmapRuntimeCapabilityProbe(Protocol):
    """Injected Windows registry, service, token, machine, and NIC APIs."""

    def snapshot(self) -> NmapRuntimeCapabilitySnapshotV1: ...


class NmapProcessProvenanceV1(BaseModel):
    """Viewer-safe immutable provenance for one process attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    provider: Literal["operator_managed_nmap"] = "operator_managed_nmap"
    provider_contract_version: Literal["1.0"] = "1.0"
    profile: NmapProfileName
    profile_version: Literal["1.0"] = "1.0"
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nmap_version: str = Field(min_length=1, max_length=128)
    publisher: str = Field(min_length=1, max_length=512)
    installation_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_identity: str = Field(min_length=1, max_length=255)
    interface_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9:-]+$")
    interface_name: str = Field(min_length=1, max_length=255)
    source_ip: str
    npcap_version: str | None = Field(default=None, max_length=128)

    @field_validator("source_ip")
    @classmethod
    def _source_ipv4(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("Nmap provenance source_ip must be IPv4") from error
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError("Nmap provenance source_ip must be IPv4")
        return str(address)


class NmapProcessLaunchV1(BaseModel):
    """Exact CreateProcessW contract passed to the Windows adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    application_name: str = Field(min_length=3, max_length=2048)
    arguments: tuple[str, ...] = Field(min_length=1, max_length=256)
    working_directory: str = Field(min_length=3, max_length=2048)
    environment: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=8)
    shell: Literal[False] = False
    suspended: Literal[True] = True
    create_no_window: Literal[True] = True
    stdin: Literal["null"] = "null"
    inherited_handle_roles: tuple[Literal["stdout", "stderr"], ...] = (
        "stdout",
        "stderr",
    )

    @field_validator("application_name", "working_directory")
    @classmethod
    def _absolute_local_path(cls, value: str) -> str:
        return _absolute_windows_path(value)

    @field_validator("arguments")
    @classmethod
    def _safe_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item or "\r" in item or "\n" in item
            for item in value
        ):
            raise ValueError("Nmap arguments must be bounded individual strings")
        return value

    @model_validator(mode="after")
    def _minimal_environment(self) -> NmapProcessLaunchV1:
        keys = tuple(key for key, _value in self.environment)
        if keys != ("SystemRoot", "TEMP", "TMP", "WINDIR"):
            raise ValueError("Nmap environment must contain only the fixed minimal keys")
        if self.inherited_handle_roles != ("stdout", "stderr"):
            raise ValueError("Nmap may inherit only its stdout and stderr pipe handles")
        for key, value in self.environment:
            if not value or len(value) > 2048 or "\x00" in value or "\r" in value or "\n" in value:
                raise ValueError(f"Nmap environment value {key} is invalid")
        return self


class NmapJobLimitsV1(BaseModel):
    """Limits applied with SetInformationJobObject before process resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    kill_on_job_close: Literal[True] = True
    active_process_limit: int = Field(default=4, ge=1, le=8)
    process_memory_bytes: int = Field(default=512 * 1024 * 1024, ge=64 * 1024 * 1024)
    job_memory_bytes: int = Field(default=1024 * 1024 * 1024, ge=128 * 1024 * 1024)
    cpu_time_seconds: float = Field(gt=0, le=3000)


class NmapRawXmlArtifactV1(BaseModel):
    """Opaque protected provider artifact returned by the evidence store."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    artifact_id: str = Field(
        min_length=8,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]+$",
    )
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=67_108_864)
    capture_complete: bool


class NmapProcessResultV1(BaseModel):
    """Sanitized execution result; no path or raw provider string is public."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: NmapProcessStatus
    reason: NmapProcessReason
    exit_code: int | None = None
    xml_artifact: NmapRawXmlArtifactV1 | None = None
    stderr_artifact: NmapRawXmlArtifactV1 | None = None
    stderr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_size_bytes: int = Field(default=0, ge=0, le=_STDERR_MAX_BYTES)
    process_tree_gone: bool
    duration_seconds: float = Field(ge=0, le=3600)
    runtime_capability: NmapRuntimeCapabilityV1 | None = None
    provenance: NmapProcessProvenanceV1 | None = None

    @model_validator(mode="after")
    def _consistent_process_result(self) -> NmapProcessResultV1:
        if self.stderr_artifact is not None and (
            self.stderr_artifact.sha256 != self.stderr_sha256
            or self.stderr_artifact.size_bytes != self.stderr_size_bytes
        ):
            raise ValueError("stderr artifact metadata does not match the process result")
        if self.status is NmapProcessStatus.SUCCEEDED and (
            self.reason is not NmapProcessReason.COMPLETED
            or self.exit_code != 0
            or not self.process_tree_gone
            or self.xml_artifact is None
            or not self.xml_artifact.capture_complete
            or self.stderr_artifact is None
            or not self.stderr_artifact.capture_complete
            or self.provenance is None
        ):
            raise ValueError("successful Nmap result requires complete bounded evidence")
        return self


class NmapXmlSpool(Protocol):
    def write(self, payload: bytes) -> None: ...

    def finalize(self, *, capture_complete: bool) -> NmapRawXmlArtifactV1: ...


class NmapPrivateArtifacts(Protocol):
    """Owner-only temporary target and protected raw-evidence operations."""

    def create_owner_only_target_list(self, payload: bytes) -> str: ...

    def create_owner_only_xml_spool(self, *, max_bytes: int) -> NmapXmlSpool: ...

    def create_owner_only_stderr_spool(self, *, max_bytes: int) -> NmapXmlSpool: ...

    def private_temp_directory(self) -> str: ...

    def remove_target_list(self, path: str) -> None: ...


class NmapWindowsProcessApi(Protocol):
    """Exact Win32 process and Job Object seam required by the runner."""

    def system_root(self) -> str: ...

    def monotonic(self) -> float: ...

    def create_kill_on_close_job(self, limits: NmapJobLimitsV1) -> object: ...

    def create_process_suspended(self, launch: NmapProcessLaunchV1) -> object: ...

    def assign_process_to_job(self, job: object, process: object) -> None: ...

    def resume_process(self, process: object) -> None: ...

    def read_pipe(self, process: object, stream: Literal["stdout", "stderr"], max_bytes: int) -> bytes: ...

    def wait_process(self, process: object, timeout_seconds: float) -> int | None: ...

    def terminate_job(self, job: object, exit_code: int) -> None: ...

    def terminate_suspended_process(
        self,
        process: object,
        exit_code: int,
        timeout_seconds: float,
    ) -> bool: ...

    def wait_job_empty(self, job: object, timeout_seconds: float) -> bool: ...

    def close_process(self, process: object) -> None: ...

    def close_job(self, job: object) -> None: ...


@dataclass
class _PipeCapture:
    stdout_spool: NmapXmlSpool
    stderr_spool: NmapXmlSpool
    stdout_limit: int
    stderr_limit: int = _STDERR_MAX_BYTES
    stdout_size: int = 0
    stderr_size: int = 0
    stderr_hash: object = field(default_factory=hashlib.sha256)
    overflow: Literal["stdout", "stderr"] | None = None
    failed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def preflight_nmap_runtime(
    *,
    plan: NmapScanPlanV1,
    deployment_mode: str,
    expected_executor_identity: str,
    revalidate_trust: Callable[[], NmapTrustResultV1],
    runtime_probe: NmapRuntimeCapabilityProbe,
) -> NmapRuntimePreflightV1:
    """Revalidate deployment, installation, machine, NIC, Npcap, and token state.

    The exact internal deployment gate is checked before reading local Nmap or
    Npcap state.  The injected snapshot must use in-process Windows registry,
    service-control, token, machine-identity, and NIC APIs without opening the
    Npcap driver or starting a process, so an admin-only Npcap configuration can
    never prompt for UAC during preflight.
    """

    expected_identity = str(expected_executor_identity).strip()
    if not expected_identity or len(expected_identity) > 255:
        raise ValueError("expected_executor_identity must contain 1 through 255 characters")
    if deployment_mode != "internal_operator_managed":
        return _blocked_preflight(NmapCapabilityState.EXTERNAL_MODE_BLOCKED)
    try:
        trust = NmapTrustResultV1.model_validate(revalidate_trust())
    except Exception:
        return _blocked_preflight(
            NmapCapabilityState.TRUST_REJECTED,
            trust_reason=NmapTrustReason.INSPECTION_FAILED,
        )
    if not trust.available or trust.fingerprint is None:
        state = _trust_capability_state(trust.reason)
        return _blocked_preflight(state, trust_reason=trust.reason)
    confirmed_scripts = {(script.name, script.sha256) for script in trust.fingerprint.reviewed_scripts}
    if any((script.name, script.sha256) not in confirmed_scripts for script in plan.reviewed_scripts):
        return _blocked_preflight(
            NmapCapabilityState.TRUST_REJECTED,
            trust_reason=NmapTrustReason.REVIEWED_SCRIPT_REJECTED,
        )
    try:
        snapshot = NmapRuntimeCapabilitySnapshotV1.model_validate(runtime_probe.snapshot())
    except Exception:
        return _blocked_preflight(NmapCapabilityState.CAPABILITY_PROBE_FAILED)
    if snapshot.executor_identity != expected_identity:
        return _blocked_preflight(
            NmapCapabilityState.EXECUTOR_MISMATCH,
            snapshot=snapshot,
        )
    matching_interface = next(
        (
            item
            for item in snapshot.interfaces
            if (item.interface_id, item.interface_name, item.source_ip)
            == (plan.interface_id, plan.interface_name, plan.source_ip)
        ),
        None,
    )
    if matching_interface is None:
        return _blocked_preflight(
            NmapCapabilityState.INTERFACE_UNAVAILABLE,
            snapshot=snapshot,
        )

    if not snapshot.npcap_installed:
        state = NmapCapabilityState.NPCAP_MISSING
        raw_available = False
    elif not snapshot.npcap_service_running:
        state = NmapCapabilityState.NPCAP_STOPPED
        raw_available = False
    elif snapshot.npcap_admin_only and not snapshot.current_token_is_administrator:
        state = NmapCapabilityState.NPCAP_ADMIN_ONLY
        raw_available = False
    elif snapshot.current_token_has_raw_rights:
        state = NmapCapabilityState.RAW_CAPABLE
        raw_available = True
    else:
        state = (
            NmapCapabilityState.INSUFFICIENT_RAW_RIGHTS
            if plan.raw_capability_required
            else NmapCapabilityState.INTERNAL_CONNECT_ONLY
        )
        raw_available = False
    if plan.raw_capability_required and raw_available and (
        plan.nmap_device_name is None
        or plan.nmap_device_name != nmap_device_name_for_interface_id(plan.interface_id)
        or matching_interface.nmap_device_name != plan.nmap_device_name
    ):
        return _blocked_preflight(
            NmapCapabilityState.INTERFACE_UNAVAILABLE,
            snapshot=snapshot,
        )
    launch_allowed = not plan.raw_capability_required or raw_available
    capability = _runtime_capability(
        state=state,
        snapshot=snapshot,
        connect_available=True,
        raw_available=raw_available,
        launch_allowed=launch_allowed,
        interface_available=True,
        trust_reason=NmapTrustReason.AVAILABLE,
    )
    if not launch_allowed:
        return NmapRuntimePreflightV1(capability=capability, fingerprint=None)
    return NmapRuntimePreflightV1(
        capability=capability,
        fingerprint=trust.fingerprint,
    )


def run_nmap_scan(
    *,
    plan: NmapScanPlanV1,
    deployment_mode: str,
    expected_executor_identity: str,
    revalidate_trust: Callable[[], NmapTrustResultV1],
    runtime_probe: NmapRuntimeCapabilityProbe,
    control_guard: Callable[[], None],
    artifacts: NmapPrivateArtifacts,
    windows: NmapWindowsProcessApi,
    poll_interval_seconds: float = 0.1,
) -> NmapProcessResultV1:
    """Launch one sealed Nmap batch and contain its complete process tree."""

    if not 0.01 <= poll_interval_seconds <= 1.0:
        raise ValueError("poll_interval_seconds must be between 0.01 and 1.0")
    started_at = windows.monotonic()
    control_failure = _check_control(control_guard)
    if control_failure is not None:
        return _early_result(control_failure, started_at=started_at, windows=windows)

    preflight = preflight_nmap_runtime(
        plan=plan,
        deployment_mode=deployment_mode,
        expected_executor_identity=expected_executor_identity,
        revalidate_trust=revalidate_trust,
        runtime_probe=runtime_probe,
    )
    if not preflight.capability.launch_allowed or preflight.fingerprint is None:
        reason = (
            NmapProcessReason.TRUST_UNAVAILABLE
            if preflight.capability.trust_reason not in {None, NmapTrustReason.AVAILABLE}
            else NmapProcessReason.CAPABILITY_UNAVAILABLE
        )
        return _early_result(
            reason,
            started_at=started_at,
            windows=windows,
            runtime_capability=preflight.capability,
        )
    fingerprint = preflight.fingerprint
    provenance = _process_provenance(
        plan=plan,
        fingerprint=fingerprint,
        capability=preflight.capability,
    )

    target_list_path: str | None = None
    spool: NmapXmlSpool | None = None
    stderr_spool: NmapXmlSpool | None = None
    job: object | None = None
    process: object | None = None
    assigned = False
    resumed = False
    artifact: NmapRawXmlArtifactV1 | None = None
    stderr_artifact: NmapRawXmlArtifactV1 | None = None
    capture: _PipeCapture | None = None
    readers: tuple[threading.Thread, threading.Thread] = ()
    exit_code: int | None = None
    process_tree_gone = True
    reason = NmapProcessReason.LAUNCH_FAILED
    cleanup_deadline: float | None = None

    try:
        private_temp = _absolute_windows_path(artifacts.private_temp_directory())
        created_target_path = _absolute_windows_path(
            artifacts.create_owner_only_target_list(render_nmap_target_list(plan))
        )
        if not _strictly_contained(private_temp, created_target_path):
            raise ValueError("Nmap target list must remain inside the owner-only temp directory")
        target_list_path = created_target_path
        spool = artifacts.create_owner_only_xml_spool(max_bytes=plan.output_max_bytes)
        stderr_spool = artifacts.create_owner_only_stderr_spool(max_bytes=_STDERR_MAX_BYTES)
        capture = _PipeCapture(
            stdout_spool=spool,
            stderr_spool=stderr_spool,
            stdout_limit=plan.output_max_bytes,
        )

        control_failure = _check_control(control_guard)
        if control_failure is not None:
            reason = control_failure
            early_xml_artifact = spool.finalize(capture_complete=False)
            early_stderr_artifact = stderr_spool.finalize(capture_complete=False)
            return _final_result(
                reason=reason,
                exit_code=None,
                artifact=early_xml_artifact,
                stderr_artifact=early_stderr_artifact,
                stderr_sha256=early_stderr_artifact.sha256,
                stderr_size=early_stderr_artifact.size_bytes,
                process_tree_gone=True,
                started_at=started_at,
                windows=windows,
                runtime_capability=preflight.capability,
                provenance=provenance,
            )

        try:
            execution_trust = NmapTrustResultV1.model_validate(revalidate_trust())
        except Exception:
            execution_trust = NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.INSPECTION_FAILED,
                fingerprint=None,
            )
        if (
            not execution_trust.available
            or execution_trust.fingerprint is None
            or execution_trust.fingerprint != fingerprint
        ):
            drift_xml_artifact = spool.finalize(capture_complete=False)
            drift_stderr_artifact = stderr_spool.finalize(capture_complete=False)
            return _final_result(
                reason=NmapProcessReason.TRUST_UNAVAILABLE,
                exit_code=None,
                artifact=drift_xml_artifact,
                stderr_artifact=drift_stderr_artifact,
                stderr_sha256=drift_stderr_artifact.sha256,
                stderr_size=drift_stderr_artifact.size_bytes,
                process_tree_gone=True,
                started_at=started_at,
                windows=windows,
                runtime_capability=preflight.capability,
                provenance=provenance,
            )

        arguments = build_nmap_subprocess_arguments(
            plan,
            data_directory=fingerprint.data_directory,
            target_list_path=target_list_path,
        )
        system_root = _absolute_windows_path(windows.system_root())
        launch = NmapProcessLaunchV1(
            application_name=fingerprint.executable_path,
            arguments=arguments,
            working_directory=fingerprint.install_root,
            environment=(
                ("SystemRoot", system_root),
                ("TEMP", private_temp),
                ("TMP", private_temp),
                ("WINDIR", system_root),
            ),
        )
        limits = NmapJobLimitsV1(cpu_time_seconds=plan.parent_deadline_seconds)
        job = windows.create_kill_on_close_job(limits)
        try:
            process = windows.create_process_suspended(launch)
            process_tree_gone = False
        except Exception:
            reason = NmapProcessReason.LAUNCH_FAILED
            raise
        try:
            windows.assign_process_to_job(job, process)
            assigned = True
        except Exception:
            cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
            process_tree_gone = bool(
                windows.terminate_suspended_process(
                    process,
                    _JOB_TERMINATION_EXIT_CODE,
                    _cleanup_time_left(windows, cleanup_deadline),
                )
            )
            reason = NmapProcessReason.JOB_ASSIGNMENT_FAILED
            raise

        control_failure = _check_control(control_guard)
        if control_failure is not None:
            reason = control_failure
            cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
            windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
            process_tree_gone = windows.wait_job_empty(
                job,
                _cleanup_time_left(windows, cleanup_deadline),
            )
        else:
            readers = (
                _start_reader(windows, process, "stdout", capture),
                _start_reader(windows, process, "stderr", capture),
            )
            try:
                windows.resume_process(process)
                resumed = True
            except Exception:
                reason = NmapProcessReason.LAUNCH_FAILED
                cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
                windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
                process_tree_gone = windows.wait_job_empty(
                    job,
                    _cleanup_time_left(windows, cleanup_deadline),
                )
                if not process_tree_gone:
                    reason = NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
                    windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
                    process_tree_gone = windows.wait_job_empty(
                        job,
                        _cleanup_time_left(windows, cleanup_deadline),
                    )
            if resumed:
                exit_code, reason = _wait_for_process(
                    plan=plan,
                    process=process,
                    job=job,
                    capture=capture,
                    control_guard=control_guard,
                    windows=windows,
                    poll_interval_seconds=poll_interval_seconds,
                    started_at=started_at,
                )
                forced = reason is not NmapProcessReason.COMPLETED
                cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
                if forced:
                    windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
                    try:
                        exit_code = windows.wait_process(
                            process,
                            _cleanup_time_left(windows, cleanup_deadline),
                        )
                    except Exception:
                        exit_code = None
                process_tree_gone = windows.wait_job_empty(
                    job,
                    _cleanup_time_left(windows, cleanup_deadline),
                )
                if not process_tree_gone:
                    reason = NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
                    windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
                    process_tree_gone = windows.wait_job_empty(
                        job,
                        _cleanup_time_left(windows, cleanup_deadline),
                    )

        if readers:
            cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
        for reader in readers:
            reader.join(timeout=_cleanup_time_left(windows, cleanup_deadline))
        if any(reader.is_alive() for reader in readers):
            reason = NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
            process_tree_gone = False
        if capture.failed and reason is NmapProcessReason.COMPLETED:
            reason = NmapProcessReason.PIPE_FAILURE
        if not process_tree_gone:
            reason = NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
        if reason is NmapProcessReason.COMPLETED and exit_code != 0:
            reason = NmapProcessReason.PROCESS_NONZERO
        artifact = spool.finalize(
            capture_complete=(
                capture.overflow is None
                and not capture.failed
                and process_tree_gone
                and reason in {NmapProcessReason.COMPLETED, NmapProcessReason.PROCESS_NONZERO}
            )
        )
        stderr_artifact = stderr_spool.finalize(
            capture_complete=(
                capture.overflow is None
                and not capture.failed
                and process_tree_gone
                and reason in {NmapProcessReason.COMPLETED, NmapProcessReason.PROCESS_NONZERO}
            )
        )
        if reason is NmapProcessReason.COMPLETED and (
            not artifact.capture_complete or not stderr_artifact.capture_complete
        ):
            reason = NmapProcessReason.PIPE_FAILURE
        return _final_result(
            reason=reason,
            exit_code=exit_code,
            artifact=artifact,
            stderr_artifact=stderr_artifact,
            stderr_sha256=stderr_artifact.sha256,
            stderr_size=stderr_artifact.size_bytes,
            process_tree_gone=process_tree_gone,
            started_at=started_at,
            windows=windows,
            runtime_capability=preflight.capability,
            provenance=provenance,
        )
    except Exception:
        if assigned and job is not None and not process_tree_gone:
            try:
                cleanup_deadline = _begin_cleanup_deadline(windows, cleanup_deadline)
                windows.terminate_job(job, _JOB_TERMINATION_EXIT_CODE)
                process_tree_gone = windows.wait_job_empty(
                    job,
                    _cleanup_time_left(windows, cleanup_deadline),
                )
            except Exception:
                process_tree_gone = False
        if spool is not None and artifact is None:
            try:
                artifact = spool.finalize(capture_complete=False)
            except Exception:
                artifact = None
        if stderr_spool is not None and stderr_artifact is None:
            try:
                stderr_artifact = stderr_spool.finalize(capture_complete=False)
            except Exception:
                stderr_artifact = None
        if not process_tree_gone and process is not None:
            reason = NmapProcessReason.PROCESS_TREE_CLEANUP_FAILED
        return _final_result(
            reason=reason,
            exit_code=exit_code,
            artifact=artifact,
            stderr_artifact=stderr_artifact,
            stderr_sha256=(
                stderr_artifact.sha256
                if stderr_artifact is not None
                else capture.stderr_hash.hexdigest()
                if capture is not None
                else None
            ),
            stderr_size=(
                stderr_artifact.size_bytes
                if stderr_artifact is not None
                else capture.stderr_size
                if capture is not None
                else 0
            ),
            process_tree_gone=process_tree_gone,
            started_at=started_at,
            windows=windows,
            runtime_capability=preflight.capability,
            provenance=provenance,
        )
    finally:
        if process is not None:
            try:
                windows.close_process(process)
            except Exception:
                pass
        if job is not None:
            try:
                windows.close_job(job)
            except Exception:
                pass
        if target_list_path is not None:
            try:
                artifacts.remove_target_list(target_list_path)
            except Exception:
                pass


def _start_reader(
    windows: NmapWindowsProcessApi,
    process: object,
    stream: Literal["stdout", "stderr"],
    capture: _PipeCapture,
) -> threading.Thread:
    reader = threading.Thread(
        target=_drain_pipe,
        args=(windows, process, stream, capture),
        name=f"nmap-{stream}-drain",
        daemon=True,
    )
    reader.start()
    return reader


def _begin_cleanup_deadline(
    windows: NmapWindowsProcessApi,
    current: float | None,
) -> float:
    return current if current is not None else windows.monotonic() + _JOB_CLEANUP_SECONDS


def _cleanup_time_left(windows: NmapWindowsProcessApi, deadline: float | None) -> float:
    if deadline is None:
        raise ValueError("Nmap cleanup deadline was not initialized")
    return max(0.0, deadline - windows.monotonic())


def _drain_pipe(
    windows: NmapWindowsProcessApi,
    process: object,
    stream: Literal["stdout", "stderr"],
    capture: _PipeCapture,
) -> None:
    try:
        while True:
            chunk = windows.read_pipe(process, stream, _PIPE_CHUNK_BYTES)
            if not isinstance(chunk, bytes):
                raise TypeError("Windows pipe adapter returned non-bytes")
            if not chunk:
                return
            with capture.lock:
                current = capture.stdout_size if stream == "stdout" else capture.stderr_size
                limit = capture.stdout_limit if stream == "stdout" else capture.stderr_limit
                remaining = max(0, limit - current)
                retained = chunk[:remaining]
                if stream == "stdout":
                    if retained:
                        capture.stdout_spool.write(retained)
                    capture.stdout_size += len(retained)
                else:
                    if retained:
                        capture.stderr_spool.write(retained)
                    capture.stderr_hash.update(retained)
                    capture.stderr_size += len(retained)
                if len(chunk) > remaining:
                    capture.overflow = stream
                    return
    except Exception:
        with capture.lock:
            capture.failed = True


def _wait_for_process(
    *,
    plan: NmapScanPlanV1,
    process: object,
    job: object,
    capture: _PipeCapture,
    control_guard: Callable[[], None],
    windows: NmapWindowsProcessApi,
    poll_interval_seconds: float,
    started_at: float,
) -> tuple[int | None, NmapProcessReason]:
    deadline = started_at + plan.parent_deadline_seconds
    while True:
        with capture.lock:
            overflow = capture.overflow
            pipe_failed = capture.failed
        if overflow == "stdout":
            return None, NmapProcessReason.STDOUT_LIMIT_EXCEEDED
        if overflow == "stderr":
            return None, NmapProcessReason.STDERR_LIMIT_EXCEEDED
        if pipe_failed:
            return None, NmapProcessReason.PIPE_FAILURE
        control_failure = _check_control(control_guard)
        if control_failure is not None:
            return None, control_failure
        remaining = deadline - windows.monotonic()
        if remaining <= 0:
            return None, NmapProcessReason.DEADLINE_EXCEEDED
        try:
            exit_code = windows.wait_process(process, min(poll_interval_seconds, remaining))
        except Exception:
            return None, NmapProcessReason.LAUNCH_FAILED
        if exit_code is not None:
            return exit_code, NmapProcessReason.COMPLETED


def _check_control(control_guard: Callable[[], None]) -> NmapProcessReason | None:
    try:
        control_guard()
    except Exception as error:
        reason = str(getattr(error, "reason", "")).casefold()
        text = f"{reason} {error}".casefold()
        if "stop" in text or "cancel" in text:
            return NmapProcessReason.STOP_REQUESTED
        if "authorization" in text or "grant" in text or "initiating user" in text:
            return NmapProcessReason.AUTHORIZATION_LOST
        if "owner" in text or "attempt" in text or "lease" in text:
            return NmapProcessReason.OWNERSHIP_LOST
        if "deadline" in text or "window" in text or "expir" in text:
            return NmapProcessReason.DEADLINE_EXCEEDED
        return NmapProcessReason.CONTROL_STORE_FAILURE
    return None


def _trust_capability_state(reason: NmapTrustReason) -> NmapCapabilityState:
    if reason is NmapTrustReason.VERSION_REJECTED:
        return NmapCapabilityState.VERSION_REJECTED
    if reason in {
        NmapTrustReason.PATH_REJECTED,
        NmapTrustReason.REPARSE_REJECTED,
        NmapTrustReason.ACL_REJECTED,
        NmapTrustReason.DATA_MANIFEST_REJECTED,
    }:
        return NmapCapabilityState.PATH_OR_DATA_REJECTED
    return NmapCapabilityState.TRUST_REJECTED


def _blocked_preflight(
    state: NmapCapabilityState,
    *,
    trust_reason: NmapTrustReason | None = None,
    snapshot: NmapRuntimeCapabilitySnapshotV1 | None = None,
) -> NmapRuntimePreflightV1:
    return NmapRuntimePreflightV1(
        capability=_runtime_capability(
            state=state,
            snapshot=snapshot,
            connect_available=False,
            raw_available=False,
            launch_allowed=False,
            interface_available=False,
            trust_reason=trust_reason,
        ),
        fingerprint=None,
    )


def _runtime_capability(
    *,
    state: NmapCapabilityState,
    snapshot: NmapRuntimeCapabilitySnapshotV1 | None,
    connect_available: bool,
    raw_available: bool,
    launch_allowed: bool,
    interface_available: bool,
    trust_reason: NmapTrustReason | None,
) -> NmapRuntimeCapabilityV1:
    return NmapRuntimeCapabilityV1(
        state=state,
        executor_identity=snapshot.executor_identity if snapshot is not None else None,
        trust_reason=trust_reason,
        connect_available=connect_available,
        raw_available=raw_available,
        launch_allowed=launch_allowed,
        interface_available=interface_available,
        npcap_installed=snapshot.npcap_installed if snapshot is not None else None,
        npcap_version=snapshot.npcap_version if snapshot is not None else None,
        npcap_service_running=(snapshot.npcap_service_running if snapshot is not None else None),
        npcap_admin_only=snapshot.npcap_admin_only if snapshot is not None else None,
        current_token_is_administrator=(snapshot.current_token_is_administrator if snapshot is not None else None),
    )


def _absolute_windows_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ch in value for ch in "\x00\r\n"):
        raise ValueError("Nmap runner paths must be absolute local Windows paths")
    normalized = ntpath.normpath(value.strip())
    drive, tail = ntpath.splitdrive(normalized)
    if (
        not re.fullmatch(r"[A-Za-z]:", drive)
        or not tail.startswith("\\")
        or any(character in tail for character in '<>:"|?*')
    ):
        raise ValueError("Nmap runner paths must be absolute local Windows paths")
    return normalized


def _strictly_contained(parent: str, child: str) -> bool:
    try:
        common = ntpath.normcase(ntpath.commonpath((parent, child)))
    except ValueError:
        return False
    return common == ntpath.normcase(parent) and ntpath.normcase(child) != ntpath.normcase(parent)


def _process_provenance(
    *,
    plan: NmapScanPlanV1,
    fingerprint: NmapInstallationFingerprintV1,
    capability: NmapRuntimeCapabilityV1,
) -> NmapProcessProvenanceV1:
    if capability.executor_identity is None:
        raise ValueError("launchable Nmap capability is missing executor identity")
    return NmapProcessProvenanceV1(
        profile=plan.profile,
        profile_fingerprint=plan.profile_fingerprint,
        packet_plan_sha256=plan.packet_plan_sha256,
        nmap_version=fingerprint.version,
        publisher=fingerprint.publisher,
        installation_fingerprint_sha256=fingerprint.fingerprint_sha256,
        executor_identity=capability.executor_identity,
        interface_id=plan.interface_id,
        interface_name=plan.interface_name,
        source_ip=plan.source_ip,
        npcap_version=capability.npcap_version,
    )


def _early_result(
    reason: NmapProcessReason,
    *,
    started_at: float,
    windows: NmapWindowsProcessApi,
    runtime_capability: NmapRuntimeCapabilityV1 | None = None,
) -> NmapProcessResultV1:
    return _final_result(
        reason=reason,
        exit_code=None,
        artifact=None,
        stderr_artifact=None,
        stderr_sha256=None,
        stderr_size=0,
        process_tree_gone=True,
        started_at=started_at,
        windows=windows,
        runtime_capability=runtime_capability,
    )


def _final_result(
    *,
    reason: NmapProcessReason,
    exit_code: int | None,
    artifact: NmapRawXmlArtifactV1 | None,
    stderr_artifact: NmapRawXmlArtifactV1 | None,
    stderr_sha256: str | None,
    stderr_size: int,
    process_tree_gone: bool,
    started_at: float,
    windows: NmapWindowsProcessApi,
    runtime_capability: NmapRuntimeCapabilityV1 | None = None,
    provenance: NmapProcessProvenanceV1 | None = None,
) -> NmapProcessResultV1:
    if reason is NmapProcessReason.COMPLETED and exit_code == 0 and process_tree_gone:
        status = NmapProcessStatus.SUCCEEDED
    elif reason in {
        NmapProcessReason.TRUST_UNAVAILABLE,
        NmapProcessReason.CAPABILITY_UNAVAILABLE,
    }:
        status = NmapProcessStatus.UNAVAILABLE
    elif reason in {
        NmapProcessReason.STOP_REQUESTED,
        NmapProcessReason.AUTHORIZATION_LOST,
        NmapProcessReason.OWNERSHIP_LOST,
    }:
        status = NmapProcessStatus.CANCELLED
    else:
        status = NmapProcessStatus.FAILED
    duration = max(0.0, min(3600.0, windows.monotonic() - started_at))
    return NmapProcessResultV1(
        status=status,
        reason=reason,
        exit_code=exit_code,
        xml_artifact=artifact,
        stderr_artifact=stderr_artifact,
        stderr_sha256=stderr_sha256,
        stderr_size_bytes=stderr_size,
        process_tree_gone=process_tree_gone,
        duration_seconds=duration,
        runtime_capability=runtime_capability,
        provenance=provenance,
    )
