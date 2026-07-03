from pydantic import BaseModel


class SystemInterface(BaseModel):
    name: str  # OS adapter name, e.g. "Ethernet 3"
    ipv4: str  # "192.168.1.10"
    prefix_length: int  # 24
    cidr: str  # "192.168.1.10/24" (what the Source Interface dropdown stores)
    is_up: bool
