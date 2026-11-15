"""Pre-decision fetch of the chains live iv-v1 gates read (LIVE-ONLY glue): ONE scheduler entry,
parameterised by the LIVE IV CHAIN SOURCE (app setting ``iv_live_chain_source``):

* ``alphavantage`` -> the PREFETCH below (06:30-09:45 ET of the decision day; on-demand stays possible);
* ``tastytrade`` (TastyTrade market data with the app-level market-data credentials
  ``tastytrade_market_client_secret`` / ``tastytrade_market_refresh_token``; no account definition, no
  order or account endpoint) -> the BROKER CAPTURE: between the prior session's close and the next open
  only (a broker snapshot is the prior-session chain only then, and cannot be backfilled). Attempts every 30 minutes from ``CAPTURE_START`` (20:15 ET on the session's
  date) to ``CAPTURE_END_BEFORE_OPEN`` before the next open (09:20 ET), plus a catch-up at app start
  inside that window; every attempt is validated on the data (``option_chain_quotes``), so an early or
  late attempt can only refuse, never store a wrong chain. The captured set is exactly the union of the
  BOUNDED universes of the iv-gated experts (fixed instruments / ``screener_symbol_universe``); an expert
  whose universe is unbounded is refused loudly here and at its gates.

Unset source: nothing is scheduled; an active gate refuses (naming the setting) and one ERROR is logged
when the schedule is evaluated.

THE ALPHA VANTAGE PREFETCH (the original text of this module):

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
#: Broker capture window (America/New_York): first attempt on the session's date, last attempt before the
#: next open. Conservative until the brokers' weekday snapshot behaviour is observed (tools/probe_broker_iv_snapshot.py).
CAPTURE_START_HOUR, CAPTURE_START_MINUTE = 20, 15
CAPTURE_END_BEFORE_OPEN_MINUTES = 10
#: Attempts at these minutes of every hour (inside the window only).
CAPTURE_CRON_MINUTES = "15,45"
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


# =========================================================================================== source dispatch
#: App settings of the TastyTrade MARKET-DATA credentials (blank allowed: the source then refuses).
TT_SECRET_SETTING = "tastytrade_market_client_secret"
TT_REFRESH_SETTING = "tastytrade_market_refresh_token"


def configured_source_value():
    from ba2_common.core.market_condition_iv_live import configured_live_chain_source_value

    return configured_live_chain_source_value()


def configured_source():
    """The parsed live IV chain source, or None (unset or malformed -- logged by the caller)."""
    from ba2_common.core.market_condition_iv_live import parse_live_chain_source

    try:
        return parse_live_chain_source(configured_source_value())
    except ValueError:
        return None


def _setting() -> str:
    from ba2_common.core.market_condition_iv_live import LIVE_CHAIN_SOURCE_SETTING

    return LIVE_CHAIN_SOURCE_SETTING


def tastytrade_market_data_client():
    """The market-data-only TastyTrade client over the app-level credentials (blank values allowed: the
    client then reports itself not ``configured``). Never logs or returns the credentials."""
    from ba2_common.config import get_app_setting
    from ba2_providers.options.tastytrade_quotes import TastyTradeMarketDataClient

    return TastyTradeMarketDataClient(get_app_setting(TT_SECRET_SETTING), get_app_setting(TT_REFRESH_SETTING))


def tastytrade_credentials_reason() -> str:
    """Non-empty when the TastyTrade market-data credentials are not set (names the settings)."""
    if tastytrade_market_data_client().configured:
        return ""
    return (f"the live IV chain source is 'tastytrade' but its market-data credentials are not set (app settings "
            f"{TT_SECRET_SETTING} and {TT_REFRESH_SETTING})")


def make_tastytrade_chain_reader(profile: str, *, cache_root=None, source_profile=None):
    """The ``tastytrade`` factory ``seam_wiring`` registers: the reader of the captured chains, or a refusing
    reader naming the blank credential settings."""
    from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
    from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
    from ba2_providers.market_conditions.broker_live_chain import make_reader

    reason = tastytrade_credentials_reason()
    if reason:
        return LiveUnavailableMarketConditionReader(profile, f"market-condition profile {profile!r}: {reason}; "
                                                             f"its gates refuse every live entry")
    return make_reader(profile, source="tastytrade", cache_root=cache_root,
                       source_profile=source_profile or SOURCE_PROFILE_FMP_DAILY)


def capture_universe(instances=None) -> Tuple[List[str], Dict[int, str]]:
    """``(symbols, unbounded)``: the union of the iv-gated experts' bounded universes, and the instances
    whose universe is unbounded (with the reason; a captured source cannot serve them)."""
    from ba2_common.core.market_condition_iv_live import bounded_iv_universe

    symbols: set = set()
    unbounded: Dict[int, str] = {}
    for iid, expert in (iv_gated_instances() if instances is None else instances):
        syms, reason = bounded_iv_universe(expert)
        if syms:
            symbols.update(syms)
        else:
            unbounded[iid] = reason
    return sorted(symbols), unbounded


def capture_target(now: datetime = None):
    """``(session S, window start, window end)`` (aware UTC) when ``now`` is inside the broker capture
    window of the most recent regular session that has CLOSED, else None."""
    from zoneinfo import ZoneInfo

    from ba2_common.core.market_calendar import is_regular_session, regular_session_close_utc
    from ba2_common.core.option_chain_quotes import session_close_and_next_open

    ny = ZoneInfo("America/New_York")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.astimezone(ny).date()
    for _ in range(12):
        if is_regular_session(day) and regular_session_close_utc(day) <= now:
            break
        day -= timedelta(days=1)
    else:
        return None
    _close, next_open = session_close_and_next_open(day)
    start = datetime(day.year, day.month, day.day, CAPTURE_START_HOUR, CAPTURE_START_MINUTE, tzinfo=ny)
    end = next_open - timedelta(minutes=CAPTURE_END_BEFORE_OPEN_MINUTES)
    if start.astimezone(timezone.utc) <= now < end:
        return day, start.astimezone(timezone.utc), end
    return None


def schedule_spec():
    """What the ONE scheduler entry runs: ``None`` (nothing to schedule: no active gate, or no usable live
    source -- logged) or ``{"source", "cron": CronTrigger kwargs, "describe"}``."""
    if not has_active_iv_gates():
        return None
    from ba2_common.core.market_condition_iv_live import SOURCE_ALPHAVANTAGE, parse_live_chain_source

    raw = configured_source_value()
    try:
        choice = parse_live_chain_source(raw)
    except ValueError as e:
        logger.error(f"iv-v1: active iv gates exist but app setting {_setting()} is malformed ({e}); nothing is "
                     f"fetched and the gates refuse every live entry")
        return None
    if choice is None:
        logger.error(f"iv-v1: active iv gates exist but app setting {_setting()} is not set; nothing is fetched and "
                     f"the gates refuse every live entry (choose 'alphavantage' or 'tastytrade')")
        return None
    if choice.source == SOURCE_ALPHAVANTAGE:
        return {"source": "alphavantage", "cron": {"hour": PREFETCH_HOUR, "minute": PREFETCH_MINUTE,
                                                   "day_of_week": "mon-fri"},
                "describe": f"Alpha Vantage prefetch {PREFETCH_HOUR:02d}:{PREFETCH_MINUTE:02d} America/New_York, "
                            f"Mon-Fri (deadline {DEADLINE_HOUR:02d}:{DEADLINE_MINUTE:02d})"}
    reason = tastytrade_credentials_reason()
    if reason:
        logger.error(f"iv-v1: {reason}; the capture still runs on schedule and records the refusal, the gates refuse")
    _symbols, unbounded = capture_universe()
    if unbounded:
        logger.error(f"iv-v1 {choice.source} capture: expert instance(s) {sorted(unbounded)} have an unbounded universe "
                     f"and cannot be served by a captured source; their iv gates refuse "
                     f"({next(iter(unbounded.values()))})")
    return {"source": choice.source, "cron": {"minute": CAPTURE_CRON_MINUTES},
            "describe": f"{choice.source} chain capture every 30 min from {CAPTURE_START_HOUR:02d}:"
                        f"{CAPTURE_START_MINUTE:02d} America/New_York until {CAPTURE_END_BEFORE_OPEN_MINUTES} min "
                        f"before the next open"}


def in_window(now: datetime = None) -> bool:
    """Whether the configured source's fetch window is open now (the start-up catch-up)."""
    choice = configured_source()
    if choice is None:
        return False
    if not choice.captured:
        return in_prefetch_window(now)
    return capture_target(now) is not None


def run_once(now: datetime = None) -> Dict[str, Any]:
    """The scheduler entry: dispatch on the configured source (no-op without an active gate)."""
    if not has_active_iv_gates():
        return {"skipped": "no active iv-v1 gate"}
    choice = configured_source()
    if choice is None:
        return {"skipped": f"{_setting()} not set (or malformed)"}
    if not choice.captured:
        return run_prefetch_once(now)
    return run_capture_once(now)


def run_capture_once(now: datetime = None, *, client: Any = None, cache_root: str = None,
                     instances=None) -> Dict[str, Any]:
    """One TastyTrade capture attempt for the session whose window is open now (no-op outside it). Every
    snapshot is validated on its data before it is cached; blank or rejected credentials record the
    refusal for every symbol (the gates then say why)."""
    from ba2_common.core.option_chain_quotes import FETCH_STRIKE_BAND, fetch_expiry_window
    from ba2_providers.market_conditions.broker_live_chain import capture_chains
    from ba2_providers.options.tastytrade_quotes import fetch_chain_quote_snapshot

    now = now or datetime.now(timezone.utc)
    target = capture_target(now)
    if target is None:
        return {"skipped": "outside the broker capture window"}
    session, _start, end = target
    symbols, unbounded = capture_universe(instances)
    if not symbols:
        return {"skipped": "no bounded universe to capture", "unbounded": unbounded}
    client = client or tastytrade_market_data_client()
    lo, hi = fetch_expiry_window(session)

    def snapshot(sym):
        return fetch_chain_quote_snapshot(client.get_json, sym, expiry_min=lo, expiry_max=hi,
                                          strike_band=FETCH_STRIKE_BAND, pause=lambda: time.sleep(0.2))

    remaining_s = max(0.0, (end - now.astimezone(timezone.utc)).total_seconds())
    report = capture_chains(symbols, session, snapshot_fn=snapshot, source="tastytrade", cache_root=cache_root,
                            deadline=time.monotonic() + remaining_s, log=logger.info)
    report["unbounded_instances"] = unbounded
    report["requests"] = getattr(client, "requests", None)
    if report["refused"] or report["failed"] or report["unsupported"]:
        from zoneinfo import ZoneInfo

        shown = ", ".join(list(report["refused"])[:5] + list(report["failed"])[:5])
        logger.warning(f"iv-v1 tastytrade capture {session}: {len(report['refused'])} refused (not the prior-session "
                       f"close), {len(report['failed'])} failed"
                       f"{' -- ' + report['unsupported'] if report['unsupported'] else ''} ({shown}); retried at the "
                       f"next attempt until {end.astimezone(ZoneInfo('America/New_York')):%H:%M} ET")
    return report
