from pydantic import BaseModel


class SystemInterface(BaseModel):
    name: str  # OS adapter name, e.g. "Ethernet 3"
    ipv4: str  # "192.168.1.10"
    prefix_length: int  # 24
    subnet_mask: str  # "255.255.255.0" (dotted form of prefix_length)
    cidr: str  # "192.168.1.10/24" (what the Source Interface dropdown stores)
    # Default IPv4 gateway for this NIC, or None when the host has no route for it
    # / it cannot be determined (non-Windows host, locked-down box, no default
    # route). Populated from a guarded Windows routing-table lookup — psutil does
    # not expose gateways. See interface_service and proposal section 5.3.
    gateway: str | None = None
    is_up: bool
