"""GET /experts and /experts/{id}."""
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import experts as svc
from .common import Paging, paging

router = APIRouter(prefix="/experts", tags=["experts"])


@router.get("")
def list_experts(account: Optional[int] = None, enabled: Optional[bool] = None, p: Paging = Depends(paging)):
    return svc.list_experts(account=account, enabled=enabled, limit=p.limit, offset=p.offset)


@router.get("/{expert_id}")
def expert_detail(expert_id: int):
    return svc.expert_detail(expert_id)
