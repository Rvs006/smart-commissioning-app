"""Pure, immutable IP-register comparison contracts.

The comparator never mutates or rewrites register rows.  It indexes the full
frozen authority for one call, keeps duplicate values visible, and only emits a
wrong-IP review when positive reachability is paired with one internally
consistent, high-confidence identity.
"""

from __future__ import annotations

import ipaddress
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from smart_commissioning_core.discovery_observations import (
    PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    normalize_public_string_v1,
)
from smart_commissioning_core.engines.ip.models import ProviderIdentityEvidenceV1
from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.scan_contract import MAX_IPV4_HOSTS

IPRegisterMatchV1 = Literal[
    "not_configured",
    "expected_match",
    "wrong_ip_review",
    "ambiguous_review",
    "unregistered",
]
IPComparisonBasisV1 = Literal[
    "expected_ip",
    "asset_id",
    "hostname",
    "mac_address",
]
IPComparisonReasonV1 = Literal[
    "register_not_configured",
    "expected_ip_match",
    "target_unconfirmed",
    "identity_missing",
    "identity_weak",
    "identity_unknown",
    "identity_duplicate",
    "identity_conflict",
    "identity_expected_ip_ambiguous",
    "unique_identity_wrong_ip",
]
IPComparisonReachabilityV1 = Literal[
    "pending",
    "reachable",
    "unconfirmed",
    "not_applicable",
]

_MAC_ADDRESS = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
_IDENTITY_FIELDS: tuple[Literal["asset_id", "hostname", "mac_address"], ...] = (
    "asset_id",
    "hostname",
    "mac_address",
)
@dataclass(frozen=True)
class _FrozenIndex[IndexValue](Mapping[str, IndexValue]):
    """Copy-safe immutable mapping with logarithmic lookup."""

    _keys: tuple[str, ...]
    _values: tuple[IndexValue, ...]

    @classmethod
    def from_mapping(cls, values: Mapping[str, IndexValue]) -> _FrozenIndex[IndexValue]:
        ordered = tuple(sorted(values.items()))
        return cls(
            _keys=tuple(key for key, _value in ordered),
            _values=tuple(value for _key, value in ordered),
        )

    def __getitem__(self, key: str) -> IndexValue:
        index = bisect_left(self._keys, key)
        if index == len(self._keys) or self._keys[index] != key:
            raise KeyError(key)
        return self._values[index]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenIndex[IndexValue]:
        memo[id(self)] = self
        return self


def _ipv4(value: str, *, field_name: str) -> str:
    try:
        parsed = ipaddress.ip_address(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a numeric IPv4 address") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError(f"{field_name} must be a numeric IPv4 address")
    return str(parsed)


def _required_text(
    value: object,
    *,
    field_name: str,
    max_length: int = PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
) -> str:
    normalized = normalize_public_string_v1(
        value,
        path=f"ip_register.{field_name}",
        max_chars=max_length,
    ).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return normalized


def _optional_text(
    value: object,
    *,
    field_name: str,
    max_length: int = PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
) -> str | None:
    if value is None:
        return None
    normalized = normalize_public_string_v1(
        value,
        path=f"ip_register.{field_name}",
        max_chars=max_length,
    ).strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return normalized


def _canonical_hostname(value: str) -> str:
    return value.strip().rstrip(".").casefold()


def _canonical_mac(value: str) -> str:
    normalized = value.strip().replace("-", ":").upper()
    if _MAC_ADDRESS.fullmatch(normalized) is None:
        raise ValueError("mac_address must contain six hexadecimal octets")
    return normalized


def _canonical_identity(field_name: str, value: str) -> str:
    if field_name == "hostname":
        return _canonical_hostname(value)
    if field_name == "mac_address":
        return _canonical_mac(value)
    return value.strip().casefold()


def _stable_strings(values: Sequence[str] | set[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))


def _stable_ips(values: Sequence[str] | set[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: int(ipaddress.IPv4Address(item))))


class IPRegisterAuthorityRowV1(BaseModel):
    """One normalized accepted row from the frozen IP register.

    ``row_key`` is intentionally separate from ``device_key``. Several accepted
    rows may name the same device, and duplicate identity values remain present
    instead of being collapsed into a lossy dictionary.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    row_key: str = Field(min_length=1, max_length=255)
    device_key: str = Field(
        min_length=1,
        max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    )
    expected_ip: str
    asset_id: str | None = Field(
        default=None,
        max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    )
    asset_name: str | None = Field(
        default=None,
        max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    )
    hostname: str | None = Field(default=None, max_length=253)
    mac_address: str | None = None

    @field_validator("row_key", "device_key")
    @classmethod
    def _normalize_required_text(cls, value: str, info: Any) -> str:
        return _required_text(
            value,
            field_name=info.field_name,
            max_length=(255 if info.field_name == "row_key" else 4_096),
        )

    @field_validator("asset_id", "asset_name")
    @classmethod
    def _normalize_optional_identity(cls, value: str | None, info: Any) -> str | None:
        return _optional_text(value, field_name=info.field_name)

    @field_validator("expected_ip")
    @classmethod
    def _normalize_expected_ip(cls, value: str) -> str:
        return _ipv4(value, field_name="expected_ip")

    @field_validator("hostname")
    @classmethod
    def _normalize_hostname(cls, value: str | None) -> str | None:
        normalized = _optional_text(value, field_name="hostname", max_length=253)
        return None if normalized is None else _canonical_hostname(normalized)

    @field_validator("mac_address")
    @classmethod
    def _normalize_mac(cls, value: str | None) -> str | None:
        normalized = _optional_text(value, field_name="mac_address", max_length=17)
        return None if normalized is None else _canonical_mac(normalized)


class IPRegisterAuthorityV1(BaseModel):
    """The complete immutable authority selected for one run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    import_id: str = Field(min_length=1, max_length=255)
    accepted_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_count: int = Field(ge=0)
    rows: tuple[IPRegisterAuthorityRowV1, ...] = ()
    _rows_by_expected_ip: _FrozenIndex[tuple[IPRegisterAuthorityRowV1, ...]] = (
        PrivateAttr()
    )
    _rows_by_device_key: _FrozenIndex[tuple[IPRegisterAuthorityRowV1, ...]] = (
        PrivateAttr()
    )
    _rows_by_identity: _FrozenIndex[
        _FrozenIndex[tuple[IPRegisterAuthorityRowV1, ...]]
    ] = PrivateAttr()

    @field_validator("import_id")
    @classmethod
    def _normalize_import_id(cls, value: str) -> str:
        return _required_text(value, field_name="import_id", max_length=255)

    @model_validator(mode="after")
    def _validate_complete_authority(self) -> IPRegisterAuthorityV1:
        if self.accepted_count != len(self.rows):
            raise ValueError("accepted_count must equal the number of authority rows")
        row_keys = tuple(row.row_key for row in self.rows)
        if len(set(row_keys)) != len(row_keys):
            raise ValueError("authority row_key values must be unique")
        return self

    def model_post_init(self, __context: Any) -> None:
        """Index the frozen authority once without changing its wire contract."""

        by_expected_ip: dict[str, list[IPRegisterAuthorityRowV1]] = {}
        by_device_key: dict[str, list[IPRegisterAuthorityRowV1]] = {}
        by_identity: dict[
            Literal["asset_id", "hostname", "mac_address"],
            dict[str, list[IPRegisterAuthorityRowV1]],
        ] = {field_name: {} for field_name in _IDENTITY_FIELDS}
        for row in self.rows:
            by_expected_ip.setdefault(row.expected_ip, []).append(row)
            by_device_key.setdefault(row.device_key, []).append(row)
            for field_name in _IDENTITY_FIELDS:
                candidates = (
                    (row.asset_id, row.asset_name)
                    if field_name == "asset_id"
                    else (getattr(row, field_name),)
                )
                for candidate in candidates:
                    if candidate is None:
                        continue
                    canonical = _canonical_identity(field_name, candidate)
                    matches = by_identity[field_name].setdefault(canonical, [])
                    if not matches or matches[-1] is not row:
                        matches.append(row)

        self._rows_by_expected_ip = _FrozenIndex.from_mapping(
            {key: tuple(rows) for key, rows in by_expected_ip.items()}
        )
        self._rows_by_device_key = _FrozenIndex.from_mapping(
            {key: tuple(rows) for key, rows in by_device_key.items()}
        )
        self._rows_by_identity = _FrozenIndex.from_mapping(
            {
                field_name: _FrozenIndex.from_mapping(
                    {key: tuple(rows) for key, rows in values.items()}
                )
                for field_name, values in by_identity.items()
            }
        )

    @property
    def expected_device_keys(self) -> tuple[str, ...]:
        """Stable deduplicated device denominator without dropping source rows."""

        return _stable_strings({row.device_key for row in self.rows})


class IPHostComparisonInputV1(BaseModel):
    """Reachability and approved identity evidence for one observed target."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    observed_ip: str
    reachability_state: IPComparisonReachabilityV1
    identity_evidence: ProviderIdentityEvidenceV1 | None = None

    @field_validator("observed_ip")
    @classmethod
    def _normalize_observed_ip(cls, value: str) -> str:
        return _ipv4(value, field_name="observed_ip")


class IPRegisterComparisonV1(BaseModel):
    """Viewer-safe register verdict with the addresses and evidence basis intact."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    register_match: IPRegisterMatchV1
    observed_ip: str
    expected_ip: str | None = None
    expected_ips: tuple[str, ...] = Field(default=(), max_length=MAX_IPV4_HOSTS)
    matched_device_key: str | None = Field(
        default=None,
        max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    )
    candidate_device_keys: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_IPV4_HOSTS,
    )
    match_basis: tuple[IPComparisonBasisV1, ...] = Field(default=(), max_length=3)
    reason_code: IPComparisonReasonV1
    authority_import_id: str | None = Field(default=None, max_length=255)
    authority_rows_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("observed_ip", "expected_ip")
    @classmethod
    def _normalize_result_ip(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _ipv4(value, field_name=info.field_name)

    @field_validator("matched_device_key")
    @classmethod
    def _normalize_matched_device_key(cls, value: str | None) -> str | None:
        return _optional_text(value, field_name="matched_device_key")

    @field_validator("candidate_device_keys")
    @classmethod
    def _normalize_candidate_device_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _required_text(item, field_name=f"candidate_device_keys[{index}]")
            for index, item in enumerate(value)
        )

    @field_validator("authority_import_id")
    @classmethod
    def _normalize_authority_import_id(cls, value: str | None) -> str | None:
        return _optional_text(
            value,
            field_name="authority_import_id",
            max_length=255,
        )

    @model_validator(mode="after")
    def _validate_verdict_evidence(self) -> IPRegisterComparisonV1:
        has_authority = self.authority_import_id is not None
        if has_authority != (self.authority_rows_sha256 is not None):
            raise ValueError("authority import and digest must be supplied together")
        if (self.register_match == "not_configured") == has_authority:
            raise ValueError("register_match must agree with authority provenance")
        if self.expected_ips != _stable_ips(self.expected_ips):
            raise ValueError("expected_ips must be unique and numerically ordered")
        if self.candidate_device_keys != _stable_strings(self.candidate_device_keys):
            raise ValueError("candidate_device_keys must be unique and ordered")
        if self.expected_ip is not None and self.expected_ip not in self.expected_ips:
            raise ValueError("expected_ip must be included in expected_ips")
        if self.matched_device_key is not None and (
            self.matched_device_key not in self.candidate_device_keys
        ):
            raise ValueError("matched_device_key must be one of the candidates")
        if self.register_match == "not_configured" and any(
            (
                self.expected_ip is not None,
                bool(self.expected_ips),
                self.matched_device_key is not None,
                bool(self.candidate_device_keys),
                bool(self.match_basis),
            )
        ):
            raise ValueError("not_configured cannot carry register evidence")
        if self.register_match == "expected_match":
            if self.expected_ip != self.observed_ip:
                raise ValueError("expected_match requires the observed expected IP")
            if self.match_basis != ("expected_ip",):
                raise ValueError("expected_match requires expected_ip basis")
        if self.register_match == "wrong_ip_review":
            if (
                self.expected_ip is None
                or self.expected_ip == self.observed_ip
                or self.matched_device_key is None
                or len(self.candidate_device_keys) != 1
                or not self.match_basis
                or "expected_ip" in self.match_basis
            ):
                raise ValueError("wrong_ip_review requires one distinct expected identity")
        return self


def build_ip_register_authority(
    rows: Sequence[Mapping[str, object]],
    *,
    import_id: str,
    accepted_rows_sha256: str,
) -> IPRegisterAuthorityV1:
    """Normalize accepted import rows without collapsing duplicate identities."""

    raw_rows = list(rows)
    if canonical_sha256(raw_rows) != accepted_rows_sha256:
        raise ValueError("accepted-row digest verification failed")
    normalized: list[IPRegisterAuthorityRowV1] = []
    for index, row in enumerate(raw_rows, start=1):
        asset_id = _optional_text(row.get("Asset ID"), field_name="Asset ID")
        asset_name = _optional_text(row.get("Asset name"), field_name="Asset name")
        device_key = asset_id or asset_name
        if device_key is None:
            raise ValueError(f"authority row {index} has no device identity")
        expected_ip = _required_text(
            row.get("Expected IP address"),
            field_name="Expected IP address",
        )
        mac_address = row.get("Expected MAC address")
        if mac_address is None:
            mac_address = row.get("MAC address")
        normalized.append(
            IPRegisterAuthorityRowV1(
                row_key=f"row:{index}",
                device_key=device_key,
                expected_ip=expected_ip,
                asset_id=asset_id,
                asset_name=asset_name,
                hostname=_optional_text(
                    row.get("Expected hostname"),
                    field_name="Expected hostname",
                    max_length=253,
                ),
                mac_address=_optional_text(
                    mac_address,
                    field_name="Expected MAC address",
                    max_length=17,
                ),
            )
        )
    return IPRegisterAuthorityV1(
        import_id=import_id,
        accepted_rows_sha256=accepted_rows_sha256,
        accepted_count=len(normalized),
        rows=tuple(normalized),
    )


def _configured_comparison(
    *,
    authority: IPRegisterAuthorityV1,
    register_match: IPRegisterMatchV1,
    observed_ip: str,
    reason_code: IPComparisonReasonV1,
    expected_ip: str | None = None,
    expected_ips: tuple[str, ...] = (),
    matched_device_key: str | None = None,
    candidate_device_keys: tuple[str, ...] = (),
    match_basis: tuple[IPComparisonBasisV1, ...] = (),
) -> IPRegisterComparisonV1:
    return IPRegisterComparisonV1(
        register_match=register_match,
        observed_ip=observed_ip,
        expected_ip=expected_ip,
        expected_ips=expected_ips,
        matched_device_key=matched_device_key,
        candidate_device_keys=candidate_device_keys,
        match_basis=match_basis,
        reason_code=reason_code,
        authority_import_id=authority.import_id,
        authority_rows_sha256=authority.accepted_rows_sha256,
    )


def _rows_for_identity(
    authority: IPRegisterAuthorityV1,
    field_name: Literal["asset_id", "hostname", "mac_address"],
    value: str,
) -> tuple[IPRegisterAuthorityRowV1, ...]:
    canonical = _canonical_identity(field_name, value)
    return authority._rows_by_identity[field_name].get(canonical, ())


def _candidate_projection(
    rows: Sequence[IPRegisterAuthorityRowV1],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        _stable_strings({row.device_key for row in rows}),
        _stable_ips({row.expected_ip for row in rows}),
    )


def compare_ip_register(
    observed: IPHostComparisonInputV1,
    *,
    authority: IPRegisterAuthorityV1 | None,
) -> IPRegisterComparisonV1:
    """Compare one host against one frozen authority without side effects."""

    if authority is None:
        return IPRegisterComparisonV1(
            register_match="not_configured",
            observed_ip=observed.observed_ip,
            reason_code="register_not_configured",
        )

    expected_rows = authority._rows_by_expected_ip.get(observed.observed_ip, ())
    if expected_rows:
        candidates, expected_ips = _candidate_projection(expected_rows)
        matched_device_key = candidates[0] if len(candidates) == 1 else None
        return _configured_comparison(
            authority=authority,
            register_match=(
                "expected_match" if len(candidates) == 1 else "ambiguous_review"
            ),
            observed_ip=observed.observed_ip,
            expected_ip=observed.observed_ip,
            expected_ips=expected_ips,
            matched_device_key=matched_device_key,
            candidate_device_keys=candidates,
            match_basis=("expected_ip",),
            reason_code="expected_ip_match",
        )

    if observed.reachability_state != "reachable":
        return _configured_comparison(
            authority=authority,
            register_match="unregistered",
            observed_ip=observed.observed_ip,
            reason_code="target_unconfirmed",
        )

    identity = observed.identity_evidence
    if identity is None:
        return _configured_comparison(
            authority=authority,
            register_match="unregistered",
            observed_ip=observed.observed_ip,
            reason_code="identity_missing",
        )

    populated_fields = tuple(
        field_name for field_name in _IDENTITY_FIELDS if getattr(identity, field_name) is not None
    )
    rows_by_field = {
        field_name: _rows_for_identity(
            authority,
            field_name,
            str(getattr(identity, field_name)),
        )
        for field_name in populated_fields
    }
    all_rows = tuple(row for rows in rows_by_field.values() for row in rows)
    candidate_keys, expected_ips = _candidate_projection(all_rows)
    common_expected_ip = expected_ips[0] if len(expected_ips) == 1 else None

    if identity.confidence != "high":
        return _configured_comparison(
            authority=authority,
            register_match="ambiguous_review",
            observed_ip=observed.observed_ip,
            expected_ip=common_expected_ip,
            expected_ips=expected_ips,
            candidate_device_keys=candidate_keys,
            match_basis=populated_fields,
            reason_code="identity_weak",
        )
    if not all_rows:
        return _configured_comparison(
            authority=authority,
            register_match="unregistered",
            observed_ip=observed.observed_ip,
            reason_code="identity_unknown",
        )
    if any(not rows for rows in rows_by_field.values()):
        return _configured_comparison(
            authority=authority,
            register_match="ambiguous_review",
            observed_ip=observed.observed_ip,
            expected_ip=common_expected_ip,
            expected_ips=expected_ips,
            candidate_device_keys=candidate_keys,
            match_basis=populated_fields,
            reason_code="identity_conflict",
        )

    device_sets = tuple(
        {row.device_key for row in rows} for rows in rows_by_field.values()
    )
    if any(len(device_keys) != 1 for device_keys in device_sets):
        return _configured_comparison(
            authority=authority,
            register_match="ambiguous_review",
            observed_ip=observed.observed_ip,
            expected_ip=common_expected_ip,
            expected_ips=expected_ips,
            candidate_device_keys=candidate_keys,
            match_basis=populated_fields,
            reason_code="identity_duplicate",
        )
    resolved_keys = {next(iter(device_keys)) for device_keys in device_sets}
    if len(resolved_keys) != 1:
        return _configured_comparison(
            authority=authority,
            register_match="ambiguous_review",
            observed_ip=observed.observed_ip,
            expected_ip=common_expected_ip,
            expected_ips=expected_ips,
            candidate_device_keys=candidate_keys,
            match_basis=populated_fields,
            reason_code="identity_conflict",
        )

    matched_device_key = next(iter(resolved_keys))
    matched_rows = authority._rows_by_device_key.get(matched_device_key, ())
    _, matched_expected_ips = _candidate_projection(matched_rows)
    if len(matched_expected_ips) != 1:
        return _configured_comparison(
            authority=authority,
            register_match="ambiguous_review",
            expected_ip=None,
            expected_ips=matched_expected_ips,
            candidate_device_keys=(matched_device_key,),
            observed_ip=observed.observed_ip,
            match_basis=populated_fields,
            reason_code="identity_expected_ip_ambiguous",
        )

    return _configured_comparison(
        authority=authority,
        register_match="wrong_ip_review",
        observed_ip=observed.observed_ip,
        expected_ip=matched_expected_ips[0],
        expected_ips=matched_expected_ips,
        matched_device_key=matched_device_key,
        candidate_device_keys=(matched_device_key,),
        match_basis=populated_fields,
        reason_code="unique_identity_wrong_ip",
    )


__all__ = [
    "IPComparisonBasisV1",
    "IPComparisonReachabilityV1",
    "IPComparisonReasonV1",
    "IPHostComparisonInputV1",
    "IPRegisterAuthorityRowV1",
    "IPRegisterAuthorityV1",
    "IPRegisterComparisonV1",
    "IPRegisterMatchV1",
    "build_ip_register_authority",
    "compare_ip_register",
]
