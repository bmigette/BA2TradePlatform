"""Expert instance reads for the management API."""
from typing import Any, Dict, Optional

from sqlmodel import select

from ..core.db import get_db
from ..core.models import ExpertInstance, MarketAnalysis
from ..core.utils import get_expert_instance_from_id
from ._common import page, require_row, row_dict
from ._scrub import mask_setting
from ._readonly import readonly_balance_reads
from .errors import Invalid, NotFound, Unmeasurable

_SCHEDULE_PREFIX = "execution_schedule"
_RESERVE_KEY = "overalloc_min_res_pct"


def _last_analysis(session, expert_id: int) -> Optional[Dict[str, Any]]:
    row = session.exec(select(MarketAnalysis).where(MarketAnalysis.expert_instance_id == expert_id)
                       .order_by(MarketAnalysis.id.desc()).limit(1)).first()
    if row is None:
        return None
    return {"id": row.id, "symbol": row.symbol, "status": row.status.value,
            "subtype": row.subtype.value, "created_at": row.created_at.isoformat()}


def list_experts(*, account: Optional[int], enabled: Optional[bool], limit: int, offset: int) -> Dict[str, Any]:
    """Instance rows + last analysis. No settings and no broker figures here (those need a live expert and a
    broker round trip per row): use the detail endpoint."""
    stmt = select(ExpertInstance)
    if account is not None:
        stmt = stmt.where(ExpertInstance.account_id == account)
    if enabled is not None:
        stmt = stmt.where(ExpertInstance.enabled == enabled)
    with get_db() as session:
        def convert(e: ExpertInstance) -> Dict[str, Any]:
            out = row_dict(e)
            out["last_analysis"] = _last_analysis(session, e.id)
            return out
        return page(session, stmt.order_by(ExpertInstance.id), limit=limit, offset=offset, convert=convert)


def expert_detail(expert_id: int) -> Dict[str, Any]:
    with get_db() as session:
        out = row_dict(require_row(session, ExpertInstance, expert_id, "expert"))
        out["last_analysis"] = _last_analysis(session, expert_id)
    try:
        expert = get_expert_instance_from_id(expert_id)
    except ValueError as e:        # e.g. the stored expert type is not registered
        raise Invalid(f"Expert {expert_id} cannot be instantiated: {type(e).__name__}: {e}", {"id": expert_id}) from e
    if expert is None:
        raise NotFound(f"No expert instance with id {expert_id}", {"id": expert_id})
    definitions = type(expert).get_merged_settings_definitions()
    settings: Dict[str, Any] = {}
    for key, value in expert.settings.items():
        definition = definitions[key] if key in definitions else None
        settings[key] = mask_setting(key, value, definition)
    out["settings"] = settings
    out["schedule"] = {k: v for k, v in settings.items() if k.startswith(_SCHEDULE_PREFIX)}
    # Explicit access: the key is a built-in MarketExpertInterface setting, so a KeyError is a real bug.
    out["overalloc_min_res_pct"] = expert.settings[_RESERVE_KEY]
    # A GET must not touch the live buying-power failure memory nor sleep on retries (see _readonly).
    with readonly_balance_reads():
        capital = expert.describe_capital_mapping()
    if capital is None or "error" in capital:
        reason = "nothing to describe" if capital is None else capital["error"]
        raise Unmeasurable(f"Expert {expert_id}: capital cannot be measured: {reason}", {"id": expert_id})
    out["capital"] = capital
    return out
