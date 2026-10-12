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


# ---- operator visibility: one summary per ENTER_MARKET pass ---------------------------------
_PASS_LOCK = threading.Lock()
#: (expert id, batch id) -> {"total": {symbols}, "closed": {symbol: normalised reason}}
_PASS: Dict[Tuple[int, Any], Dict[str, Any]] = {}


def _normalise_reason(symbol: str, reason: str) -> str:
    r = re.sub(re.escape(symbol), "<sym>", reason or "unknown", flags=re.IGNORECASE)
    r = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", r)
    r = re.sub(r"\d+", "#", r)
    return r[:120]


def gate_closed_reason(series, error: Optional[BaseException] = None) -> Optional[str]:
    """Why the iv_rank gate cannot open for this symbol after its fill, or None when it can.
    (The sample-count floor is not judged here; this names the DATA failures.)"""
    if error is not None:
        return f"ensure_filled raised {type(error).__name__}"
    if series is None:
        return "no ATM-IV series produced"
    if series.status != "complete":
        return f"{series.status}: {series.reason or 'no reason given'}"
    if series.end_session not in series.values:
        kind = "tombstone (no bar)" if series.end_session in series.tombstone_days else "no_spot"
        return f"no derived ATM IV for the last completed session ({kind})"
    return None


def record_gate_outcome(expert_instance_id: int, batch_id: Any, symbol: str, series,
                        error: Optional[BaseException] = None) -> None:
    reason = gate_closed_reason(series, error)
    with _PASS_LOCK:
        entry = _PASS.setdefault((int(expert_instance_id), batch_id), {"total": set(), "closed": {}})
        entry["total"].add(symbol)
        if reason is not None:
            entry["closed"][symbol] = _normalise_reason(symbol, reason)
        else:
            entry["closed"].pop(symbol, None)


_ALL = object()


def pop_gate_reports(expert_instance_id: int, batch_id: Any = _ALL) -> list:
    """Remove and return the recorded passes of the expert (memory only, no IO: safe under a lock).
    With the default every batch of the expert is returned: the caller runs when NO ENTER_MARKET task
    of the expert is pending, so every recorded batch is finished and none may leak."""
    with _PASS_LOCK:
        keys = [k for k in _PASS if k[0] == int(expert_instance_id) and (batch_id is _ALL or k[1] == batch_id)]
        return [(k[0], k[1], _PASS.pop(k)) for k in keys]


def emit_gate_reports(popped: list) -> list:
    """ONE log line + ONE Activity Log entry ``IV gate closed for N of M symbols: <reason counts>`` per
    popped pass that has a closed gate. Does IO: call OUTSIDE any lock. Never raises."""
    out = []
    for expert_id, batch_id, entry in popped:
        try:
            if not entry["closed"]:
                continue
            counts: Dict[str, int] = {}
            for r in entry["closed"].values():
                counts[r] = counts.get(r, 0) + 1
            n, m = len(entry["closed"]), len(entry["total"])
            detail = "; ".join(f"{r} x{c}" for r, c in sorted(counts.items(), key=lambda kv: -kv[1]))
            msg = f"IV gate closed for {n} of {m} symbols: {detail}"
            all_closed = n >= m
            (logger.error if all_closed else logger.warning)(
                f"[Expert {expert_id}] {msg}" + (" -- NO iv_rank-gated entry can fire this pass" if all_closed else ""))
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
            log_activity(ActivityLogSeverity.FAILURE if all_closed else ActivityLogSeverity.WARNING,
                         ActivityLogType.ANALYSIS_FAILED, msg,
                         data={"closed": n, "total": m, "reasons": counts,
                               "symbols": sorted(entry["closed"])[:200], "batch_id": str(batch_id)},
                         source_expert_id=int(expert_id))
            out.append(msg)
        except Exception as e:  # noqa: BLE001 - reporting must never break order processing
            logger.error(f"IV gate summary failed for expert {expert_id}: {e}", exc_info=True)
    return out


def flush_gate_report(expert_instance_id: int, batch_id: Any = _ALL) -> Optional[str]:
    """pop + emit in one call (tests / callers holding no lock). Returns the first message or None."""
    msgs = emit_gate_reports(pop_gate_reports(expert_instance_id, batch_id))
    return msgs[0] if msgs else None


def ensure_for_analysis_task(expert_instance_id: int, symbol: str, use_case: str, *,
                             account_resolver: Optional[Callable[[int], Any]] = None,
                             deadline_seconds: Optional[float] = None, batch_id: Any = None):
    """Fill the symbol's derived ATM-IV series before its analysis runs. Returns the
    ``AtmIvSeries`` or None when nothing applied. NEVER raises."""
    from ba2_common.core.iv_rank_audit import PLACEHOLDER_SYMBOLS
    if not symbol or symbol in PLACEHOLDER_SYMBOLS:
        return None                   # a selection-mode sentinel ('EXPERT', ...) is not a ticker
    gated = False
    try:
        account_id = _account_id_if_iv_rank_gated(expert_instance_id, use_case)
        if account_id is None:
            return None
        gated = True
        if account_resolver is None:
            from ba2_trade_platform.core.utils import get_account_instance_from_id as account_resolver
        account = account_resolver(account_id)
        if account is None or not hasattr(account, "_atm_iv_history"):
            return None               # not an Alpaca account: this derivation does not apply
        prov = account._atm_iv_history()
        kw = {} if deadline_seconds is None else {"deadline_seconds": deadline_seconds}
        series = prov.ensure_filled(symbol, **kw)
        if str(use_case).lower().endswith("enter_market"):
            record_gate_outcome(expert_instance_id, batch_id, symbol, series)
        return series
    except Exception as e:  # noqa: BLE001 - the analysis must run; the gate stays closed
        logger.error(f"ATM-IV history update failed for {symbol} (expert {expert_instance_id}): {e}",
                     exc_info=True)
        if gated and str(use_case).lower().endswith("enter_market"):
            record_gate_outcome(expert_instance_id, batch_id, symbol, None, error=e)
        return None


# ---- pre-open warm ---------------------------------------------------------------------------
WARM_DEADLINE_ET = (9, 20)            # the warm must be finished by 09:20 America/New_York
WARM_FORBIDDEN_ET = ((9, 25), (9, 45))  # never run inside the FMP 09:30 collision window
WARM_CATCHUP_ET = ((8, 31), (9, 15))  # an app (re)started in this window runs the warm once, at once
WARM_PER_SYMBOL_SECONDS = 180.0
WARM_PACE_SECONDS = 1.0               # between symbols: FMP-friendly (sequential, no concurrency)


def _ny():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def warm_catchup_due(now: Optional[datetime] = None) -> bool:
    """True on a weekday between 08:31 and 09:15 ET: a restart after the 08:30 cron missed the warm."""
    t = (now or datetime.now(_ny())).astimezone(_ny())
    return t.weekday() < 5 and WARM_CATCHUP_ET[0] <= (t.hour, t.minute) < WARM_CATCHUP_ET[1]


def warm_iv_history(*, targets: Optional[Dict[int, list]] = None,
                    account_resolver: Optional[Callable[[int], Any]] = None,
                    now: Optional[Callable[[], datetime]] = None,
                    sleep: Callable[[float], None] = time.sleep,
                    pace_seconds: float = WARM_PACE_SECONDS) -> Dict[str, Any]:
    """Pre-open pass: for every iv_rank-gated symbol (sequentially, paced) run the same
    ``ensure_filled`` the 09:30 analysis would (FMP OHLCV top-up + split calendar included), so
    the 09:30 pass finds the store warm. Deep spot retries (the in-task path gets one). The deadline
    (09:20) and the 09:25-09:45 window are re-checked between symbols AND before every FMP call
    (``guard``). Logs ONE INFO summary (complete / filling / failed with reasons). Never raises."""
    from .atm_iv_history import SPOT_RETRIES_WARM
    ny = _ny()
    clock = now or (lambda: datetime.now(ny))
    summary: Dict[str, Any] = {"complete": 0, "filling": 0, "failed": 0, "skipped_deadline": 0,
                               "reasons": {}, "total": 0}

    def in_window(t: datetime) -> bool:
        return WARM_FORBIDDEN_ET[0] <= (t.hour, t.minute) < WARM_FORBIDDEN_ET[1]

    try:
        t0 = clock().astimezone(ny)
        if in_window(t0):
            logger.warning(f"IV warm skipped: started {t0:%H:%M} ET inside the 09:25-09:45 FMP collision window")
            summary["skipped_window"] = True
            return summary
        if targets is None:
            from ba2_common.core.iv_rank_audit import find_iv_rank_gates, recording_targets
            gates = find_iv_rank_gates()
            blind = [g for g in gates if not g.universe_is_known]
            if blind:
                logger.warning(f"IV warm cannot see the universe of {len(blind)} iv_rank-gated expert(s) "
                               f"({', '.join(str(g.expert_id) for g in blind)}); their symbols are not warmed")
            targets = recording_targets(gates)
        if account_resolver is None:
            from ba2_trade_platform.core.utils import get_account_instance_from_id as account_resolver
        deadline = t0.replace(hour=WARM_DEADLINE_ET[0], minute=WARM_DEADLINE_ET[1], second=0, microsecond=0)
        work = [(account_id, sym) for account_id, symbols in sorted(targets.items()) for sym in symbols]
        summary["total"] = len(work)

        def guard() -> Optional[str]:
            t = clock().astimezone(ny)
            if t >= deadline:
                return "warm deadline 09:20 ET reached"
            if in_window(t):
                return "inside the 09:25-09:45 ET FMP collision window"
            return None

        def note(kind: str, reason: Optional[str] = None, sym: str = ""):
            summary[kind] += 1
            if reason:
                r = _normalise_reason(sym, reason)
                summary["reasons"][r] = summary["reasons"].get(r, 0) + 1

        for i, (account_id, sym) in enumerate(work):
            if guard() is not None:
                summary["skipped_deadline"] = len(work) - i
                break
            try:
                account = account_resolver(account_id)
                if account is None or not hasattr(account, "_atm_iv_history"):
                    note("failed", "account has no ATM-IV history provider", sym)
                    continue
                left = (deadline - clock().astimezone(ny)).total_seconds()
                series = account._atm_iv_history().ensure_filled(
                    sym, deadline_seconds=max(1.0, min(WARM_PER_SYMBOL_SECONDS, left)),
                    spot_retries=SPOT_RETRIES_WARM, guard=guard)
                if series.status == "complete" and series.end_session in series.values:
                    note("complete")
                elif series.status == "filling":
                    note("filling", series.reason or "filling", sym)
                else:
                    note("failed", gate_closed_reason(series), sym)
            except Exception as e:  # noqa: BLE001 - one symbol never stops the pass
                logger.error(f"IV warm failed for {sym}: {e}", exc_info=True)
                note("failed", f"{type(e).__name__}", sym)
            sleep(pace_seconds)
        reasons = "; ".join(f"{r} x{c}" for r, c in sorted(summary["reasons"].items(), key=lambda kv: -kv[1]))
        logger.info(f"IV warm summary: {summary['complete']} complete / {summary['filling']} filling / "
                    f"{summary['failed']} failed of {summary['total']} symbol(s)"
                    + (f"; {summary['skipped_deadline']} not reached before the 09:20 ET deadline"
                       if summary["skipped_deadline"] else "")
                    + (f"; reasons: {reasons}" if reasons else ""))
    except Exception as e:  # noqa: BLE001
        logger.error(f"IV warm pass aborted: {e}", exc_info=True)
        summary["aborted"] = f"{type(e).__name__}: {e}"
    return summary
