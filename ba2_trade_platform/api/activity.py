"""GET /activity."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import activity as svc
from .common import Paging, paging

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
def list_activity(type: Optional[str] = None, severity: Optional[str] = None, expert: Optional[int] = None,
                  account: Optional[int] = None, from_: Optional[datetime] = Query(None, alias="from"),
                  p: Paging = Depends(paging)):
    return svc.list_activity(type_=type, severity=severity, expert=expert, account=account, from_=from_,
                             limit=p.limit, offset=p.offset)
