"""GET /accounts and friends: definitions with masked settings, live snapshot, broker positions / orders."""
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import accounts as svc
from .common import Paging, paging

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
def list_accounts(p: Paging = Depends(paging)):
    return svc.list_accounts(limit=p.limit, offset=p.offset)


@router.get("/{account_id}")
def account_detail(account_id: int):
    return svc.account_detail(account_id)


@router.get("/{account_id}/positions")
def account_positions(account_id: int):
    return svc.broker_positions(account_id)


@router.get("/{account_id}/orders")
def account_orders(account_id: int, status: Optional[str] = None):
    return svc.broker_orders(account_id, status)
