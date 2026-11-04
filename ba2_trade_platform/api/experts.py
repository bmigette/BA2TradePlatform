"""GET /experts and /experts/{id}; PATCH the instance and its settings, POST a run (token required, audited)."""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from ..services import experts as svc
from ..services._locks import expert_write_lock
from ..services.errors import Invalid
from ..ui import api_routes
from .audit import AuditedRoute
from .common import Paging, paging

router = APIRouter(prefix="/experts", tags=["experts"], route_class=AuditedRoute)


class ExpertPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[StrictBool] = None
    virtual_equity_pct: Optional[float] = None


class RunBody(BaseModel):
    """``subtype`` is the AnalysisUseCase value stored on the schedule: "enter_market" / "open_positions"."""
    model_config = ConfigDict(extra="forbid")
    subtype: str


@router.get("")
def list_experts(account: Optional[int] = None, enabled: Optional[bool] = None, p: Paging = Depends(paging)):
    return svc.list_experts(account=account, enabled=enabled, limit=p.limit, offset=p.offset)


@router.get("/{expert_id}")
def expert_detail(expert_id: int):
    return svc.expert_detail(expert_id)


@router.patch("/{expert_id}")
def patch_expert(expert_id: int, body: ExpertPatch, dry_run: bool = False) -> Any:
    for name in ("enabled", "virtual_equity_pct"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise Invalid(f"{name} cannot be null: omit it to leave it unchanged", {"field": name})
    return svc.update_expert(expert_id, enabled=body.enabled, virtual_equity_pct=body.virtual_equity_pct,
                             dry_run=dry_run)


@router.patch("/{expert_id}/settings")
def patch_expert_settings(expert_id: int, request: Request, values: Dict[str, Any] = Body(...),
                          dry_run: bool = False) -> Any:
    # The audit trail redacts these keys' values whatever they are called (set BEFORE the service can fail).
    request.state.audit_secret_keys = svc.secret_setting_keys(expert_id)
    return svc.update_expert_settings(expert_id, values, dry_run=dry_run)


@router.post("/{expert_id}/run")
def run_expert(expert_id: int, body: RunBody, dry_run: bool = False) -> Any:
    """The Scheduled Jobs page's "Run Now" (and POST /run-schedule): fire today's registered schedule for this
    expert immediately. Holds the expert's processing locks while it submits, like every write."""
    if dry_run:
        return api_routes.run_schedule(expert_id, body.subtype, dry_run=True)
    with expert_write_lock(expert_id):
        return api_routes.run_schedule(expert_id, body.subtype)
