from typing import Literal

from pydantic import BaseModel, Field

# Best-effort adapter classification for the Source Interface picker. Virtual
# adapters ARE returned (ranked last, since 2026-07-14 they can be the host's
# only routable NIC on Hyper-V vSwitch / NIC-team machines) — clients label
# them as a pick-with-care option rather than hiding them. Classification
# failures degrade to "unknown", never an error.
AdapterType = Literal["ethernet", "wifi", "usb_ethernet", "virtual", "unknown"]


class SystemInterface(BaseModel):
    name: str  # OS adapter name, e.g. "Ethernet 3"
    ipv4: str  # "192.168.1.10"
    prefix_length: int  # 24
    subnet_mask: str  # dotted quad derived from prefix_length, e.g. "255.255.255.0"
    cidr: str  # "192.168.1.10/24" (what the Source Interface dropdown stores)
    is_up: bool
    adapter_type: AdapterType = "unknown"
    # Gateway/DNS exposure is a deliberate product-owner reversal (2026-07-03
    # meeting) of the section-5.3 omission in the NIC proposal: engineers need
    # them to confirm the tool reads the NIC correctly. MAC / driver /
    # InterfaceDescription strings stay deliberately omitted.
    gateway: str | None = None  # adapter's IPv4 default gateway; None when absent/degraded
    dns_servers: list[str] = Field(default_factory=list)  # IPv4 DNS servers in OS order
