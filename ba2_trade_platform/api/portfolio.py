"""GET /accounts/{id}/portfolio and /accounts/{id}/portfolio/runs."""
from fastapi import APIRouter, Depends

from ..services import portfolio as svc
from .common import Paging, paging

router = APIRouter(prefix="/accounts", tags=["portfolio"])


@router.get("/{account_id}/portfolio")
def portfolio(account_id: int):
    return svc.portfolio_view(account_id)
