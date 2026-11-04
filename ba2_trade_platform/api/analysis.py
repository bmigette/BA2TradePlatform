"""GET /analyses, /analyses/{id}, /recommendations."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..services import analysis as svc
from .common import Paging, paging

router = APIRouter(tags=["analysis"])


@router.get("/analyses")
def list_analyses(expert: Optional[int] = None, symbol: Optional[str] = None, status: Optional[str] = None,
                  subtype: Optional[str] = None, from_: Optional[datetime] = Query(None, alias="from"),
                  p: Paging = Depends(paging)):
    return svc.list_analyses(expert=expert, symbol=symbol, status=status, subtype=subtype, from_=from_,
                             limit=p.limit, offset=p.offset)


@router.get("/analyses/{analysis_id}")
def analysis_detail(analysis_id: int, include_text: bool = False):
    return svc.analysis_detail(analysis_id, include_text)


@router.get("/recommendations")
def list_recommendations(expert: Optional[int] = None, symbol: Optional[str] = None,
                         from_: Optional[datetime] = Query(None, alias="from"), p: Paging = Depends(paging)):
    return svc.list_recommendations(expert=expert, symbol=symbol, from_=from_, limit=p.limit, offset=p.offset)
