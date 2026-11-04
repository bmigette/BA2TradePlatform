"""Risk-manager run history for the management API: classic/options runs and smart-RM jobs in one feed."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import RiskManagerRun, SmartRiskManagerJob
from ._common import require_row, row_dict
from .errors import Invalid

_KINDS = {"rm": RiskManagerRun, "smart": SmartRiskManagerJob}
_BIG = {"rm": ("decisions", "context"), "smart": ("graph_state",)}


def _summary(kind: str, row) -> Dict[str, Any]:
    d = row_dict(row)
    for key in _BIG[kind]:
        d.pop(key)
    d["kind"] = kind
    d["ref"] = f"{kind}-{row.id}"
    return d


def list_runs(*, expert: Optional[int], kind: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    if kind is not None and kind not in _KINDS:
        raise Invalid(f"kind={kind!r} is not one of {sorted(_KINDS)}", {"field": "kind"})
    items = []
    with get_db() as session:
        for k, model in _KINDS.items():
            if kind is not None and kind != k:
                continue
            stmt = select(model)
            if expert is not None:
                stmt = stmt.where(model.expert_instance_id == expert)
            items.extend((row.run_date, _summary(k, row)) for row in session.exec(stmt).all())
    items.sort(key=lambda pair: pair[0], reverse=True)
    window = [summary for _, summary in items[offset:offset + limit]]
    return {"items": window, "limit": limit, "offset": offset, "total": len(items)}


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
