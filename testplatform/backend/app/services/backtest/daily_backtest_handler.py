"""``daily_backtest`` task handler (Phase 2 Task 5).

Contract matches the existing handlers (``handle_backtest`` etc.):
``handler(task_id: str, payload: dict) -> result dict``; a returned ``{'status':'failed',...}``
marks the task failed (``TaskQueueService._process_task``). The handler:

  1. validates the payload fail-early (no-defaults rule, ``backend/CLAUDE.md``);
  2. loads the host ``Backtest`` row (the results row), sets it ``running``;
  3. wires the ba2 seams, opens a per-run backtest TRADING DB, seeds the AccountDefinition +
     ExpertInstance rows, builds the ``BacktestAccount`` + the (clean) expert instances;
  4. runs ``DailyBacktestEngine`` (the real ba2trade order path), polling ``is_task_paused``
     and pushing ``update_progress`` via the engine's progress callback;
  5. converts the finished account to the metric blob (``results.build_results``) and persists
     every metric column + the equity/drawdown/trades JSON onto the ``Backtest`` row,
     ``status='completed'``.

Two distinct DBs (per the replan): the ``Backtest`` RESULTS row lives in the host
``app.models.database.SessionLocal`` DB; the TRADING rows (TradingOrder/Transaction/...) live
in the separate per-run ``ba2_common.core.db`` sqlite (``backtest_trading_db``).
"""
from __future__ import annotations

import logging
import os
import pathlib
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ba2_common.core.types import is_option_action

from app.models.backtest import Backtest
from app.models.database import SessionLocal
from app.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)


# Alpaca's options-history floor: there is no chain/bar data before this date, so an
# options backtest that starts earlier would silently see empty chains. Reject it with a
# clear error instead (a missing cache still fails fast via OptionsCacheMiss).
_OPTIONS_HISTORY_FLOOR = date(2024, 2, 1)


def validate_options_window(start, uses_options: bool) -> None:
    """Reject option backtests before Alpaca's 2024-02-01 options-history floor."""
    if not uses_options:
        return
    if isinstance(start, datetime):
        d = start.date()
    elif isinstance(start, date):
        d = start
    else:
        d = date.fromisoformat(str(start)[:10])
    if d < _OPTIONS_HISTORY_FLOOR:
        raise ValueError(
            f"Options backtests require start >= {_OPTIONS_HISTORY_FLOOR.isoformat()} "
            f"(Alpaca options history floor); got {d.isoformat()}.")


def strategy_uses_options(cfg: Dict[str, Any]) -> bool:
    """True iff ANY exit/RM rule names an OPTION action (``is_option_action``).

    A strategy's exit/RM rules can now carry an option action (buy_call, sell_covered_call,
    open_bull_call_spread, ...) instead of an equity action (close / adjust_stop_loss). When
    any such rule is present the run is an OPTIONS run: the handler derives an
    ``options_cache_db`` so the Plan-1 seam builds + injects the HistoricalOptionsProvider and
    validates the Feb-2024 window. When none is present the run is equity-only (unchanged).

    The option action can live under the canonical evaluator key ``action_type``, the API/UI
    alias ``action``, or the (forward-compat) ``option_strategy`` key — checked in that
    precedence. Rules are read from ``exit_rules`` (the canonical handler key) else the
    API-shaped ``exit_conditions`` alias. Non-dict rules are ignored (no crash).

    ALSO True when ``cfg["entry_action"]`` names an option action — a pure-option ENTRY (the
    enter_market ruleset fires the option directly, no equity leg), which is an options run even
    when no exit rule names an option."""
    ea = cfg.get("entry_action")
    if isinstance(ea, dict):
        a = ea.get("option_strategy") or ea.get("action_type") or ea.get("action")
        if a and is_option_action(str(a)):
            return True
    for rule in (cfg.get("exit_rules") or cfg.get("exit_conditions") or []):
        if not isinstance(rule, dict):
            continue
        action = rule.get("option_strategy") or rule.get("action_type") or rule.get("action")
        if action and is_option_action(str(action)):
            return True
    return False


# Default offline options-cache filename, placed under the same datasets/cache dir family the
# OHLCV/screener caches use (``datasets/cache``; overridable via ``BACKTEST_OPTIONS_CACHE_DB``
# for an explicit path, or ``BACKTEST_CACHE_DIR`` for just the directory). The cache itself is
# built once by ``ba2-test fetch-options`` — a missing/empty cache fails fast at read time with
# OptionsCacheMiss (the build-the-cache message), never a silent empty chain.
_DEFAULT_OPTIONS_CACHE_FILENAME = "options_cache.sqlite"


def default_options_cache_db() -> str:
    """Path to the offline options cache used when a strategy needs options but the payload
    did not pin ``options_cache_db``. ``BACKTEST_OPTIONS_CACHE_DB`` overrides the full path;
    else ``<BACKTEST_CACHE_DIR>/options_cache.sqlite`` when that env is set, otherwise the
    shared options cache dir under ba2_common (``~/Documents/ba2/common/options``) — never the
    repo/CWD. The directory is created on demand so the path is usable (the cache builder/reader
    opens the sqlite there)."""
    explicit = os.environ.get("BACKTEST_OPTIONS_CACHE_DB")
    if explicit:
        return explicit
    backtest_cache_dir = os.environ.get("BACKTEST_CACHE_DIR")
    if backtest_cache_dir:
        cache_dir = pathlib.Path(backtest_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir / _DEFAULT_OPTIONS_CACHE_FILENAME)
    # Canonical shared options cache: the SAME file ``ba2-test fetch-options`` writes and the
    # distributed workers expect (``ba2_common.config.OPTIONS_CACHE_DB`` ->
    # ``.../options/options_history.sqlite``). Previously this returned a sibling
    # ``options_cache.sqlite`` in the same dir, so a locally-built cache was never found by a
    # local optimize run — reconciled here to one canonical path.
    from ba2_common.config import OPTIONS_CACHE_DB
    pathlib.Path(OPTIONS_CACHE_DB).parent.mkdir(parents=True, exist_ok=True)
    return OPTIONS_CACHE_DB


# Payload keys the handler REQUIRES (validated fail-early, no defaults).
# ``enabled_instruments`` is NOT in this list: it is either supplied directly (static
# universe) or RESOLVED from the offline screener cache (screener universe) in
# ``_build_config``; the post-resolution non-empty check lives there so a screener run is
# not rejected for lacking a static symbol list.
REQUIRED_KEYS = [
    "backtest_id",
    "experts",
    "start_date",
    "end_date",
    "initial_capital",
    "commission",
    "slippage",
    "fill_model",
    "seed",
]

# The clean (no-LLM) experts this phase ships. The payload's ``experts`` list names a subset
# of these by class name; anything else is rejected fail-early (a typo must not silently no-op).
_SUPPORTED_EXPERTS = {
    "FMPEarningsDrift": "ba2_experts.FMPEarningsDrift",
    "FMPInsiderClusterBuy": "ba2_experts.FMPInsiderClusterBuy",
    # FMP analyst price-target consensus ("FMPConsensus"). Backtestable: analyze_as_of +
    # no-lookahead as_of reconstruction (grades-historical + v4/price-target history), no LLM.
    "FMPRating": "ba2_experts.FMPRating",
    # BYPASS expert (piece 1): FactorRanker declares ``bypasses_classic_rm`` — it does NOT use
    # the enter/exit ruleset or the classic RM, and rebalances to target weights via its own
    # FactorPortfolioManager. ``_build_experts`` detects the marker and skips ruleset seeding /
    # RM-gate enabling for it; the engine routes its targets straight to the portfolio manager.
    "FactorRanker": "ba2_experts.FactorRanker",
    # Classic (non-bypass) signal experts, also no-LLM + analyze_as_of-driven:
    #  * FinnHubRating — analyst recommendation-trend rating (needs the ``finnhub_api_key`` setting;
    #    no LLM). Recommendation trends are disk-cached (backtest-only) like the FMP histories.
    #  * FMPSenateTraderWeight / FMPSenateTraderCopy — US congressional-trade signal (FMP). Copy
    #    declares ``required_instrument_selection_method: "expert"`` (it surfaces its own names),
    #    so it is best run with a screener/expert universe rather than a narrow static list.
    "FinnHubRating": "ba2_experts.FinnHubRating",
    "FMPSenateTraderWeight": "ba2_experts.FMPSenateTraderWeight",
    "FMPSenateTraderCopy": "ba2_experts.FMPSenateTraderCopy",
}


# Max indicator/lookback window (in trading BARS) each expert needs warmed up before the
# first trading bar. The classic-RM ATR (~14) is the floor; FactorRanker's 12-1 month
# momentum needs a full year. An expert may override via a ``BACKTEST_WARMUP_BARS`` class attr.
_EXPERT_WARMUP_BARS = {
    "FactorRanker": 252,          # momentum_12_1 lookback (12 months)
    "FMPRating": 10,
    "FMPEarningsDrift": 10,
    "FMPInsiderClusterBuy": 10,
    "FinnHubRating": 10,          # recommendation-trend rating; no long OHLCV lookback
    "FMPSenateTraderWeight": 10,  # recent congressional trades; ATR floor governs warmup
    "FMPSenateTraderCopy": 10,
}
_WARMUP_FLOOR_DAYS = 60           # never warm up less than this (ATR + safety)
_BARS_TO_CALDAYS = 1.45           # trading bars -> calendar days (≈252 bars/year -> ~365 days)


def derive_warmup_days(expert_specs: List[Any]) -> int:
    """Calendar-day warmup window derived from the run's experts' max bar lookback.

    Looks up each expert's lookback (the class's ``BACKTEST_WARMUP_BARS`` if present, else
    the ``_EXPERT_WARMUP_BARS`` table, else a small default), takes the max, converts BARS ->
    calendar days, and floors at ``_WARMUP_FLOOR_DAYS``. Used when the payload does not pin
    ``warmup_days`` so indicators (200-EMA, 252-bar momentum, ...) get enough history.
    """
    max_bars = 14  # ATR-14 floor
    for spec in expert_specs or []:
        name = spec.get("class") if isinstance(spec, dict) else spec
        bars = _EXPERT_WARMUP_BARS.get(name, 20)
        mod_path = _SUPPORTED_EXPERTS.get(name)
        if mod_path:
            try:
                import importlib
                cls = getattr(importlib.import_module(mod_path), name)
                bars = int(getattr(cls, "BACKTEST_WARMUP_BARS", bars))
            except Exception:  # noqa: BLE001 — fall back to the table value
                pass
        max_bars = max(max_bars, bars)
    return max(_WARMUP_FLOOR_DAYS, int(max_bars * _BARS_TO_CALDAYS) + 10)


class _Paused(Exception):
    """Raised from the progress callback when the task is paused (surfaces as a failure
    with a clear message — the queue's pause/resume re-queues the task)."""


def handle_daily_backtest(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run a daily multi-asset expert backtest and persist the ``Backtest`` results row."""
    # --- validate fail-early (no defaults) ---------------------------------
    for key in REQUIRED_KEYS:
        if payload.get(key) is None:
            return {"status": "failed", "error": f"payload.{key} is required"}

    backtest_id = payload["backtest_id"]
    tq = get_task_queue()
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if bt is None:
            return {"status": "failed", "error": f"Backtest {backtest_id} not found"}

        bt.status = "running"
        bt.started_at = datetime.now()
        db.commit()

        try:
            config = _build_config(payload)
        except (KeyError, ValueError) as e:
            _fail(db, bt, str(e))
            return {"status": "failed", "error": str(e)}

        def progress(pct: float, msg: str) -> None:
            if tq.is_task_paused(task_id):
                raise _Paused(msg)
            tq.update_progress(task_id, pct, msg)

        results = run_daily_backtest(config, progress_cb=progress)

        _persist_results(db, bt, results)
        bt.status = "completed"
        bt.completed_at = datetime.now()
        db.commit()
        logger.info(
            f"Daily backtest {backtest_id} completed: {results.get('total_trades', 0)} trades, "
            f"return={results.get('total_return')}%"
        )
        return {"status": "completed", "backtest_id": backtest_id, "results": results}

    except _Paused as e:
        # Leave the row 'running' status untouched? No — surface paused as a failure so the
        # row is not stuck; the queue handles re-queue on resume independently.
        _fail(db, bt, f"paused: {e}")
        return {"status": "failed", "error": "paused"}
    except Exception as e:  # noqa: BLE001 — any engine failure must fail the row, not crash the worker
        logger.error(f"Daily backtest {backtest_id} failed: {e}", exc_info=True)
        try:
            row = db.query(Backtest).filter(Backtest.id == backtest_id).first()
            if row is not None:
                _fail(db, row, str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# The metric-store key normalizer now lives in ba2_providers.screener.metric_store (next to the
# gate that consumes the keys — single source of truth, so the optimizer path can reuse it too).
# Kept as a thin re-export so existing call sites in this module are unchanged.
from ba2_providers.screener.metric_store import normalize_screener_settings as _metric_store_settings


def _resolve_screener_store(universe: Dict[str, Any]) -> str:
    """The metric-store dir for a screener run: the explicit ``universe.screener_store`` if given,
    else the canonical default ``SCREENER_STORE_DIR`` (where ``ba2-test build-screener-metrics``
    writes). Validates the dir exists so a missing store fails with an actionable message instead
    of a cryptic empty-union error — and so a single screener BT 'just works' against the default
    store without the caller having to pass the path."""
    from ba2_common.config import SCREENER_STORE_DIR
    store = universe.get("screener_store") or SCREENER_STORE_DIR
    if not os.path.isdir(store):
        raise ValueError(
            f"screener metric_store not found at {store!r} — build it first, e.g.: "
            f"ba2-test build-screener-metrics --store {store} --start <YYYY-MM-DD> "
            f"--end <YYYY-MM-DD> --market-cap-min <N>"
        )
    return store


def _build_screener_runtime(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """For a ``universe.mode=='screener'`` run, build the per-bar gate config the engine reads.

    ``{store, settings, cadence_days}`` — identical shape to the optimizer's
    ``strategy_optimization_handler._build_daily_trial_config``. The engine's
    ``_screened_symbols_for_bar`` resolves ``metric_store.screen_universe_as_of`` at each bar
    (point-in-time, cached), restricting entries to that bar's survivors. None for non-screener
    runs -> the engine gate is a no-op (byte-identical to a static run).
    """
    universe = payload.get("universe") or {}
    if universe.get("mode") != "screener":
        return None
    return {
        "store": _resolve_screener_store(universe),
        "settings": _metric_store_settings(universe.get("screener_settings") or {}),
        "cadence_days": int(universe.get("screener_cadence_days") or 7),
    }


def _build_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse + assemble the engine run config from the validated payload.

    ``account_settings`` is the BacktestAccount's resolved config dict (the no-defaults keys
    the account reads: starting_cash / commission_per_trade / slippage_bps / fill_model).
    Raises ValueError on a bad date / unsupported expert (fail-early).
    """
    start_date = _parse_dt(payload["start_date"], "start_date")
    end_date = _parse_dt(payload["end_date"], "end_date")
    if end_date < start_date:
        raise ValueError("payload.end_date must be on or after start_date")

    expert_specs = payload["experts"]
    if not isinstance(expert_specs, list) or not expert_specs:
        raise ValueError("payload.experts must be a non-empty list")
    for spec in expert_specs:
        name = spec.get("class") if isinstance(spec, dict) else spec
        if name not in _SUPPORTED_EXPERTS:
            raise ValueError(
                f"unsupported expert '{name}'; supported: {sorted(_SUPPORTED_EXPERTS)}"
            )

    enabled_instruments = _resolve_enabled_instruments(payload, start_date, end_date)

    initial_capital = float(payload["initial_capital"])
    account_settings = {
        "starting_cash": initial_capital,
        "commission_per_trade": float(payload["commission"]),
        "slippage_bps": float(payload["slippage"]),
        "fill_model": str(payload["fill_model"]),
    }

    # warmup_days: longest indicator/lookback window the experts need preloaded before
    # start_date. If the payload sets it explicitly, honour that; otherwise DERIVE it from
    # the experts' max indicator lookback (in bars -> calendar days) so e.g. a 200-EMA or
    # FactorRanker's 252-bar momentum actually has its lookback. This is a fetch-window
    # sizing knob, NOT a trading parameter, so deriving it never affects a decision.
    warmup_days = (int(payload["warmup_days"]) if payload.get("warmup_days") is not None
                   else derive_warmup_days(expert_specs))

    # Options seam (Plan-2 Task 6): an exit/RM rule may now name an OPTION action. Detect that
    # from the payload's rules; when the strategy uses options and no explicit cache path was
    # supplied, derive the default offline options-cache path. A non-None ``options_cache_db``
    # is the Plan-1 seam trigger: ``run_daily_backtest`` builds + injects the
    # HistoricalOptionsProvider from it. Validate the Feb-2024 options-history floor here so an
    # out-of-window options run fails early with a clear message. Equity-only runs (no option
    # rule) keep ``options_cache_db`` None and skip validation — behaviour is byte-identical.
    options_cache_db = payload.get("options_cache_db")
    uses_options = strategy_uses_options(payload)
    if uses_options and not options_cache_db:
        options_cache_db = default_options_cache_db()
    validate_options_window(start_date, uses_options or bool(options_cache_db))

    return {
        "backtest_id": payload["backtest_id"],
        "name": payload.get("name", f"daily-backtest-{payload['backtest_id']}"),
        "start_date": start_date,
        "end_date": end_date,
        "enabled_instruments": enabled_instruments,
        "experts": expert_specs,
        "initial_capital": initial_capital,
        "account_settings": account_settings,
        "warmup_days": warmup_days,
        "seed": int(payload["seed"]),
        "subtype": payload.get("subtype"),
        # Entry cadence (optimizer/CLI seam): {"days": {weekday: bool}, "times": [...]}.
        # None/absent -> analyse every bar (legacy). The engine's _entry_schedule honours it.
        "run_schedule_override": payload.get("run_schedule_override"),
        # Open-positions MANAGEMENT cadence (separate from entry, mirrors live). The engine's
        # _manage_schedule honours it; None/absent -> falls back to the entry schedule (legacy).
        "manage_schedule_override": payload.get("manage_schedule_override"),
        # Optimizer/API condition trees: when a buy-entry tree is present the engine builds the
        # enter ruleset FROM it (_build_experts -> seed_ruleset_from_tree) so the condition
        # thresholds + on/off toggles gate entries; else it falls back to the bullish+flat
        # default. The optimizer builds its config dict directly (bypassing _build_config), so
        # these forwards are what carry the trees through the API/CLI create path. sell_tree /
        # exit_rules are plumbed through for the engine's follow-up consumption.
        "buy_tree": payload.get("buy_tree"),
        "sell_tree": payload.get("sell_tree"),
        "exit_rules": payload.get("exit_rules"),
        # Initial TP/SL bracket percents applied per opened position so trades close (the
        # engine's _apply_initial_brackets reads these). Optional on the standalone path;
        # the optimizer always supplies them via the tp/sl genes.
        "initial_tp_percent": payload.get("initial_tp_percent"),
        "initial_sl_percent": payload.get("initial_sl_percent"),
        # CANONICAL take-profit reference key. ``_apply_initial_brackets`` reads
        # ``initial_tp_reference``: unset / anything but "expert_target_price" -> the legacy
        # percent-off-entry TP (default); "expert_target_price" -> anchor the TP on the
        # recommendation's target_price (RE4). This is the SINGLE TP-reference vocabulary —
        # the legacy ``initial_tp_ref`` name is accepted here as an ALIAS (the only place the
        # alias is collapsed) so both spellings reach the one canonical config key + code path.
        "initial_tp_reference": (
            payload.get("initial_tp_reference")
            if payload.get("initial_tp_reference") is not None
            else payload.get("initial_tp_ref")
        ),
        # Intraday fill clock (e.g. "1h"/"15m"); 1d default. Decoupled from entry cadence.
        "execution_interval": payload.get("execution_interval", "1d"),
        # Options seam: path to the offline OptionsHistoryCache sqlite (built via
        # ``ba2-test fetch-options``). Present -> the run uses options: run_daily_backtest
        # builds a HistoricalOptionsProvider from it, injects it into the BacktestAccount,
        # and the Feb-2024 window is validated. Set explicitly in the payload OR DERIVED above
        # when the strategy's exit/RM rules name an option action; absent/None -> equity-only
        # (unchanged).
        "options_cache_db": options_cache_db,
        # Screener (universe.mode=='screener'): per-bar metric_store entry gate (point-in-time,
        # cached) — same mechanism the optimizer uses. None for static runs (engine gate no-op).
        "screener_runtime": _build_screener_runtime(payload),
    }


def _resolve_enabled_instruments(
    payload: Dict[str, Any], start_date: datetime, end_date: datetime
) -> List[str]:
    """Resolve the run's instrument list: static symbols OR the screener metric_store union.

    Two universe shapes are supported (discriminated by ``payload['universe']['mode']``):

      * static (default, or ``universe.mode == 'static'``): the explicit ``enabled_instruments``
        list the payload carries — behaviour unchanged.
      * screener (``universe.mode == 'screener'``): the CANDIDATE superset is the symbol union of
        the prebuilt ``metric_store`` parquet (``universe.screener_store``, built via
        ``ba2-test build-screener-metrics``). The engine then GATES entries PER BAR to the
        point-in-time screened set via ``screener_runtime`` (see ``_build_screener_runtime``) —
        the SAME path the optimizer uses. (Replaces the old offline ``ScreenerHistoryCache``
        static-union, which was both non-dynamic AND a lookahead — it admitted names that only
        passed the screen LATER in the range.)
    """
    universe = payload.get("universe") or {}
    mode = universe.get("mode")

    if mode == "screener":
        from ba2_providers.screener import metric_store as ms

        store = _resolve_screener_store(universe)  # explicit path, else default SCREENER_STORE_DIR
        # Candidate universe = the symbols THIS run's screen can ever select (the union of the
        # per-bar screen over the window), NOT the whole store. The store is the loosest-bound
        # superset of every gene (e.g. 868 symbols) but a given run selects far fewer (~26) —
        # preloading the full store loads/holds OHLCV for ~800 never-touched symbols (huge memory
        # + load time, and one data-less symbol aborts the run). The per-bar screener_runtime gate
        # still restricts entries each bar; this only bounds what OHLCV gets loaded.
        df = ms.load_store(store)
        settings = _metric_store_settings(universe.get("screener_settings") or {})
        instruments = ms.screened_symbol_union(
            df, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), settings
        )
        if not instruments:
            raise ValueError(
                f"screener metric_store {store!r} selected zero symbols for the window/settings "
                f"(check screener_settings / build the store via ba2-test build-screener-metrics)"
            )
        return instruments

    # Static universe (default): the explicit instrument list (fail-early if absent/empty).
    instruments = payload.get("enabled_instruments")
    if not instruments:
        raise ValueError("payload.enabled_instruments is required for a static universe")
    return list(instruments)


# ---------------------------------------------------------------------------
# Engine run (the per-run trading DB scope)
# ---------------------------------------------------------------------------
def run_daily_backtest(
    config: Dict[str, Any],
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """Run ONE daily multi-asset backtest synchronously, in-process, and return the
    results metric blob (the ``results.build_results`` shape).

    This is the SYNCHRONOUS core extracted from ``handle_daily_backtest`` so it can be
    called directly (e.g. by the joint genetic optimizer fitness function, which must
    NOT enqueue a sub-task under ``max_workers=1``). It opens the per-run trading DB,
    seeds the account + experts, runs ``DailyBacktestEngine``, and converts the finished
    account into the full metric blob.

    Determinism: the engine seeds ``random``/``numpy`` from ``config["seed"]`` at the start
    of ``run()`` so a run is byte-reproducible (same cache + same config + same seed =>
    identical equity curve / metrics).

    Args:
        config: the engine run config dict (the shape ``_build_config`` produces). Required
            keys: ``backtest_id``, ``account_settings``, ``enabled_instruments``,
            ``start_date``, ``end_date``, ``warmup_days``, ``experts``, ``seed``.
        progress_cb: optional ``callable(pct: float, msg: str)`` invoked once per bar
            (the handler wires pause/progress through it). Defaults to a no-op so a direct
            in-process call (the optimizer) needs no task queue.

    Returns:
        The results dict (``build_results`` output): total_trades / win_rate / total_return /
        sharpe_ratio / max_drawdown / profit_factor / ... + equity_curve / drawdown_curve /
        trades.
    """
    from ba2_common.core.db import activity_logging_disabled
    from ba2_common.logger import file_logging_disabled
    from ba2_providers import get_provider
    from ba2_providers.fmp_common import frozen_ttl_cache, hermetic_fmp_history

    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from datetime import timedelta

    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import (
        AsOfClampedOHLCVProvider,
        AsOfPriceSource,
        MemoizedOHLCVProvider,
    )
    from app.services.backtest.results import build_results
    from app.services.backtest.seam_wiring import (
        make_indicator_provider,
        set_backtest_ohlcv_override,
        wire_backtest_seams,
    )

    progress = progress_cb or (lambda pct, msg: None)

    # Accept ISO string dates: the joint optimizer's _build_daily_trial_config forwards the
    # dates straight from the JSON optimization_config (strings), and AsOfPriceSource.preload
    # does start - timedelta. Coerce once here so every caller (CLI/API/optimizer) is safe.
    config = {
        **config,
        "start_date": _parse_dt(config["start_date"], "start_date"),
        "end_date": _parse_dt(config["end_date"], "end_date"),
        # Per-day DYNAMIC screener universe (screener-settings optimization). The optimizer's
        # trial config sets ``screener_runtime`` ({"store", "settings"[, "cadence_days"]}); the
        # engine reads it (``self._screener_runtime``) to gate ENTRIES to the per-day screened
        # universe. Forwarded explicitly here so the engine constructor receives it; absent/None
        # on every non-screener run -> the engine's entry gate is a no-op (behaviour unchanged).
        "screener_runtime": config.get("screener_runtime"),
    }

    # Free the PREVIOUS run's OHLCV memo if this run's working set (universe + window + interval)
    # differs. The memo is kept across one job's GA population (the big perf win) but the pool
    # workers + the master process are long-lived across jobs, so without this they accumulate every
    # band's universe (504-symbol large-cap, then 814-symbol mid-cap, then small-cap, ...) and never
    # free it — the worker memory leak. This chokepoint covers ALL callers (remote/local pool
    # workers, the master's in-process top-N persist, parallel=1, standalone) in one place.
    from app.services.backtest.price_source import evict_memo_if_working_set_changed
    evict_memo_if_working_set_changed((
        tuple(sorted(config.get("enabled_instruments") or [])),
        config.get("execution_interval", "1d"),
        str(config["start_date"]), str(config["end_date"]),
        int(config.get("warmup_days") or 0),
    ))

    # Options seam: a present ``options_cache_db`` flags an options run. Build the as-of
    # clamped HistoricalOptionsProvider from it and inject it into the account; a missing
    # cache fails fast (OptionsCacheMiss is raised by the cache reader, not swallowed).
    # Validate the Feb-2024 options-history floor BEFORE the run starts (clear error vs.
    # silently empty chains). Equity-only runs (no cache db) are unaffected.
    options_cache_db = config.get("options_cache_db")
    uses_options = bool(options_cache_db)
    validate_options_window(config["start_date"], uses_options)
    if uses_options:
        from .options_provider import HistoricalOptionsProvider

        options_provider = HistoricalOptionsProvider(options_cache_db)
    else:
        options_provider = None

    resolver = wire_backtest_seams()
    account_id = 1

    # Backtest perf, scoped to the whole run (live path unaffected):
    #   * frozen_ttl_cache()      — FMP fundamentals are fetched once per symbol and reused
    #                               for every as_of bar (no 15-min TTL re-fetch mid-run).
    #   * activity_logging_disabled() — silence the per-bar ActivityLog write churn (from
    #                               TradeActionEvaluator / TradeRiskManagement), which would
    #                               otherwise serialize thousands of writes through the DB lock.
    # The per-run trading DB is RAM-only by default (fast GA fitness path); a tagged top-N
    # re-run sets ``persist_trading_db`` so its full instance/analysis rows are kept on disk.
    _persist_db = bool(config.get("persist_trading_db", False))
    # hermetic_fmp_history(): a backtest must run from PRE-WARMED caches only (0 network fetches);
    # a missing per-symbol history raises FMPHistoryCacheMiss instead of silently fetching mid-run.
    # file_logging_disabled(): file logging is LIVE-only — suppress the rotating FILE handlers for
    # this run so in-process re-run worker THREADS (serve) never race on RotatingFileHandler
    # rollover/close ("I/O operation on closed file"). STDOUT still logs.
    with backtest_trading_db(config["backtest_id"], in_memory=not _persist_db), \
            frozen_ttl_cache(), hermetic_fmp_history(), activity_logging_disabled(), \
            file_logging_disabled():
        seed_account_definition(account_id, config["account_settings"])

        # Time-machine price source backed by the FMP OHLCV provider (as_of-aware).
        # execution_interval governs the FILL clock granularity (default 1d). Intraday
        # values (e.g. "1h", "15m") give finer open/close fill detection; it is decoupled
        # from whatever interval the experts request via the provider seam in _gather.
        interval = config.get("execution_interval", "1d")
        # Memoize each symbol's full [start - warmup, end] OHLCV series in process memory ONCE
        # and serve every get_ohlcv_data call as an in-memory slice. The worker process stays
        # alive across the whole GA population, so this load is paid ~once per worker instead of
        # re-reading + re-parsing the disk cache on every bar (the dominant cost — ~370s of an
        # 836s 6-month profile). Used by the fill-engine preload, the expert price/data path
        # (via the seam override below) AND the clamped indicator/ATR path.
        raw_ohlcv = get_provider("ohlcv", "fmp")
        fetch_start = config["start_date"] - timedelta(days=int(config["warmup_days"]))
        # cached_only=True: a backtest is HERMETIC — serve bars from the on-disk caches only and
        # raise a clear BacktestCacheMiss (aggregated by preload) for any symbol absent from every
        # cache layout, instead of network-fetching mid-run (429 backoff -> multi-minute hang) or
        # silently skipping it. Pre-cache with `ba2-test fetch-cache` / `build-screener-metrics`.
        ohlcv = MemoizedOHLCVProvider(
            raw_ohlcv, fetch_start, config["end_date"], interval=interval, cached_only=True
        )

        ps = AsOfPriceSource(ohlcv_provider=ohlcv, interval=interval)
        ps.preload(
            config["enabled_instruments"],
            config["start_date"],
            config["end_date"],
            warmup_days=config["warmup_days"],
        )

        account = BacktestAccount(
            account_id, ps, config["account_settings"], options_provider=options_provider
        )
        resolver.register_account(account_id, account)

        experts = _build_experts(config, resolver, account_id)

        # Route the expert's OHLCV fetches (LiveProviderBundle.ohlcv / price_at_date) through the
        # memoized provider too. The expert self-clamps (it passes end_date=as_of), so the memo's
        # in-memory slice is as_of-correct without an extra wrapper.
        set_backtest_ohlcv_override(ohlcv)
        try:
            # Clamp the indicator/ATR OHLCV fetches to the backtest clock: PandasIndicatorCalc
            # and get_latest_atr fetch with end_date=now(), which would leak future bars into the
            # ATR/indicators used for sizing + rule conditions. The clamp follows ps.set_clock();
            # the inner memoized provider serves the actual bars from memory.
            indicator_provider = make_indicator_provider(
                ohlcv_provider=AsOfClampedOHLCVProvider(ohlcv, ps)
            )

            engine = DailyBacktestEngine(
                account=account,
                experts=experts,
                price_source=ps,
                config=config,
                progress_cb=progress,
                indicator_provider=indicator_provider,
            )
            engine.run()

            # build_results consumes the SAME account (get_balance_history / get_filled_trades).
            return build_results(account, config)
        finally:
            # Drop the per-run OHLCV override so it never leaks into a later (non-backtest) call.
            set_backtest_ohlcv_override(None)


def _build_experts(
    config: Dict[str, Any], resolver: Any, account_id: int
) -> List[Tuple[Any, int, Dict[str, Any], int]]:
    """Construct + register the (clean) expert instances and seed their backtest DB rows.

    Returns the ``(expert_instance, expert_instance_id, settings_dict, ruleset_id)`` tuples the
    ``DailyBacktestEngine`` iterates. Each expert:
      * gets a seeded ExpertInstance row (account_id + enter ruleset) so the inherited
        decision/RM code resolves it by id;
      * is constructed from its ba2_experts class (``__init__`` runs ``_load_expert_instance``);
      * has its automated-trading gates enabled (``allow_automated_trade_opening``/``enable_buy``)
        so the RM actually sizes + submits the pending orders;
      * is registered on the resolver so the inherited code (and the RM) finds it.

    ``settings_dict`` (fed to ``_process`` via the engine's BacktestContext) is the expert's
    declared decision settings: explicit overrides from the payload spec, else the declared
    defaults from ``get_settings_definitions`` (these are the expert's OWN defaults — not the
    host adding hidden config — so the no-defaults rule is honoured).
    """
    import importlib

    from app.services.backtest.backtest_db import seed_expert_instance
    from app.services.backtest.default_rulesets import (
        seed_enter_long_ruleset,
        seed_enter_long_short_ruleset,
        seed_open_positions_ruleset,
        seed_ruleset_from_tree,
    )

    # When the (optimizer-decoded) buy-entry condition tree is present, build the enter ruleset
    # FROM it so the optimizer's cond:<id>:value thresholds + on/off toggles actually gate
    # entries; else fall back to the default "BUY when bullish & flat" ruleset.
    buy_tree = config.get("buy_tree")
    # Optimizer-decoded exit rules (open_positions): list of {conditions, action_type,
    # reference_value, action_value, enabled} — seeded into the per-expert OPEN_POSITIONS
    # ruleset so the engine manages held positions via the real RM/evaluator (Adjust TP/SL/Close).
    exit_rules = config.get("exit_rules")
    # The initial TP/SL bracket is applied at transaction-OPEN by the engine's
    # ``_apply_initial_brackets`` (driven by ``initial_tp_percent``/``initial_sl_percent`` and the
    # canonical ``initial_tp_reference`` key) — NOT as an entry Adjust action, because at
    # enter_market time the BUY/SELL only stages a PENDING order (see ``_entry_actions``). The
    # enter ruleset therefore needs no bracket plumbing. enable_short adds the symmetric SELL/short
    # entry rule + the RM enable_sell gate.
    enable_short = bool(config.get("enable_short"))
    # Pure-option ENTRY: when set, the enter_market ruleset fires this option action directly
    # (no equity leg) — the engine submits it directly (``_entry_is_option``).
    entry_action = config.get("entry_action")

    def _seed_enter(nm: str) -> int:
        if buy_tree or entry_action:
            return seed_ruleset_from_tree(buy_tree, name=nm, enable_short=enable_short,
                                          entry_action=entry_action)
        return (seed_enter_long_short_ruleset(name=nm) if enable_short
                else seed_enter_long_ruleset(name=nm))

    def _seed_exit(nm: str) -> Optional[int]:
        return seed_open_positions_ruleset(exit_rules, name=nm) if exit_rules else None

    out: List[Tuple[Any, int, Dict[str, Any], int]] = []
    for idx, spec in enumerate(config["experts"], start=1):
        if isinstance(spec, dict):
            class_name = spec["class"]
            overrides = spec.get("settings", {}) or {}
        else:
            class_name = spec
            overrides = {}

        module_path = _SUPPORTED_EXPERTS[class_name]
        module = importlib.import_module(module_path)
        expert_cls = getattr(module, class_name)

        # BYPASS expert (piece 1b): an expert that declares ``bypasses_classic_rm`` does NOT
        # use the enter/exit ruleset or the classic RM. For it we seed NO enter ruleset and
        # enable NO RM gates — it rebalances to target weights via its own
        # FactorPortfolioManager (the engine routes its analyze_as_of targets there directly).
        bypass = bool(getattr(expert_cls, "bypasses_classic_rm", False))

        if bypass:
            ruleset_id: Optional[int] = None
            expert_id = seed_expert_instance(
                account_id=account_id,
                expert_class_name=class_name,
                # The ExpertInstance FK is non-nullable; seed a ruleset row to satisfy it even
                # though the engine never evaluates it for a bypass expert.
                enter_market_ruleset_id=seed_enter_long_ruleset(
                    name=f"backtest-bypass-{class_name}-{idx}"
                ),
                instance_id=idx,
            )
        else:
            ruleset_id = _seed_enter(f"backtest-enter-{class_name}-{idx}")
            open_ruleset_id = _seed_exit(f"backtest-open-positions-{class_name}-{idx}")
            expert_id = seed_expert_instance(
                account_id=account_id,
                expert_class_name=class_name,
                enter_market_ruleset_id=ruleset_id,
                open_positions_ruleset_id=open_ruleset_id,
                instance_id=idx,
            )

        # The expert's declared decision settings: its own defaults + payload overrides.
        decision_settings = _expert_decision_settings(expert_cls, overrides)

        expert = expert_cls(expert_id)
        if bypass:
            # No RM gates: a bypass expert never goes through TradeRiskManagement. It DOES need
            # its own universe (FactorRanker resolves it from the ``enabled_instruments``
            # setting), so seed that from the run's universe; persist the decision settings so
            # any self.settings read on the rebalance path matches the engine's _process dict.
            bypass_settings: Dict[str, Any] = {
                "enabled_instruments": (
                    {sym: {} for sym in config["enabled_instruments"]},
                    "json",
                ),
            }
            for k, v in decision_settings.items():
                bypass_settings[k] = (v, _setting_type(v))
            expert.save_settings(bypass_settings)
        else:
            # Persist the decision settings, THEN force the RM trade-permission gates so they
            # ALWAYS win. These are backtest INVARIANTS: a backtest simulates automated trading,
            # so opening/modification must be enabled and buys allowed. The interface defaults
            # (and the UI settings panel) default these to False — and decision_settings carries
            # whatever the payload sent — so applying the gates AFTER the merge is essential.
            # Applying them BEFORE (the previous order) let a form-sent
            # allow_automated_trade_opening=False overwrite the gate and silently drop EVERY
            # order -> 0 trades for any UI-started backtest. enable_sell follows enable_short.
            gate_settings: Dict[str, Any] = {}
            for k, v in decision_settings.items():
                gate_settings[k] = (v, _setting_type(v))
            gate_settings["allow_automated_trade_opening"] = (True, "bool")
            gate_settings["enable_buy"] = (True, "bool")
            # Live gates open-positions management (Adjust TP/SL/Close) on this flag.
            gate_settings["allow_automated_trade_modification"] = (True, "bool")
            # SHORT entries (the SELL enter rule) are gated by the RM on enable_sell.
            gate_settings["enable_sell"] = (bool(config.get("enable_short")), "bool")
            expert.save_settings(gate_settings)

        resolver.register_expert(expert_id, expert)
        out.append((expert, expert_id, decision_settings, ruleset_id))

    return out


def _expert_decision_settings(expert_cls: Any, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the expert's decision settings dict: declared defaults overlaid by overrides.

    Pulls each ``_SETTING_KEYS`` entry from the class's ``get_settings_definitions`` default,
    then applies the payload overrides. The defaults belong to the EXPERT (its own contract),
    not the host — fail-early if a key has neither a default nor an override.
    """
    defs = expert_cls.get_settings_definitions()
    keys = getattr(expert_cls, "_SETTING_KEYS", tuple(defs.keys()))
    settings: Dict[str, Any] = {}
    for key in keys:
        if key in overrides:
            settings[key] = overrides[key]
        elif key in defs and "default" in defs[key]:
            settings[key] = defs[key]["default"]
        else:
            raise ValueError(
                f"{expert_cls.__name__} setting '{key}' has no default and no payload override"
            )
    # Pass through any EXPLICIT overrides that aren't among the expert's own decision keys —
    # e.g. the classic-RM sizing settings (risk_per_trade_pct / atr_multiplier / min_stop_loss_pct
    # / max_virtual_equity_per_instrument_percent) optimized via model:* but read by the RM off
    # the expert (get_setting_with_interface_default), not declared on the expert itself. An
    # override is an intentional caller/optimizer instruction, so it must reach the saved settings.
    for k, v in overrides.items():
        if k not in settings:
            settings[k] = v
    return settings


def _setting_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _persist_results(db: Any, bt: Backtest, results: Dict[str, Any]) -> None:
    """Map the results metric blob onto the ``Backtest`` row's columns + JSON blobs.

    Mirrors ``handle_backtest``'s assignment block so the SAME columns + ``to_dict`` camelCase
    contract + UI consume the daily-engine output unchanged.
    """
    # Basic trade metrics
    bt.total_trades = results["total_trades"]
    bt.winning_trades = results["winning_trades"]
    bt.losing_trades = results["losing_trades"]
    bt.win_rate = results["win_rate"]

    # Return metrics
    bt.total_return = results["total_return"]
    bt.adjusted_total_return = results.get("adjusted_total_return")
    bt.annualized_return = results.get("annualized_return")
    bt.buy_hold_return = results.get("buy_hold_return")

    # Risk metrics
    bt.sharpe_ratio = results["sharpe_ratio"]
    bt.sortino_ratio = results.get("sortino_ratio")
    bt.calmar_ratio = results.get("calmar_ratio")
    bt.volatility = results.get("volatility")

    # Drawdown metrics
    bt.max_drawdown = results["max_drawdown"]
    bt.avg_drawdown = results.get("avg_drawdown")
    bt.max_drawdown_duration = results.get("max_drawdown_duration")

    # Trade quality metrics
    bt.profit_factor = results["profit_factor"]
    bt.expectancy = results.get("expectancy")
    bt.sqn = results.get("sqn")
    bt.avg_trade = results.get("avg_trade")
    bt.best_trade = results.get("best_trade")
    bt.worst_trade = results.get("worst_trade")

    # Duration metrics
    bt.avg_trade_duration = results["avg_trade_duration"]
    bt.exposure_time = results.get("exposure_time")

    # Equity metrics
    bt.final_equity = results["final_equity"]
    bt.equity_peak = results.get("equity_peak")

    # Curves + trades (JSON blobs the UI reads as equityCurve/drawdownCurve/trades).
    bt.equity_curve = results["equity_curve"]
    bt.drawdown_curve = results["drawdown_curve"]
    bt.trades = results["trades"]
    bt.results = {k: v for k, v in results.items()
                  if k not in ("equity_curve", "drawdown_curve", "trades")}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_dt(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as e:
        raise ValueError(f"payload.{field} is not a valid ISO date: {value!r} ({e})")


def _fail(db: Any, bt: Backtest, message: str) -> None:
    bt.status = "failed"
    bt.error_message = message[:1000]
    bt.completed_at = datetime.now()
    db.commit()
