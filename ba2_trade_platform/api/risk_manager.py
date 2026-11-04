"""GET /risk-manager/runs and /risk-manager/runs/{ref} (ref = ``rm-<id>`` or ``smart-<id>``)."""
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import risk_manager as svc
from .common import Paging, paging

router = APIRouter(prefix="/risk-manager", tags=["risk-manager"])


@router.get("/runs")
def list_runs(expert: Optional[int] = None, kind: Optional[str] = None, p: Paging = Depends(paging)):
    return svc.list_runs(expert=expert, kind=kind, limit=p.limit, offset=p.offset)


@router.get("/runs/{ref}")
def run_detail(ref: str):
    return svc.run_detail(ref)
