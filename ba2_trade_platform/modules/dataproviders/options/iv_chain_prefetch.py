"""Pre-decision prefetch of the Alpha Vantage chains live iv-v1 gates read (LIVE-ONLY glue).

The decision pass reads each gated symbol's PRIOR-session end-of-day chain
(``ba2_providers.market_conditions.av_live_chain``). Fetching them on demand at 10:00 ET would cost the
pass one rate-limited call per symbol, so ``JobManager`` runs :func:`run_prefetch_once` early in the
morning ET (AV publishes the prior session's chains overnight; measured in the download ledgers:
available from ~00:00 ET, see docs/plans/2026-11-15-option-iv-conditions.md) for the candidate
symbols of every expert with an ACTIVE iv gate. The decision pass then finds them in the disk cache.

BACKWARD COMPATIBLE: :func:`has_active_iv_gates` is False for every installation without an enabled
expert whose ``market_condition_profile`` names iv-v1 with option source ``alphavantage`` and whose
ruleset carries an iv leaf -- then ``JobManager`` schedules nothing, logs nothing and fetches nothing.

THE CANDIDATE UNIVERSE: an expert's enabled instruments when they are static; for an expert that picks
its universe at analysis time (a screener sentinel) the universe is not knowable before the decision,
so the prefetch takes the symbols the live chain cache holds for that profile from recent sessions
(what the gated experts actually evaluated lately) and the rest stays ON DEMAND at the decision.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from ba2_trade_platform.logger import logger

IV_PROFILE = "iv-v1"
OPTION_SOURCE = "alphavantage"
#: Prefetch window: start (cron) and the deadline after which the remainder is left on demand.
PREFETCH_HOUR, PREFETCH_MINUTE = 6, 30
DEADLINE_HOUR, DEADLINE_MINUTE = 9, 45
#: Recent sessions of the live chain cache a screener expert's candidate set is taken from.
RECENT_SESSIONS = 5


def _iv_fields() -> set:
    from ba2_common.core.market_conditions import PROFILES

    return {f.name for f in PROFILES[IV_PROFILE].fields}


def iv_gated_instances() -> List[Tuple[int, Any]]:
    """``[(instance_id, expert)]`` of the ENABLED experts with an ACTIVE iv gate trained on alphavantage."""
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.instance_resolver import get_instance_resolver
    from ba2_common.core.market_condition_live import market_condition_fields_in_ruleset
    from ba2_common.core.market_condition_rules import (
        OPTION_SOURCE_SETTING, PROFILE_SETTING, parse_profile_setting,
    )
    from ba2_common.core.models import ExpertInstance

    fields = _iv_fields()
    out = []
    resolver = None
    # CHEAP PRE-FILTER: the profile setting rows naming iv-v1. Every installation without an iv gate
    # stops here -- no expert is instantiated, no ruleset read.
    candidates = _instances_naming_iv()
    if not candidates:
        return out
    for inst in get_all_instances(ExpertInstance):
        if not inst.enabled or int(inst.id) not in candidates:
            continue
        if resolver is None:
            resolver = get_instance_resolver()
        expert = resolver.get_expert_instance(int(inst.id))
        settings = expert.settings
        try:
            profiles = parse_profile_setting(settings.get(PROFILE_SETTING))
        except ValueError:
            continue
        if IV_PROFILE not in profiles or str(settings.get(OPTION_SOURCE_SETTING) or "") != OPTION_SOURCE:
            continue
        rulesets = [rid for rid in (inst.enter_market_ruleset_id, inst.open_positions_ruleset_id) if rid]
        if any(f in fields for rid in rulesets for _l, f in market_condition_fields_in_ruleset(rid)):
            out.append((int(inst.id), expert))
    return out


def _instances_naming_iv() -> set:
    """Ids whose stored ``market_condition_profile`` row mentions iv-v1 (read-only, one query)."""
    from sqlmodel import select

    from ba2_common.core.db import get_db
    from ba2_common.core.market_condition_rules import PROFILE_SETTING
    from ba2_common.core.models import ExpertSetting

    with get_db() as session:
        rows = session.exec(select(ExpertSetting).where(ExpertSetting.key == PROFILE_SETTING)).all()
    return {int(r.instance_id) for r in rows if IV_PROFILE in str(r.value_str or "")}


def has_active_iv_gates() -> bool:
    return bool(iv_gated_instances())


def prior_session(now: datetime = None) -> date:
    """The session a live decision NOW reads -- the live rule itself (``prior_session_v1``)."""
    from ba2_common.core.market_calendar import decision_data_session, live_decision_label

    return decision_data_session(live_decision_label(now or datetime.now(timezone.utc)))


def candidate_symbols(cache_root: str = None, reference: date = None) -> Tuple[List[str], List[int]]:
    """``(symbols, instances whose universe is only known at analysis time)``. For the latter, the
    symbols with a cached chain for one of the last ``RECENT_SESSIONS`` sessions (by the file's
    session date) -- what those experts actually evaluated lately."""
    from ba2_common.core.market_condition_live import UNIVERSE_SENTINELS
    from ba2_providers.market_conditions.av_live_chain import recent_symbols

    symbols: set = set()
    dynamic: List[int] = []
    for iid, expert in iv_gated_instances():
        names = [str(n).upper() for n in (expert.get_enabled_instruments() or ())]
        if any(n in UNIVERSE_SENTINELS for n in names):
            dynamic.append(iid)
        symbols.update(n for n in names if n not in UNIVERSE_SENTINELS)
    if dynamic:
        if cache_root is None:
            from ba2_common.core import native_cache
            cache_root = native_cache.CACHE_FOLDER
        symbols.update(recent_symbols(cache_root, RECENT_SESSIONS, reference))
    return sorted(symbols), dynamic


def in_prefetch_window(now: datetime = None) -> bool:
    """A weekday between the cron time (06:30 ET) and the deadline (09:45 ET)."""
    from zoneinfo import ZoneInfo

    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    start = local.replace(hour=PREFETCH_HOUR, minute=PREFETCH_MINUTE, second=0, microsecond=0)
    end = local.replace(hour=DEADLINE_HOUR, minute=DEADLINE_MINUTE, second=0, microsecond=0)
    return local.weekday() < 5 and start <= local < end


def run_prefetch_once(now: datetime = None) -> Dict[str, Any]:
    """Prefetch today's decision session's chains for the gated candidates (no-op without a gate)."""
    from zoneinfo import ZoneInfo

    from ba2_providers.market_conditions.av_live_chain import prefetch_chains

    if not has_active_iv_gates():
        return {"skipped": "no active iv-v1 gate"}
    now = now or datetime.now(timezone.utc)
    ny = ZoneInfo("America/New_York")
    local = now.astimezone(ny)
    deadline_local = local.replace(hour=DEADLINE_HOUR, minute=DEADLINE_MINUTE, second=0, microsecond=0)
    remaining_s = max(0.0, (deadline_local - local).total_seconds())
    session = prior_session(now)
    symbols, dynamic = candidate_symbols(reference=session)
    report = prefetch_chains(symbols, session, deadline=time.monotonic() + remaining_s, log=logger.info)
    report["dynamic_instances"] = dynamic
    if report["failed"]:
        shown = ", ".join(f"{s} ({r.split(':', 1)[0]})" for s, r in list(report["failed"].items())[:8])
        logger.warning(f"iv-v1 chain prefetch: {len(report['failed'])} symbol(s) not fetched ({shown}); "
                       f"they are tried again on demand at the decision")
    if dynamic:
        logger.info(f"iv-v1 chain prefetch: expert instance(s) {dynamic} pick their universe at analysis time "
                    f"(a screener run at the decision); their symbols of the last {RECENT_SESSIONS} sessions were "
                    f"prefetched, any NEW name is fetched on demand at up to 10/min and beyond that refused for "
                    f"the pass ('chain not prefetched; rate budget exhausted')")
    return report
