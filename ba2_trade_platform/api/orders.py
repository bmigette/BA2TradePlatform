"""GET /orders: platform TradingOrder rows."""
from typing import Optional

from fastapi import APIRouter, Depends

from ..services import trades as svc
from .common import Paging, paging

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
def list_orders(account: Optional[int] = None, status: Optional[str] = None,
                transaction: Optional[int] = None, p: Paging = Depends(paging)):
    return svc.list_orders(account=account, status=status, transaction=transaction,
                           limit=p.limit, offset=p.offset)
