"""GET /stats/overview: the Overview page's figures."""
from typing import Optional

from fastapi import APIRouter

from ..services import overview as svc

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview")
def stats_overview(account: Optional[int] = None):
    return svc.stats_overview(account)
