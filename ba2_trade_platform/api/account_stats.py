"""Per-account figures of the Overview: growth (total, by label, by position), closed P&L, dividends."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Query

from ..services import account_growth as growth_svc

router = APIRouter(prefix="/accounts", tags=["account stats"])


@router.get("/{account_id}/growth")
def account_growth(account_id: int, from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None):
    return growth_svc.account_growth(account_id, from_, to)


@router.get("/{account_id}/growth/labels")
def growth_by_label(account_id: int, labels: Optional[str] = None,
                    from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None):
    wanted = [x.strip() for x in labels.split(",") if x.strip()] if labels else None
    return growth_svc.growth_by_label(account_id, wanted, from_, to)


@router.get("/{account_id}/growth/labels/{label}/positions")
def growth_by_position(account_id: int, label: str, from_: Optional[date] = Query(None, alias="from"),
                       to: Optional[date] = None):
    return growth_svc.growth_by_position(account_id, label, from_, to)
