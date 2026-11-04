"""GET /transactions and /transactions/{id}; the TP/SL, unlock and close writes (token required, audited)."""
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, StrictBool

from ..services import trades as svc
from ..services.errors import Invalid
from .audit import AuditedRoute
from .common import Paging, paging

router = APIRouter(prefix="/transactions", tags=["transactions"], route_class=AuditedRoute)


class TpSlBody(BaseModel):
    """The side(s) to change. An omitted side keeps its stored value; ``null`` is refused (removing a leg is not
    offered in v1). ``lock`` makes it a manual override (the Live Trades dialog's own behaviour)."""
    model_config = ConfigDict(extra="forbid")
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    lock: StrictBool = False


@router.get("")
def list_transactions(account: Optional[int] = None, expert: Optional[int] = None,
                      status: Optional[str] = None, symbol: Optional[str] = None,
                      from_: Optional[datetime] = Query(None, alias="from"), to: Optional[datetime] = None,
                      include_open_pnl: bool = True, p: Paging = Depends(paging)):
    return svc.list_transactions(include_open_pnl=include_open_pnl, account=account, expert=expert, status=status, symbol=symbol,
                                 from_=from_, to=to, limit=p.limit, offset=p.offset)


@router.get("/{transaction_id}")
def transaction_detail(transaction_id: int, include_open_pnl: bool = True):
    return svc.transaction_detail(transaction_id, include_open_pnl=include_open_pnl)


@router.post("/{transaction_id}/tp-sl")
def set_tp_sl(transaction_id: int, body: TpSlBody, dry_run: bool = False) -> Any:
    for name in ("take_profit", "stop_loss"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise Invalid(f"{name} cannot be null: omit it to leave that side unchanged", {"field": name})
    return svc.set_tp_sl(transaction_id, take_profit=body.take_profit, stop_loss=body.stop_loss,
                         lock=body.lock, dry_run=dry_run)


@router.post("/{transaction_id}/tp-sl/unlock")
def unlock_tp_sl(transaction_id: int, dry_run: bool = False) -> Any:
    return svc.unlock_tp_sl(transaction_id, dry_run=dry_run)


@router.post("/{transaction_id}/close")
def close_transaction(transaction_id: int, dry_run: bool = False) -> Any:
    return svc.close_transaction(transaction_id, dry_run=dry_run)
