"""GET /orders: platform TradingOrder rows; POST /orders/{id}/cancel (token required, audited)."""
from typing import Any, Optional

from fastapi import APIRouter, Depends

from ..services import trades as svc
from .audit import AuditedRoute
from .common import Paging, paging

router = APIRouter(prefix="/orders", tags=["orders"], route_class=AuditedRoute)


@router.get("")
def list_orders(account: Optional[int] = None, status: Optional[str] = None,
                transaction: Optional[int] = None, p: Paging = Depends(paging)):
    return svc.list_orders(account=account, status=status, transaction=transaction,
                           limit=p.limit, offset=p.offset)


@router.post("/{order_id}/cancel")
def cancel_order(order_id: int, dry_run: bool = False) -> Any:
    return svc.cancel_order(order_id, dry_run=dry_run)
