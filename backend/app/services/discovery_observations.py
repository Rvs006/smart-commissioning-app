from datetime import datetime, timezone
import re

from app.schemas.jobs import DiscoveryAssetObservation, ObservedPort


COMMON_PORTS = [
    ObservedPort(port=47808, protocol="udp", service="BACnet"),
    ObservedPort(port=80, protocol="tcp", service="HTTP"),
    ObservedPort(port=443, protocol="tcp", service="HTTPS"),
]


def parse_port_specification(value: str) -> list[ObservedPort]:
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    if not tokens:
        return COMMON_PORTS.copy()

    ports: list[ObservedPort] = []
    for token in tokens:
        protocol = "tcp"
        raw_port = token
        if "/" in token:
            raw_port, raw_protocol = token.split("/", 1)
            protocol = raw_protocol.strip().casefold()
        port = int(raw_port.strip())
        if protocol not in {"tcp", "udp"}:
            raise ValueError(f"Unsupported port protocol '{protocol}'.")
        ports.append(ObservedPort(port=port, protocol=protocol, service=_service_name(port, protocol)))
    return ports


def build_observation(
    observed: dict[str, str],
    expected: dict[str, str] | None = None,
    *,
    port_specification: str = "",
    status_detail: str = "Observed during scan.",
) -> DiscoveryAssetObservation:
    expected = expected or {}
    observed_mac = normalize_mac(observed.get("mac_address", ""))
    expected_mac = normalize_mac(expected.get("mac_address", ""))
    match_basis = "none"
    asset_id = expected.get("asset_id") or observed.get("asset_id") or None

    if observed_mac and expected_mac and observed_mac == expected_mac:
        match_basis = "mac"
    elif observed.get("ip_address") and observed.get("ip_address") == expected.get("ip_address"):
        match_basis = "ip"
    elif observed.get("hostname") and observed.get("hostname") == expected.get("hostname"):
        match_basis = "hostname"

    return DiscoveryAssetObservation(
        asset_id=asset_id,
        ip_address=observed.get("ip_address") or None,
        mac_address=observed_mac or None,
        hostname=observed.get("hostname") or None,
        observed_ports=parse_port_specification(port_specification),
        match_basis=match_basis,
        last_seen_at=datetime.now(timezone.utc),
        status_detail=status_detail,
    )


def normalize_mac(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", value or "")
    if len(cleaned) != 12:
        return ""
    return ":".join(cleaned[index : index + 2] for index in range(0, 12, 2)).upper()


def _service_name(port: int, protocol: str) -> str | None:
    known = {
        (47808, "udp"): "BACnet",
        (80, "tcp"): "HTTP",
        (443, "tcp"): "HTTPS",
        (1883, "tcp"): "MQTT",
        (8883, "tcp"): "MQTT/TLS",
        (502, "tcp"): "Modbus TCP",
    }
    return known.get((port, protocol))
