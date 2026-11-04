"""GET /transactions and /transactions/{id}."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import trades as svc
from .common import Paging, paging

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("")
def list_transactions(account: Optional[int] = None, expert: Optional[int] = None,
                      status: Optional[str] = None, symbol: Optional[str] = None,
                      from_: Optional[datetime] = Query(None, alias="from"), to: Optional[datetime] = None,
                      p: Paging = Depends(paging)):
    return svc.list_transactions(account=account, expert=expert, status=status, symbol=symbol,
                                 from_=from_, to=to, limit=p.limit, offset=p.offset)


@router.get("/{transaction_id}")
def transaction_detail(transaction_id: int):
    return svc.transaction_detail(transaction_id)
