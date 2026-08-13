"""Bounded, non-resolving normalization for complete Nmap XML output.

The adapter uses Expat as an event parser and never constructs a general XML
DOM.  DTDs, entity declarations, external entities, processing instructions,
and XInclude are rejected by handlers.  Unknown ordinary elements are ignored
within global size, depth, element, attribute, text, host, port, and time caps.
Script output is represented only by its digest and byte count; the complete
source XML belongs in the protected artifact store.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
import time
from collections.abc import Callable
from typing import Any, Literal
from xml.parsers import expat

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PORT_STATES = frozenset(
    {"open", "closed", "filtered", "unfiltered", "open|filtered", "closed|filtered"}
)
_PROTOCOLS = frozenset({"tcp", "udp"})
_SNAKE_OR_HYPHEN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_PUBLIC_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:+()#,@'-]*$")
_PROTECTED_MARKER = re.compile(
    r"(?:[A-Za-z]:\\|/Users/|/home/|\.\.?/|file:|secret:|"
    r"(?:password|passwd|token|authorization|auth_token|secret)\s*[:=]|-----BEGIN)",
    re.IGNORECASE,
)
_XINCLUDE_NAMESPACE = "http://www.w3.org/2001/XInclude"


class NmapXmlParseError(ValueError):
    """One sanitized fail-closed XML normalization reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Nmap XML could not be normalized: {reason}")


class NmapXmlLimitsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=256, le=67_108_864)
    max_depth: int = Field(default=32, ge=2, le=128)
    max_elements: int = Field(default=200_000, ge=4, le=1_000_000)
    max_attributes: int = Field(default=400_000, ge=4, le=2_000_000)
    max_text_bytes: int = Field(default=8 * 1024 * 1024, ge=16, le=67_108_864)
    max_hosts: int = Field(default=4096, ge=0, le=4096)
    max_ports_per_host: int = Field(default=4096, ge=0, le=4096)
    max_total_ports: int = Field(default=14_000, ge=0, le=50_000)
    max_parse_seconds: float = Field(default=5.0, gt=0, le=60)


class NmapScriptEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    script_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_bytes: int = Field(ge=0)


class NmapPortObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["tcp", "udp"]
    port: int = Field(ge=1, le=65535)
    state: Literal[
        "open",
        "closed",
        "filtered",
        "unfiltered",
        "open|filtered",
        "closed|filtered",
    ]
    reason: str | None = Field(default=None, max_length=64)
    detected_service: str | None = Field(default=None, max_length=64)
    detected_product: str | None = Field(default=None, max_length=128)
    detected_version: str | None = Field(default=None, max_length=128)
    script_evidence: tuple[NmapScriptEvidenceV1, ...] = ()

    @property
    def script_ids(self) -> tuple[str, ...]:
        return tuple(item.script_id for item in self.script_evidence)

    @property
    def script_output_sha256(self) -> str:
        if not self.script_evidence:
            return ""
        digest = hashlib.sha256()
        for item in self.script_evidence:
            digest.update(item.script_id.encode("ascii"))
            digest.update(bytes.fromhex(item.output_sha256))
        return digest.hexdigest()


class NmapTraceHopV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: int = Field(ge=1, le=255)
    address: str
    rtt_ms: float | None = Field(default=None, ge=0)

    @field_validator("address")
    @classmethod
    def _numeric_ipv4(cls, value: str) -> str:
        return _numeric_ipv4(value, label="trace address")


class NmapHostObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str
    state: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=64)
    hostname: str | None = Field(default=None, max_length=253)
    ports: tuple[NmapPortObservationV1, ...] = ()
    os_name: str | None = Field(default=None, max_length=128)
    os_accuracy: int | None = Field(default=None, ge=0, le=100)
    trace: tuple[NmapTraceHopV1, ...] = ()

    @field_validator("address")
    @classmethod
    def _numeric_ipv4(cls, value: str) -> str:
        return _numeric_ipv4(value, label="host address")


class NmapXmlResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    parser_contract_version: Literal["1.0"] = "1.0"
    nmap_version: str = Field(max_length=32)
    xml_output_version: str = Field(max_length=32)
    finished_exit: Literal["success"]
    elapsed_seconds: float | None = Field(default=None, ge=0)
    total_hosts: int = Field(ge=0, le=4096)
    up_hosts: int = Field(ge=0, le=4096)
    down_hosts: int = Field(ge=0, le=4096)
    hosts: tuple[NmapHostObservationV1, ...] = ()


class _NmapXmlCollector:
    def __init__(self, *, limits: NmapXmlLimitsV1, clock: Callable[[], float]) -> None:
        self.limits = limits
        self.clock = clock
        self.started_at = clock()
        self.depth = 0
        self.stack: list[str] = []
        self.elements = 0
        self.attributes = 0
        self.text_bytes = 0
        self.total_ports = 0
        self.root_seen = False
        self.root_closed = False
        self.nmap_version: str | None = None
        self.xml_output_version: str | None = None
        self.finished_exit: str | None = None
        self.finished_seen = False
        self.elapsed_seconds: float | None = None
        self.total_hosts: int | None = None
        self.up_hosts: int | None = None
        self.down_hosts: int | None = None
        self.host_summary_seen = False
        self.hosts: list[dict[str, Any]] = []
        self.current_host: dict[str, Any] | None = None
        self.current_port: dict[str, Any] | None = None
        self.current_script: dict[str, Any] | None = None

    def _tick(self) -> None:
        if self.clock() - self.started_at > self.limits.max_parse_seconds:
            raise NmapXmlParseError("parse_time_limit")

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self._tick()
        self.depth += 1
        self.elements += 1
        self.attributes += len(attributes)
        if self.depth > self.limits.max_depth:
            raise NmapXmlParseError("depth_limit")
        if self.elements > self.limits.max_elements:
            raise NmapXmlParseError("element_limit")
        if self.attributes > self.limits.max_attributes:
            raise NmapXmlParseError("attribute_limit")
        local_name = _local_name(name)
        self.stack.append(local_name)
        path = tuple(self.stack)
        if name.startswith(_XINCLUDE_NAMESPACE + "}") or local_name.lower() == "include" and (
            "href" in attributes and "parse" in attributes
        ):
            raise NmapXmlParseError("xinclude_forbidden")
        if path == ("nmaprun",):
            if local_name != "nmaprun" or self.root_seen:
                raise NmapXmlParseError("invalid_root")
            self.root_seen = True
            if attributes.get("scanner") != "nmap":
                raise NmapXmlParseError("invalid_scanner")
            self.nmap_version = _bounded_version(attributes.get("version"), label="nmap_version")
            self.xml_output_version = _bounded_version(
                attributes.get("xmloutputversion"),
                label="xml_output_version",
            )
            return
        if path == ("nmaprun", "host"):
            if self.current_host is not None:
                raise NmapXmlParseError("nested_host")
            if len(self.hosts) >= self.limits.max_hosts:
                raise NmapXmlParseError("host_limit")
            self.current_host = {
                "address": None,
                "state": None,
                "reason": None,
                "hostname": None,
                "ports": [],
                "os_name": None,
                "os_accuracy": None,
                "trace": [],
            }
        elif path == ("nmaprun", "host", "status") and self.current_host is not None:
            state = attributes.get("state")
            if state not in {"up", "down"}:
                raise NmapXmlParseError("unsupported_host_state")
            self.current_host["state"] = state
            self.current_host["reason"] = _safe_token(attributes.get("reason"), label="host_reason")
        elif path == ("nmaprun", "host", "address") and self.current_host is not None:
            if attributes.get("addrtype") == "ipv4" and self.current_host["address"] is None:
                self.current_host["address"] = _numeric_ipv4(
                    attributes.get("addr"),
                    label="host address",
                )
        elif path == ("nmaprun", "host", "hostnames", "hostname") and self.current_host is not None:
            if self.current_host["hostname"] is None:
                self.current_host["hostname"] = _safe_hostname(attributes.get("name"))
        elif path == ("nmaprun", "host", "ports", "port") and self.current_host is not None:
            if self.current_port is not None:
                raise NmapXmlParseError("nested_port")
            if len(self.current_host["ports"]) >= self.limits.max_ports_per_host:
                raise NmapXmlParseError("port_per_host_limit")
            self.total_ports += 1
            if self.total_ports > self.limits.max_total_ports:
                raise NmapXmlParseError("total_port_limit")
            protocol = attributes.get("protocol")
            if protocol not in _PROTOCOLS:
                raise NmapXmlParseError("unsupported_port_protocol")
            port = _bounded_integer(attributes.get("portid"), 1, 65535, label="port")
            self.current_port = {
                "protocol": protocol,
                "port": port,
                "state": None,
                "reason": None,
                "detected_service": None,
                "detected_product": None,
                "detected_version": None,
                "script_evidence": [],
            }
        elif path == ("nmaprun", "host", "ports", "port", "state") and self.current_port is not None:
            state = attributes.get("state")
            if state not in _PORT_STATES:
                raise NmapXmlParseError("unsupported_port_state")
            self.current_port["state"] = state
            self.current_port["reason"] = _safe_token(attributes.get("reason"), label="port_reason")
        elif path == ("nmaprun", "host", "ports", "port", "service") and self.current_port is not None:
            self.current_port["detected_service"] = _safe_public_text(
                attributes.get("name"),
                label="detected_service",
                limit=64,
            )
            self.current_port["detected_product"] = _safe_public_text(
                attributes.get("product"),
                label="detected_product",
                limit=128,
            )
            self.current_port["detected_version"] = _safe_public_text(
                attributes.get("version"),
                label="detected_version",
                limit=128,
            )
        elif path == ("nmaprun", "host", "ports", "port", "script") and self.current_port is not None:
            if self.current_script is not None:
                raise NmapXmlParseError("nested_script")
            script_id = _safe_token(attributes.get("id"), label="script_id", limit=128)
            if script_id is None:
                raise NmapXmlParseError("script_id_missing")
            digest = hashlib.sha256()
            output = attributes.get("output", "").encode("utf-8")
            digest.update(output)
            self.current_script = {
                "script_id": script_id,
                "digest": digest,
                "output_bytes": len(output),
            }
        elif path == ("nmaprun", "host", "os", "osmatch") and self.current_host is not None:
            if self.current_host["os_name"] is None:
                self.current_host["os_name"] = _safe_public_text(
                    attributes.get("name"),
                    label="os_name",
                    limit=128,
                )
                accuracy = attributes.get("accuracy")
                self.current_host["os_accuracy"] = (
                    _bounded_integer(accuracy, 0, 100, label="os_accuracy")
                    if accuracy is not None
                    else None
                )
        elif path == ("nmaprun", "host", "trace", "hop") and self.current_host is not None:
            address = attributes.get("ipaddr")
            if address:
                rtt_text = attributes.get("rtt")
                rtt = _bounded_float(rtt_text, label="trace_rtt") if rtt_text else None
                self.current_host["trace"].append(
                    {
                        "ttl": _bounded_integer(attributes.get("ttl"), 1, 255, label="trace_ttl"),
                        "address": _numeric_ipv4(address, label="trace address"),
                        "rtt_ms": rtt,
                    }
                )
        elif path == ("nmaprun", "runstats", "finished"):
            if self.finished_seen:
                raise NmapXmlParseError("duplicate_finished")
            self.finished_seen = True
            self.finished_exit = attributes.get("exit")
            elapsed = attributes.get("elapsed")
            self.elapsed_seconds = _bounded_float(elapsed, label="elapsed") if elapsed else None
        elif path == ("nmaprun", "runstats", "hosts"):
            if self.host_summary_seen:
                raise NmapXmlParseError("duplicate_host_summary")
            self.host_summary_seen = True
            self.up_hosts = _bounded_integer(attributes.get("up"), 0, 4096, label="hosts_up")
            self.down_hosts = _bounded_integer(
                attributes.get("down"),
                0,
                4096,
                label="hosts_down",
            )
            self.total_hosts = _bounded_integer(
                attributes.get("total"),
                0,
                4096,
                label="hosts_total",
            )

    def end(self, name: str) -> None:
        self._tick()
        local_name = _local_name(name)
        path = tuple(self.stack)
        if not self.stack or self.stack[-1] != local_name:
            raise NmapXmlParseError("invalid_element_nesting")
        if path == ("nmaprun", "host", "ports", "port", "script") and self.current_script is not None:
            assert self.current_port is not None
            self.current_port["script_evidence"].append(
                {
                    "script_id": self.current_script["script_id"],
                    "output_sha256": self.current_script["digest"].hexdigest(),
                    "output_bytes": self.current_script["output_bytes"],
                }
            )
            self.current_script = None
        elif path == ("nmaprun", "host", "ports", "port") and self.current_port is not None:
            if self.current_port["state"] is None:
                raise NmapXmlParseError("port_state_missing")
            assert self.current_host is not None
            self.current_host["ports"].append(self.current_port)
            self.current_port = None
        elif path == ("nmaprun", "host") and self.current_host is not None:
            if self.current_host["address"] is None:
                raise NmapXmlParseError("ipv4_host_address_missing")
            if self.current_host["state"] is None:
                raise NmapXmlParseError("host_state_missing")
            self.current_host["ports"].sort(key=lambda item: (item["protocol"], item["port"]))
            self.hosts.append(self.current_host)
            self.current_host = None
        elif path == ("nmaprun",):
            self.root_closed = True
        self.stack.pop()
        self.depth -= 1

    def text(self, value: str) -> None:
        self._tick()
        encoded = value.encode("utf-8")
        self.text_bytes += len(encoded)
        if self.text_bytes > self.limits.max_text_bytes:
            raise NmapXmlParseError("text_limit")
        if self.current_script is not None:
            self.current_script["digest"].update(encoded)
            self.current_script["output_bytes"] += len(encoded)

    def forbidden(self, *_args: object) -> None:
        raise NmapXmlParseError("active_xml_content_forbidden")

    def result(self) -> NmapXmlResultV1:
        if not self.root_seen or not self.root_closed:
            raise NmapXmlParseError("incomplete_document")
        if not self.finished_seen or self.finished_exit != "success":
            raise NmapXmlParseError("run_not_successfully_finished")
        if not self.host_summary_seen:
            raise NmapXmlParseError("host_summary_missing")
        if self.nmap_version is None or self.xml_output_version is None:
            raise NmapXmlParseError("version_missing")
        self.hosts.sort(key=lambda item: int(ipaddress.ip_address(item["address"])))
        if len({item["address"] for item in self.hosts}) != len(self.hosts):
            raise NmapXmlParseError("duplicate_host_address")
        if self.total_hosts is None or self.up_hosts is None or self.down_hosts is None:
            raise NmapXmlParseError("host_summary_missing")
        total = self.total_hosts
        up = self.up_hosts
        down = self.down_hosts
        if total != up + down or total < len(self.hosts):
            raise NmapXmlParseError("host_count_mismatch")
        return NmapXmlResultV1(
            nmap_version=self.nmap_version,
            xml_output_version=self.xml_output_version,
            finished_exit="success",
            elapsed_seconds=self.elapsed_seconds,
            total_hosts=total,
            up_hosts=up,
            down_hosts=down,
            hosts=tuple(NmapHostObservationV1.model_validate(item) for item in self.hosts),
        )


def parse_nmap_xml(
    payload: bytes,
    *,
    limits: NmapXmlLimitsV1 | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> NmapXmlResultV1:
    """Normalize one complete, bounded XML artifact after process close."""

    active_limits = limits or NmapXmlLimitsV1()
    if not isinstance(payload, bytes):
        raise TypeError("Nmap XML payload must be bytes")
    if len(payload) > active_limits.max_bytes:
        raise NmapXmlParseError("byte_limit")
    collector = _NmapXmlCollector(limits=active_limits, clock=clock)
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator="}")
    parser.buffer_text = True
    parser.StartElementHandler = collector.start
    parser.EndElementHandler = collector.end
    parser.CharacterDataHandler = collector.text
    parser.ProcessingInstructionHandler = collector.forbidden
    parser.StartDoctypeDeclHandler = collector.forbidden
    parser.EntityDeclHandler = collector.forbidden
    parser.UnparsedEntityDeclHandler = collector.forbidden
    parser.ExternalEntityRefHandler = lambda *_args: collector.forbidden()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(payload, True)
    except NmapXmlParseError:
        raise
    except (UnicodeError, expat.ExpatError, ValueError, OverflowError) as error:
        raise NmapXmlParseError("malformed_or_unsupported_xml") from error
    return collector.result()


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _numeric_ipv4(value: object, *, label: str) -> str:
    try:
        parsed = ipaddress.ip_address(str(value))
    except ValueError as error:
        raise NmapXmlParseError(f"invalid_{label.replace(' ', '_')}") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise NmapXmlParseError(f"invalid_{label.replace(' ', '_')}")
    return str(parsed)


def _bounded_version(value: str | None, *, label: str) -> str:
    if value is None or not _VERSION.fullmatch(value) or len(value) > 32:
        raise NmapXmlParseError(f"invalid_{label}")
    return value


def _bounded_integer(value: str | None, minimum: int, maximum: int, *, label: str) -> int:
    try:
        parsed = int(str(value), 10)
    except (TypeError, ValueError) as error:
        raise NmapXmlParseError(f"invalid_{label}") from error
    if str(parsed) != str(value) or parsed < minimum or parsed > maximum:
        raise NmapXmlParseError(f"invalid_{label}")
    return parsed


def _bounded_float(value: str, *, label: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise NmapXmlParseError(f"invalid_{label}") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise NmapXmlParseError(f"invalid_{label}")
    return parsed


def _safe_token(value: str | None, *, label: str, limit: int = 64) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if len(normalized) > limit or not _SNAKE_OR_HYPHEN.fullmatch(normalized):
        raise NmapXmlParseError(f"invalid_{label}")
    return normalized


def _safe_hostname(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().rstrip(".").lower()
    if not _HOSTNAME.fullmatch(normalized):
        raise NmapXmlParseError("invalid_hostname")
    return normalized


def _safe_public_text(value: str | None, *, label: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > limit
        or _PROTECTED_MARKER.search(normalized)
        or not _PUBLIC_TEXT.fullmatch(normalized)
    ):
        raise NmapXmlParseError(f"invalid_{label}")
    return normalized
