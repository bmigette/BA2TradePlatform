"""Query-parameter dependencies shared by the routers."""
from dataclasses import dataclass

from fastapi import Query

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


@dataclass
class Paging:
    limit: int
    offset: int


def paging(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)) -> Paging:
    return Paging(limit=limit, offset=offset)
