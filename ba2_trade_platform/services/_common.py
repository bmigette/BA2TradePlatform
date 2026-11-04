"""Serialisation, paging and lookup helpers shared by every service module."""
import dataclasses
import math
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional, Type

from sqlalchemy import func
from sqlmodel import select

from .errors import Invalid, NotFound, Unmeasurable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def naive_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """The DB columns hold naive UTC. A timezone-aware bound is converted to UTC and made naive; a naive bound
    is taken as UTC already. (Comparing an aware value with a naive column compares the wrong instants.)"""
    if moment is None or moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def jsonable(value: Any) -> Any:
    """Recursively convert ``value`` to JSON-serialisable plain data.

    Enums -> their value; datetime/date -> ISO text; SQLModel/pydantic -> ``model_dump``; dataclass,
    NamedTuple, dict, list, tuple, set recurse. A NaN/inf float RAISES ``Unmeasurable`` -- JSON cannot carry
    it and silently turning it into null/0 would hide an unmeasured figure."""
    if value is None or isinstance(value, (bool, int, str)):
        return value.value if isinstance(value, Enum) else value
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Unmeasurable(f"A non-finite figure ({value!r}) cannot be returned")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if hasattr(value, "_asdict"):
        return jsonable(value._asdict())
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return str(value)


def row_dict(row: Any) -> Dict[str, Any]:
    """One DB row (columns only, no relationships) as a JSON-safe dict."""
    return jsonable(row.model_dump())


def parse_enum(enum_cls: Type[Enum], text: str, field: str) -> Enum:
    """Accept an enum VALUE or NAME (case-insensitive name); anything else is ``Invalid`` (422)."""
    for member in enum_cls:
        if text == member.value or text.upper() == member.name:
            return member
    raise Invalid(f"{field}={text!r} is not one of {[m.value for m in enum_cls]}", {"field": field})


def require_row(session, model, row_id: int, label: str):
    row = session.get(model, row_id)
    if row is None:
        raise NotFound(f"No {label} with id {row_id}", {"id": row_id})
    return row


def page(session, stmt, *, limit: int, offset: int, convert: Callable[[Any], Dict[str, Any]]) -> Dict[str, Any]:
    """Run ``stmt`` (an ordered ``select``) as one page: ``{items, limit, offset, total}``."""
    total = session.exec(select(func.count()).select_from(stmt.order_by(None).subquery())).one()
    rows = session.exec(stmt.limit(limit).offset(offset)).all()
    return {"items": [convert(r) for r in rows], "limit": limit, "offset": offset, "total": total}
