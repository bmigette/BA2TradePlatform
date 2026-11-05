"""Risk-manager run history for the management API: classic/options runs and smart-RM jobs in one feed."""
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlmodel import select

from ..core.db import get_db
from ..core.models import RiskManagerRun, SmartRiskManagerJob
from ._common import jsonable, require_row, row_dict
from .errors import Invalid

_KINDS = {"rm": RiskManagerRun, "smart": SmartRiskManagerJob}
_BIG = {"rm": ("decisions", "context"), "smart": ("graph_state",)}


def _list_columns(kind: str, model):
    """Every column except the big JSON ones: the list query must never read them off disk."""
    return [c for c in model.__table__.columns if c.name not in _BIG[kind]]


def _summary(kind: str, mapping) -> Dict[str, Any]:
    d = jsonable(dict(mapping))
    d["kind"] = kind
    d["ref"] = f"{kind}-{d['id']}"
    return d


def list_runs(*, expert: Optional[int], kind: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    """Newest first across both tables. Each table is read with an ordered, LIMITed query over the list columns
    only (the first ``offset + limit`` rows of each are enough to build the merged window), so the cost is
    bounded by the page, not by the history."""
    if kind is not None and kind not in _KINDS:
        raise Invalid(f"kind={kind!r} is not one of {sorted(_KINDS)}", {"field": "kind"})
    total = 0
    candidates = []
    with get_db() as session:
        for k, model in _KINDS.items():
            if kind is not None and kind != k:
                continue
            where = [] if expert is None else [model.expert_instance_id == expert]
            total += session.exec(select(func.count()).select_from(model).where(*where)).one()
            stmt = (select(*_list_columns(k, model)).where(*where)
                    .order_by(model.run_date.desc(), model.id.desc()).limit(offset + limit))
            for row in session.exec(stmt).all():
                mapping = row._mapping
                candidates.append((mapping["run_date"], mapping["id"], k, mapping))
        # newest first; ties (same run_date) by kind then id so the order is deterministic
        candidates.sort(key=lambda c: (c[0], c[2], c[1]), reverse=True)
        window = [_summary(k, mapping) for _, _, k, mapping in candidates[offset:offset + limit]]
    return {"items": window, "limit": limit, "offset": offset, "total": total}


def run_detail(ref: str) -> Dict[str, Any]:
    kind, sep, raw_id = ref.partition("-")
    if not sep or kind not in _KINDS or not raw_id.isdigit():
        raise Invalid(f"run ref {ref!r} must look like 'rm-<id>' or 'smart-<id>'", {"field": "ref"})
    with get_db() as session:
        row = require_row(session, _KINDS[kind], int(raw_id), "risk-manager run")
        d = row_dict(row)
    d["kind"] = kind
    d["ref"] = ref
    return d
