from fastapi import APIRouter, Depends
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.system import SystemInterface
from app.services import interface_service

router = APIRouter()

# RBAC: enumerating interfaces is viewer+ (read-only and it only feeds a
# Configuration field a viewer can already see; choosing/saving the value is
# separately engineer-gated via the configuration PUT).
require_viewer = require_role(Role.VIEWER)


@router.get("/interfaces", response_model=list[SystemInterface], dependencies=[Depends(require_viewer)])
def list_interfaces() -> list[SystemInterface]:
    return interface_service.list_usable_interfaces()
