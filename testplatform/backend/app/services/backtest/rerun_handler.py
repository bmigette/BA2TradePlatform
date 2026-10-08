"""Re-run a saved daily-expert backtest IN PLACE (overwrite the same row's results).

A saved ``Backtest`` row's stored metrics can go stale when the underlying data (e.g. a rebuilt
screener metric_store) or the engine code changes. This handler re-executes the run with its
ORIGINAL config against the CURRENT data/code and writes the fresh results back onto the SAME row
(same id / name / optimization link) — no new row.

Two row shapes (both ``engine_type='daily_expert'``):
  * OPTIMIZATION-DERIVED (``optimization_id`` set, e.g. ``TOP3-scr-mid-FactorRanker``): the
    re-runnable config is rebuilt EXACTLY as ``ba2test_launcher._persist_top_backtests`` does —
    ``decode_params(strategy, genes)`` -> ``_build_daily_trial_config(opt.optimization_config
    ['backtest'], decoded)`` — so the re-run reproduces how that top individual was persisted.
  * STANDALONE (no ``optimization_id``): the daily payload is rebuilt from ``strategy_params``
    (universe / expertSettings / trees / tp-sl) + the row columns and run through the normal
    ``_build_config`` path. ``seed`` / ``fill_model`` / ``warmup_days`` / ``run_schedule_override``
    are read from ``strategy_params`` when present (persisted on creation going forward), else fall
    back to documented defaults for legacy rows.

Runs on a DEDICATED task queue (``rerun_backtest`` type) so re-runs never consume the main queue's
worker slots and don't starve running optimizations.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from app.models.backtest import Backtest
from app.models.database import SessionLocal
from app.services.task_queue import get_task_queue
from app.services.backtest.daily_backtest_handler import (
    _Paused,
    _build_config,
    _fail,
    _persist_results,
    record_option_spread_model,
    run_daily_backtest,
)

import logging

from ba2_providers.screener.universe_superset import RULE_ID as _SUPERSET_RULE_ID

logger = logging.getLogger(__name__)

# Gene namespaces decode_params accepts (it RAISES on anything else). The stored strategy_params
# mixes these raw genes with camelCase display keys (buyEntryConditions, ...), so filter first.
#
# "schedule" WAS MISSING HERE until 2026-09-07, and its absence was silent. The GA searches the
# entry weekday per individual (schedule:<day> genes, _SCHEDULE_DAY_OPT in the launcher) and
# _build_daily_trial_config lets a decoded ``schedule_days`` REPLACE the run-level cadence. Drop
# the genes on the way in and ``decoded["schedule_days"]`` is None, so every re-run silently fell
# back to the run-level override -- Monday-only for the whole goal2020 grid -- while the genome
# that was actually scored wanted, say, Tue/Thu/Fri. Nothing errored: the run completed and
# reported numbers for a cadence the GA never chose.
#
# This filter is the ONLY thing standing between a stored genome and decode_params, so a gene
# namespace added to the search must be added here in the same change. Pinned by
# testplatform/backend/tests/backtest/test_rerun_carries_schedule_genes.py.
#
# "market" (atr_grid_2027 market master-gene addendum): the ONE ``market:enabled`` master gene.
# Missing here it would be silently stripped before decode_params ever saw it, so a re-run of a
# market-gated TOP-N row would decode as if the gene were simply absent (every individual
# cond:*:mode / exit:*:enabled gene honoured unchanged) however the master gene actually
# resolved when the GA scored it -- exactly the "gene namespace added to the search must be
# added here" trap this comment already warns about.
_GENE_PREFIXES = ("model", "screener", "cond", "exit", "entry", "schedule", "market")

# Legacy-row fallbacks: standalone rows created before the run knobs were persisted don't carry a
# seed / fill model. The re-run uses these so it can still execute (may differ slightly from the
# original for those old rows; opt-derived rows are exact, their knobs come from the opt config).
_DEFAULT_FILL_MODEL = "next_bar_open"
_DEFAULT_SEED = 42


def _gene_params(strategy_params: Dict[str, Any]) -> Dict[str, Any]:
    """The optimizer GENE subset of a stored strategy_params (drops camelCase display keys so
    decode_params doesn't raise on them)."""
    out: Dict[str, Any] = {}
    for k, v in (strategy_params or {}).items():
        if k in ("tp", "sl") or k.split(":", 1)[0] in _GENE_PREFIXES:
            out[k] = v
    return out


def recompute_static_universe(bt_block: Dict[str, Any], parameter_ranges: Any, *, row_id: Any = None
                              ) -> Dict[str, Any]:
    """A COPY of ``bt_block`` whose ``enabled_instruments`` is recomputed under the superset rule
    (``ba2_providers.screener.universe_superset``) from the optimization's DECLARED gene ranges
    (``parameter_ranges``, ``screener:<gene>`` keys), the stored base settings, the block's window and its
    recorded exclusions, then stamped ``screener_universe_rule``. Refuses (``ScreenerUniverseError``)
    when the ranges are absent or a gene has no role: it never guesses a loosest value.

    Symbols with no cached OHLCV for the execution interval (or daily) are EXCLUDED from the screen,
    recorded in ``excluded_instruments`` and logged as a WARNING naming them (the launch default is to
    refuse; a re-run is a measurement and must still run, loudly)."""
    import os
    from ba2_common.config import CACHE_FOLDER
    from ba2_providers.screener import metric_store as ms
    from ba2_providers.screener import universe_superset as us

    ranges = us.gene_ranges_from_parameter_ranges(parameter_ranges if isinstance(parameter_ranges, dict) else {})
    if not ranges:
        raise us.ScreenerUniverseError(
            f"--recompute-universe: optimization of row {row_id} stores no screener gene ranges "
            f"(parameter_ranges has no 'screener:*' keys): the loosest values cannot be derived")
    so = bt_block["screener_opt"]
    interval = bt_block["execution_interval"]
    excluded = list(bt_block.get("excluded_instruments") or [])
    if so.get("criteria_version"):
        # a row of the live-simulation gate: its static universe is computed by the SAME function the launch used
        from app.services.backtest import screener_gate as sg
        from ba2_providers.screener import live_sim as _ls
        new = sg.static_universe(sg.get_panel(_ls.resolve_panel_path(so["panel"]), so["panel_fingerprint"]), str(bt_block["start_date"])[:10],
                                 str(bt_block["end_date"])[:10], so["base_settings"], ranges,
                                 intraday=us.interval_is_intraday(interval), excluded_symbols=excluded)
    else:
        df = ms.load_store(so["store"])
        new = us.static_universe(df, str(bt_block["start_date"])[:10], str(bt_block["end_date"])[:10],
                                 so["base_settings"], ranges, intraday=us.interval_is_intraday(interval),
                                 excluded_symbols=excluded)
    cdir = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
    ivs = sorted({interval, "1d"})

    def _cached(sym: str) -> bool:
        return all(any(os.path.exists(os.path.join(cdir, f"{c}_{iv}.parquet"))
                       for c in (sym, sym.replace("-", "_"), sym.replace("-", ".")))
                   for iv in ivs)

    uncached = [x for x in new if not _cached(x)]
    gone = set(uncached) | {x.upper() for x in excluded}
    out = dict(bt_block)
    out["enabled_instruments"] = [x for x in new if x not in gone and x.upper() not in gone]
    if uncached:
        out["excluded_instruments"] = excluded + [x for x in uncached if x.upper() not in {e.upper() for e in excluded}]
        logger.warning(f"--recompute-universe row {row_id}: EXCLUDING {len(uncached)} symbols with no cached "
                       f"{'/'.join(ivs)} OHLCV from the screen: {', '.join(uncached)}")
    out["screener_universe_rule"] = us.RULE_ID
    old_n = len(bt_block.get("enabled_instruments") or [])
    # NEVER silent: an exclusion must not shrink a measurement's universe without being on the record (printed by
    # the re-run tools AND written to their output JSON from ``screener_universe_recompute_note``)
    out["_universe_recompute_note"] = {
        "static_universe_size": len(out["enabled_instruments"]), "previous_size": old_n,
        "computed_size": len(new), "excluded_for_missing_cache": list(uncached),
        "criteria_version": so.get("criteria_version")}
    logger.warning(f"--recompute-universe row {row_id}: static universe {old_n} -> "
                   f"{len(out['enabled_instruments'])} symbols (rule {us.RULE_ID}; computed {len(new)}, "
                   f"{len(uncached)} excluded for missing cache files)")
    return out


def legacy_universe_note(bt_block: Dict[str, Any], hoisted: Dict[str, Any], decoded: Dict[str, Any], *,
                         row_id: Any = None) -> Dict[str, Any]:
    """For a stored block from BEFORE the superset rule that KEEPS its frozen list: how many of the
    genome's own gate selections (over the block's window) fall outside that list. Logged as a WARNING and
    returned (``{"gate_selected", "outside_static_universe", "first_examples", "static_universe_size"}``)
    so the re-run tools can print it. Pure read of the store."""
    from ba2_providers.screener import metric_store as ms
    from ba2_providers.screener import universe_superset as us

    eff = ms.normalize_screener_settings({**(hoisted.get("screener_base") or {}),
                                          **(decoded.get("screener_overrides") or {})})
    df = ms.load_store(hoisted["screener_store"])
    sel = us.gate_selections(df, str(bt_block["start_date"])[:10], str(bt_block["end_date"])[:10], eff,
                             list(bt_block.get("excluded_instruments") or []),
                             intraday=us.interval_is_intraday(bt_block["execution_interval"]))
    note = us.count_outside(sel, bt_block["enabled_instruments"])
    note["static_universe_size"] = len(bt_block["enabled_instruments"])
    if note["outside_static_universe"]:
        share = 100.0 * note["outside_static_universe"] / max(1, note["gate_selected"])
        logger.warning(
            f"row {row_id}: LEGACY static universe ({note['static_universe_size']} symbols, built before the "
            f"{us.RULE_ID} rule) is KEPT: {note['outside_static_universe']} of {note['gate_selected']} of the "
            f"genome's own gate selections ({share:.1f}%) fall outside it and can never be traded. "
            f"Re-run with --recompute-universe for the corrected universe. First: {note['first_examples'][:5]}")
    return note


def _build_optimization_rerun_config(db: Any, bt: Backtest, window: Any = None,
                                    recompute_universe: bool = False) -> Dict[str, Any]:
    """Rebuild an opt-derived row's run config.

    Builds the SAME config the GA SCORED the individual with: ``_build_daily_trial_config`` fed the
    ``hoisted`` state so the per-individual screener genes are applied (universe_source=screener +
    the optimized thresholds), matching the UI "Load + run". (Note: the CLI ``_persist_top_backtests``
    historically built this WITHOUT hoisted, persisting the top-N as static-universe runs — so a
    re-run intentionally CORRECTS that and re-runs the actual optimized screener config.)

    STATIC UNIVERSE (screener jobs). The stored block freezes ``enabled_instruments``. A block stamped
    ``screener_universe_rule == superset-v1`` holds the true superset and is used as stored. A block from
    BEFORE the rule holds the old cap-ranked top-50 list: by default that list is KEPT (the row reproduces
    the numbers it was stored with) and a WARNING states how many of the genome's own gate selections fall
    outside it (they were untradable); ``recompute_universe=True`` (``--recompute-universe`` of the re-run
    tools) recomputes the static universe under the corrected rule from the optimization's declared gene
    ranges (``parameter_ranges``) and the stored base settings, window and exclusions, and stamps the rule.
    """
    import os
    from ba2_common.config import SCREENER_STORE_DIR
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config,
        _build_hoisted_state,
    )
    from app.services.strategy_param_space import decode_params

    opt = db.query(StrategyOptimization).filter(
        StrategyOptimization.id == bt.optimization_id
    ).first()
    if opt is None or not opt.optimization_config:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: its optimization #{bt.optimization_id} or its "
            f"saved optimization_config is missing"
        )
    cfg = opt.optimization_config
    if "backtest" not in cfg:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: optimization #{opt.id} has no 'backtest' block"
        )
    strat = db.query(Strategy).filter(Strategy.id == opt.strategy_id).first()
    bt_block = dict(cfg["backtest"])
    # The row's OWN window wins over the optimization's: a walk-forward out-of-sample row
    # (``WF<k>-OOS-R<n>-...``, tools/run_walk_forward.py) shares the optimization_id and genes but
    # ran on the test window, and re-running it on the train window would overwrite it with
    # in-sample numbers. For an ordinary TOP-N row the two windows are equal, so nothing changes.
    if bt.start_date is not None and bt.end_date is not None:
        _rs, _re = bt.start_date.date().isoformat(), bt.end_date.date().isoformat()
        if (str(bt_block["start_date"])[:10], str(bt_block["end_date"])[:10]) != (_rs, _re):
            bt_block["start_date"], bt_block["end_date"] = _rs, _re
    # An explicit ``window`` (start, end ISO dates) REPLACES both: re-measure THIS stored row's own
    # genome (pins included) on another period. The screener hoisted state below is derived from
    # bt_block, so it follows the overridden window.
    if window is not None:
        bt_block["start_date"], bt_block["end_date"] = str(window[0]), str(window[1])

    # The optimization may have run on another machine (e.g. a Windows store path) or the store may
    # have moved — remap a missing screener store to this machine's local SCREENER_STORE_DIR so the
    # re-run reads the current metric_store (and picks up any rebuild, e.g. the price_drop fix).
    so = bt_block.get("screener_opt")
    if isinstance(so, dict) and so.get("store") and not os.path.isdir(so["store"]):
        bt_block["screener_opt"] = {**so, "store": SCREENER_STORE_DIR}
        so = bt_block["screener_opt"]

    if recompute_universe:
        if not bt_block.get("screener_opt"):
            raise ValueError(f"--recompute-universe: backtest {bt.id} is not a screener-based row "
                             f"(optimization #{opt.id} has no screener_opt block)")
        bt_block = recompute_static_universe(bt_block, opt.parameter_ranges, row_id=bt.id)

    decoded = decode_params(strat, _gene_params(bt.strategy_params))
    # hoisted applies the screener (genes + store) exactly as the GA did; None for non-screener opts.
    hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
    trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted,
                                          option_trade_records=True,
                                          stored_row=True)  # a STORED block: an absent time = the legacy 09:30
    # Overwrite the SAME row; persist the trial sub-DB for post-mortem (matches _persist_top_backtests).
    trial_cfg["backtest_id"] = bt.id
    trial_cfg["name"] = bt.name
    trial_cfg["persist_trading_db"] = True
    if bt_block.get("_universe_recompute_note"):
        trial_cfg["screener_universe_recompute_note"] = bt_block["_universe_recompute_note"]
    if hoisted is not None and bt_block.get("screener_universe_rule") != _SUPERSET_RULE_ID:
        trial_cfg["screener_universe_legacy_note"] = legacy_universe_note(
            bt_block, hoisted, decoded, row_id=bt.id)
    return trial_cfg


def _build_standalone_rerun_config(bt: Backtest) -> Dict[str, Any]:
    """Rebuild a standalone daily_expert row's run config from its persisted strategy_params."""
    sp = bt.strategy_params or {}
    if not bt.expert_name:
        raise ValueError(f"cannot re-run backtest {bt.id}: no expert_name on the row")

    universe = sp.get("universe")
    if not universe:
        raise ValueError(
            f"cannot re-run backtest {bt.id}: no universe persisted on the row (created before "
            f"re-run support); re-create the backtest instead"
        )

    payload: Dict[str, Any] = {
        "backtest_id": bt.id,
        "name": bt.name,
        "experts": [{"class": bt.expert_name, "settings": sp.get("expertSettings") or {}}],
        "start_date": bt.start_date.isoformat() if hasattr(bt.start_date, "isoformat") else str(bt.start_date),
        "end_date": bt.end_date.isoformat() if hasattr(bt.end_date, "isoformat") else str(bt.end_date),
        "initial_capital": float(bt.initial_capital),
        "commission": float(bt.commission),
        "slippage": float(bt.slippage),
        "fill_model": sp.get("fillModel") or _DEFAULT_FILL_MODEL,
        "seed": sp.get("seed") if sp.get("seed") is not None else _DEFAULT_SEED,
        "warmup_days": sp.get("warmupDays"),
        "execution_interval": sp.get("executionInterval") or "1d",
    }
    if sp.get("runScheduleOverride") is not None:
        payload["run_schedule_override"] = sp["runScheduleOverride"]
    if sp.get("manageScheduleOverride") is not None:
        payload["manage_schedule_override"] = sp["manageScheduleOverride"]
    # The option spread model the row recorded (plan Part F). Absent on a row created before
    # Part F: _build_config then applies the current model explicitly and the re-run records it.
    if sp.get("optionSpreadModel") is not None:
        payload["option_spread_model"] = sp["optionSpreadModel"]
    # Universe: static -> explicit symbols; screener -> the metric_store block.
    if universe.get("mode") == "screener":
        payload["universe"] = universe
    else:
        payload["enabled_instruments"] = list(universe.get("symbols") or [])
    # Strategy trees + the entry-time TP/SL bracket (entry_rules).
    if sp.get("buyEntryConditions") is not None:
        payload["buy_tree"] = sp["buyEntryConditions"]
    if sp.get("sellEntryConditions") is not None:
        payload["sell_tree"] = sp["sellEntryConditions"]
    if sp.get("enableShort") is not None:
        payload["enable_short"] = bool(sp["enableShort"])
    if sp.get("exitConditions") is not None:
        payload["exit_rules"] = sp["exitConditions"]
    if sp.get("entryActions") is not None:
        payload["entry_rules"] = sp["entryActions"]
    return _build_config(payload)


def rebuild_config_for_backtest(bt: Backtest, db: Any = None, window: Any = None,
                              recompute_universe: bool = False) -> Dict[str, Any]:
    """Reconstruct the ``run_daily_backtest`` config that reproduces ``bt``'s ORIGINAL run.

    This is the SINGLE reconstruction path shared by BOTH the ``/rerun`` handler (which
    re-executes a row in place) AND the robustness schedule-variant launcher (which clones this
    config and overrides ``run_schedule_override`` per variant). Do NOT re-implement the
    reconstruction elsewhere — call this.

    ``db`` is only needed for OPTIMIZATION-derived rows (``optimization_id`` set), whose config is
    rebuilt from the parent ``StrategyOptimization`` row. Standalone rows reconstruct purely from
    the row's own ``strategy_params`` and need no session; when ``db`` is None here a short-lived
    ``SessionLocal`` is opened only if the row turns out to be optimization-derived.
    """
    if bt.engine_type != "daily_expert":
        raise ValueError(
            f"re-run is only supported for daily_expert backtests (backtest {bt.id} is "
            f"'{bt.engine_type}')"
        )
    if window is not None and not bt.optimization_id:
        raise ValueError("window override is only supported for optimization-derived rows")
    if recompute_universe and not bt.optimization_id:
        raise ValueError("--recompute-universe is only supported for optimization-derived rows")
    if bt.optimization_id:
        if db is not None:
            return _build_optimization_rerun_config(db, bt, window, recompute_universe)
        session = SessionLocal()
        try:
            return _build_optimization_rerun_config(session, bt, window, recompute_universe)
        finally:
            session.close()
    return _build_standalone_rerun_config(bt)


def build_rerun_config(db: Any, bt: Backtest) -> Dict[str, Any]:
    """Return a ``run_daily_backtest`` config that reproduces ``bt``'s run, targeting its own id.

    Thin back-compat wrapper over the shared ``rebuild_config_for_backtest`` (the reconstruction
    used to live here inline; it was extracted so the robustness launcher can reuse it verbatim)."""
    return rebuild_config_for_backtest(bt, db)


def handle_rerun_backtest(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Task handler: re-run the saved backtest ``payload['backtest_id']`` in place."""
    backtest_id = payload.get("backtest_id")
    if backtest_id is None:
        return {"status": "failed", "error": "payload.backtest_id is required"}

    tq = get_task_queue()
    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == backtest_id).first()
        if bt is None:
            return {"status": "failed", "error": f"Backtest {backtest_id} not found"}

        bt.status = "running"
        bt.started_at = datetime.now()
        bt.error_message = None
        db.commit()

        try:
            config = build_rerun_config(db, bt)
        except (KeyError, ValueError) as e:
            _fail(db, bt, str(e))
            return {"status": "failed", "error": str(e)}
        record_option_spread_model(db, bt, config)

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
            f"Re-run backtest {backtest_id} completed: {results.get('total_trades', 0)} trades, "
            f"return={results.get('total_return')}%"
        )
        return {"status": "completed", "backtest_id": backtest_id, "results": results}

    except _Paused as e:
        _fail(db, bt, f"paused: {e}")
        return {"status": "failed", "error": "paused"}
    except Exception as e:  # noqa: BLE001 — any failure fails the row, not the worker
        logger.error(f"Re-run backtest {backtest_id} failed: {e}", exc_info=True)
        try:
            row = db.query(Backtest).filter(Backtest.id == backtest_id).first()
            if row is not None:
                _fail(db, row, str(e))
        except Exception:  # noqa: BLE001
            pass
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
