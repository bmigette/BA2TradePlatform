"""Glue between the platform and the derived ATM-IV history (live-only).

1. ``ensure_for_analysis_task`` -- THE SEAM. ``WorkerQueue._execute_task`` calls it just before
   ``expert.run_analysis`` for the symbol's task. When (and only when) the expert instance's
   ruleset for that use case carries an iv_rank condition, it runs the BLOCKING incremental
   ``AtmIvHistoryProvider.ensure_filled`` on that task's own worker thread, so the series is
   fresh before the rule is evaluated and ``get_iv_rank`` (store-only) finds it. An expert with
   no iv_rank rule pays one cached ruleset check and makes ZERO API calls. Never raises: a
   failure is logged and the rank simply stays None (the gate stays closed).
2. ``ensure_dgs3mo`` -- the option risk-free rate (FRED DGS3MO) is the one input the derivation
   cannot default; ``refresh_for_live_decision`` refuses it by design, so JobManager's 09:00 job
   and its startup call this whenever an iv_rank gate exists.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from ba2_common.logger import logger

#: A startup refresh is skipped when the file was fetched this recently.
DGS3MO_STARTUP_MAX_AGE_HOURS = 20.0
#: How long the "does this expert instance gate on iv_rank for this use case" answer is reused.
GATE_CACHE_TTL_SECONDS = 300.0

_GATE_CACHE: Dict[Tuple[int, str], Tuple[float, Optional[int]]] = {}
_GATE_LOCK = threading.Lock()


# ---- FRED DGS3MO --------------------------------------------------------------------------
def _dgs3mo_path() -> str:
    from ba2_providers.macro import fred_series
    return fred_series.cache_path("DGS3MO")


def _fetched_at(path: str) -> Optional[datetime]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f).get("fetched_at")
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError):
        return None


_QUERY_SECRET = re.compile(r"(?i)(api_key|apikey|key|token|secret)=[^&\s'\")]+")


def scrub_secrets(text: str, *known: Optional[str]) -> str:
    """``text`` with any ``api_key=...`` query value and any ``known`` secret removed."""
    out = _QUERY_SECRET.sub(lambda m: m.group(1) + "=***", text)
    for k in known:
        if k:
            out = out.replace(k, "***")
    return out


def ensure_dgs3mo(*, only_if_stale: bool, path: Optional[str] = None,
                  key_resolver: Optional[Callable[[], Optional[str]]] = None,
                  refresher: Optional[Callable[[str, str], int]] = None,
                  now: Optional[datetime] = None) -> str:
    """Refresh the DGS3MO cache file. Returns ``"current" | "refreshed" | "failed" | "no_key"``.

    ``only_if_stale`` (startup): skip when the file exists and was fetched within
    ``DGS3MO_STARTUP_MAX_AGE_HOURS``. The daily 09:00 call passes False and always refreshes
    (one request). A failure is logged at ERROR, never raised."""
    if key_resolver is None:
        from ba2_common.core.fred_api_key import resolve_fred_api_key as key_resolver
    if refresher is None:
        from ba2_providers.macro import fred_series
        refresher = fred_series.refresh_series
    path = path or _dgs3mo_path()
    now = now or datetime.now(timezone.utc)
    if only_if_stale:
        got = _fetched_at(path)
        if got is not None and now - got < timedelta(hours=DGS3MO_STARTUP_MAX_AGE_HOURS):
            return "current"
    key = key_resolver()
    if not key:
        logger.error("FRED DGS3MO cannot be refreshed: no FRED API key configured. Every derived "
                     "ATM-IV series is UNAVAILABLE (iv_rank gates stay closed) without it.")
        return "no_key"
    try:
        rows = refresher("DGS3MO", key)
    except Exception as e:  # noqa: BLE001 - logged, the gate stays closed
        # A requests error carries the full URL INCLUDING ``api_key=``: log a scrubbed one-liner,
        # never the exception chain / traceback.
        logger.error(f"FRED DGS3MO refresh failed: {scrub_secrets(f'{type(e).__name__}: {e}', key)}. "
                     f"Derived ATM-IV series stay unavailable once the cached rate is more than "
                     f"7 days old.")
        return "failed"
    logger.info(f"FRED DGS3MO (option risk-free rate) refreshed: {rows} observation(s)")
    return "refreshed"


def has_iv_rank_gates() -> bool:
    """Light check (rule-name scan over enabled instances' rulesets); does NOT resolve any
    expert's universe the way ``find_iv_rank_gates`` does."""
    from sqlmodel import select
    from ba2_common.core.db import get_db
    from ba2_common.core.iv_rank_audit import _iv_rank_rule_names
    from ba2_common.core.models import ExpertInstance

    with get_db() as session:
        for inst in session.exec(select(ExpertInstance).where(ExpertInstance.enabled == True)).all():  # noqa: E712
            if _iv_rank_rule_names(session, [inst.enter_market_ruleset_id, inst.open_positions_ruleset_id]):
                return True
    return False


# ---- the analysis-task seam ---------------------------------------------------------------
def invalidate_gate_cache() -> None:
    with _GATE_LOCK:
        _GATE_CACHE.clear()


def _account_id_if_iv_rank_gated(expert_instance_id: int, use_case: str) -> Optional[int]:
    """The expert instance's account id when its ruleset for ``use_case`` has an iv_rank
    condition, else None. One small query, cached per (instance, use case) for
    ``GATE_CACHE_TTL_SECONDS`` (5 minutes): a ruleset edit is seen within that time, or at once
    after ``POST /api/reload`` (which calls ``invalidate_gate_cache``)."""
    key = (int(expert_instance_id), str(use_case))
    now = time.monotonic()
    with _GATE_LOCK:
        hit = _GATE_CACHE.get(key)
        if hit is not None and now - hit[0] < GATE_CACHE_TTL_SECONDS:
            return hit[1]
    from ba2_common.core.db import get_db
    from ba2_common.core.iv_rank_audit import _iv_rank_rule_names
    from ba2_common.core.models import ExpertInstance

    result: Optional[int] = None
    with get_db() as session:
        inst = session.get(ExpertInstance, int(expert_instance_id))
        if inst is not None and inst.enabled:
            ruleset_id = (inst.enter_market_ruleset_id if use_case == "enter_market"
                          else inst.open_positions_ruleset_id if use_case == "open_positions" else None)
            if ruleset_id is not None and _iv_rank_rule_names(session, [ruleset_id]):
                result = inst.account_id
    with _GATE_LOCK:
        _GATE_CACHE[key] = (now, result)
    return result


def ensure_for_analysis_task(expert_instance_id: int, symbol: str, use_case: str, *,
                             account_resolver: Optional[Callable[[int], Any]] = None,
                             deadline_seconds: Optional[float] = None):
    """Fill the symbol's derived ATM-IV series before its analysis runs. Returns the
    ``AtmIvSeries`` or None when nothing applied. NEVER raises."""
    from ba2_common.core.iv_rank_audit import PLACEHOLDER_SYMBOLS
    if not symbol or symbol in PLACEHOLDER_SYMBOLS:
        return None                   # a selection-mode sentinel ('EXPERT', ...) is not a ticker
    try:
        account_id = _account_id_if_iv_rank_gated(expert_instance_id, use_case)
        if account_id is None:
            return None
        if account_resolver is None:
            from ba2_trade_platform.core.utils import get_account_instance_from_id as account_resolver
        account = account_resolver(account_id)
        if account is None or not hasattr(account, "_atm_iv_history"):
            return None               # not an Alpaca account: this derivation does not apply
        prov = account._atm_iv_history()
        kw = {} if deadline_seconds is None else {"deadline_seconds": deadline_seconds}
        return prov.ensure_filled(symbol, **kw)
    except Exception as e:  # noqa: BLE001 - the analysis must run; the gate stays closed
        logger.error(f"ATM-IV history update failed for {symbol} (expert {expert_instance_id}): {e}",
                     exc_info=True)
        return None
