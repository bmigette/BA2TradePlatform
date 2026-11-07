#!/usr/bin/env python
"""Re-run a STORED optimization-derived backtest row from the row's OWN config, WITHOUT writing
any DB row, optionally on another window.

    PYTHONPATH=<worktree packages...> .venv/Scripts/python.exe tools/rerun_stored_row.py 1107
    ... tools/rerun_stored_row.py 1107 --window 2026-01-01 2026-06-30 --out result.json

Why this and not ``_persist_top_backtests(window=...)`` / ``run_genome_once``:
``_persist_top_backtests`` decodes the RAW genome out of ``optimization.all_results``, so it
silently drops the row pins (``_atr_swap_migration`` / ``_inert_toggle_pin``) that were written onto
the stored row's ``strategy_params`` after the fact. For stored row 1107 that is the difference
between 451 trades / +271.7% (reproduces) and 651 trades / +44.8% (does not). This path rebuilds the
config through ``rerun_handler.rebuild_config_for_backtest`` -- the same reconstruction ``/rerun``
uses -- which reads the ROW's ``strategy_params`` (pins included).

No row is written: the trial runs in-memory (``persist_trading_db`` False, like a GA trial) under a
synthetic backtest id, and the result is only printed / saved to ``--out``.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()

SUMMARY = ("total_trades", "total_return", "max_drawdown", "annualized_return", "win_rate",
           "calmar_ratio", "profit_factor", "final_equity", "buy_hold_return")


def parse_window(values):
    """``--window START END`` -> ``(start, end)`` as ISO ``YYYY-MM-DD`` strings, or ``ValueError``.

    Validated up front, before any DB or engine work: a typo otherwise surfaces minutes later as an
    unrelated failure deep in the rebuild, or worse as a silently different window."""
    if values is None:
        return None
    from datetime import date
    if len(values) != 2:
        raise ValueError(f"--window takes exactly two ISO dates (START END), got {list(values)!r}")
    parsed = []
    import re
    for label, raw in zip(("START", "END"), values):
        try:
            # ONLY YYYY-MM-DD: date.fromisoformat also takes "20261005" / ISO week dates, and the
            # ORIGINAL string is what the rebuild receives, so a lenient parse could pass a form it
            # does not read.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw)):
                raise ValueError(raw)
            parsed.append(date.fromisoformat(str(raw)))
        except ValueError:
            raise ValueError(f"--window {label} {raw!r} is not an ISO date (YYYY-MM-DD)") from None
    if not parsed[0] < parsed[1]:
        raise ValueError(f"--window START must be before END, got {values[0]} >= {values[1]}")
    return values[0], values[1]


def parse_overrides(items):
    """``["k=v", ...]`` -> ``{k: v}``; v is JSON when it parses (numbers, true/false), else the raw string."""
    out = {}
    for item in items or []:
        key, sep, raw = str(item).partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set takes KEY=VALUE, got {item!r}")
        try:
            out[key.strip()] = json.loads(raw)
        except ValueError:
            out[key.strip()] = raw
    return out


def apply_expert_overrides(config, overrides):
    """Set ``overrides`` in every expert's settings of a rebuilt run config; returns the previous values."""
    previous = {}
    for spec in config.get("experts") or []:
        settings = spec.setdefault("settings", {})
        for key, value in overrides.items():
            previous.setdefault(key, settings.get(key))
            settings[key] = value
    return previous


def basis_refused_symbols(config):
    """Symbols of ``config``'s universe the intraday/daily basis preflight would refuse (mismatched or stale)."""
    import os as _os
    from app.services.backtest import intraday_basis_preflight as pf
    from ba2_providers.ohlcv import cross_interval_basis as cib
    interval = pf.interval_to_check(config)
    if interval is None:
        return []
    lo, hi = cib.judged_window(config["start_date"], config["end_date"], int(config["warmup_days"]))
    rep = pf.scan(config["enabled_instruments"], interval, lo, hi,
                  workers=max(1, min(8, (_os.cpu_count() or 2) - 1)), job_kind=pf.job_kind_of(config))
    return sorted({r.symbol for r in rep.mismatched} | {s for s, _ in rep.stale}
                  | {r.symbol for r in rep.coverage_refused})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int)
    ap.add_argument("--window", nargs=2, metavar=("START", "END"))
    ap.add_argument("--out", help="write the summary (and the stored row's figures) as JSON")
    ap.add_argument("--decision-time", metavar="HH:MM",
                    help="re-run the stored genome with its entry AND manage schedules retimed to "
                         "this exchange-local time (days untouched), e.g. 15:30. Validated like a "
                         "--decision-times value (on the bar grid, after the first bar and before "
                         "the last). Still writes no row.")
    ap.add_argument("--recompute-universe", action="store_true",
                    help="Recompute the row's STATIC UNIVERSE under the corrected superset rule "
                         "(ba2_providers.screener.universe_superset) from the optimization's declared "
                         "gene ranges, instead of keeping the frozen list stored in its config. WITHOUT "
                         "this flag a row built before the rule keeps its stored list (the numbers it "
                         "was stored with) and a WARNING states how many of the genome's own gate "
                         "selections fall outside that list.")
    ap.add_argument("--allow-first-bar", action="store_true",
                    help="MEASUREMENT ONLY: let --decision-time 09:30 (the session's first bar) run, "
                         "with the engine's first-bar warning, as a stored row without an override "
                         "does. The GA gene validation still refuses 09:30.")
    ap.add_argument("--estimates-source", choices=("fmp", "alphavantage"),
                    help="re-measure on this estimates source; default: the row's own (fmp for a row stored "
                         "before runs named one)")
    ap.add_argument("--ohlcv-history-source", choices=("fmp", "alpaca"),
                    help="re-measure on this OHLCV history; default: the row's own (fmp for a row stored "
                         "before the Alpaca switch of 2026-10-22)")
    ap.add_argument("--exclude-symbols", metavar="A,B",
                    help="remove these symbols from the row's universe for this re-run (recorded in the output), "
                         "e.g. a symbol the intraday-basis preflight refuses since the row ran")
    ap.add_argument("--exclude-basis-refused", action="store_true",
                    help="scan the universe with the intraday/daily basis preflight first and exclude every "
                         "symbol it would refuse (mismatched or stale), listing them; for re-measuring an old row "
                         "whose cache has since been found defective. The excluded list is in the output.")
    ap.add_argument("--source-db", metavar="SQLITE",
                    help="read the row, its optimization and strategy from THIS test-platform DB (e.g. an "
                         "unzipped backup) instead of the configured one; opened read-only, nothing is written")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override an EXPERT setting for this re-run (JSON value, else a string), e.g. "
                         "--set model_target_method=consensus. Repeatable. Still writes no row.")
    ns = ap.parse_args()
    try:
        ns.window = parse_window(ns.window)
        ns.set = parse_overrides(ns.set)
    except ValueError as e:
        ap.error(str(e))

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest

    if ns.source_db:
        from pathlib import Path as _P
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        src = _P(ns.source_db).resolve()
        if not src.is_file():
            ap.error(f"--source-db {src} does not exist")
        _engine = create_engine(f"sqlite:///file:{src.as_posix()}?mode=ro&uri=true")
        db = sessionmaker(bind=_engine)()
        print(f"source DB (read-only): {src}", flush=True)
    else:
        db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ns.backtest_id).first()
        if bt is None:
            print(f"backtest {ns.backtest_id} not found")
            return 1
        stored = {m: getattr(bt, m, None) for m in SUMMARY}
        stored_window = (str(bt.start_date)[:10], str(bt.end_date)[:10])
        if ns.exclude_basis_refused:
            # Judged on the FINAL config (the universe can shrink after the rebuild), repeated until clean: a
            # verdict can depend on the universe's window, so one pass is not enough.
            excluded = {x.strip().upper() for x in (ns.exclude_symbols or "").split(",") if x.strip()}
            for _ in range(4):
                probe = rebuild_config_for_backtest(bt, db, window=ns.window,
                                                    recompute_universe=ns.recompute_universe,
                                                    estimates_source=ns.estimates_source,
                                                    exclude_symbols=sorted(excluded))
                refused = set(basis_refused_symbols(probe)) - excluded
                if not refused:
                    break
                excluded |= refused
                print(f"basis preflight: excluding {len(refused)} refused symbols: {', '.join(sorted(refused))}",
                      flush=True)
            ns.exclude_symbols = ",".join(sorted(excluded))
        config = rebuild_config_for_backtest(bt, db, window=ns.window,
                                             recompute_universe=ns.recompute_universe,
                                             estimates_source=ns.estimates_source,
                                             exclude_symbols=[x.strip() for x in (ns.exclude_symbols or "").split(",")
                                                              if x.strip()])
        if ns.ohlcv_history_source:
            from ba2_common.core.ohlcv_history import OHLCV_HISTORY_SOURCE_KEY
            config[OHLCV_HISTORY_SOURCE_KEY] = ns.ohlcv_history_source
        if ns.set:
            if not config.get("experts"):
                print("--set: the rebuilt config has no experts block")
                return 1
            previous = apply_expert_overrides(config, ns.set)
        stored_times = ((config.get("run_schedule_override") or {}).get("times"))
        if ns.decision_time:
            from ba2_common.core.schedule_genes import retime_schedules
            config = retime_schedules(config, ns.decision_time, allow_first_bar=ns.allow_first_bar)
        name = bt.name
        pins = sorted(k for k in (bt.strategy_params or {}) if k.startswith("_"))
    finally:
        db.close()

    # NO row is created: synthetic id, in-memory trading store, no post-mortem file.
    config["backtest_id"] = 900_000_000 + ns.backtest_id
    config["name"] = f"H1CHECK-{name}"[:255]
    config["persist_trading_db"] = False

    print(f"row {ns.backtest_id} {name}  pins={pins}")
    print(f"window {ns.window or stored_window}  (stored row window {stored_window})", flush=True)
    print(f"decision time {ns.decision_time or stored_times}  (stored row's own: {stored_times})",
          flush=True)
    print(f"estimates source {config.get('estimates_source')}, OHLCV history "
          f"{config.get('ohlcv_history_source', 'fmp (legacy)')}", flush=True)
    if ns.set:
        print(f"expert setting overrides {ns.set}  (row's own: {previous})", flush=True)
    _note = config.get("screener_universe_legacy_note")
    if _note is not None:
        print(f"static universe: {_note['static_universe_size']} symbols, LEGACY list kept "
              f"(no --recompute-universe)", flush=True)
        if _note["outside_static_universe"]:
            print(f"WARNING: {_note['outside_static_universe']} of {_note['gate_selected']} gate selections of "
                  f"this genome fall OUTSIDE the stored static universe and can never be traded "
                  f"(first: {_note['first_examples'][:5]}); this run is NOT the corrected-universe run",
                  flush=True)
    elif config.get("screener_universe_guard"):
        print(f"static universe: {len(config['enabled_instruments'])} symbols"
              f"{' (recomputed, --recompute-universe)' if ns.recompute_universe else ''}", flush=True)
    recompute_note = config.get("screener_universe_recompute_note")
    if recompute_note:
        print(f"universe recompute: size {recompute_note['static_universe_size']} (previous "
              f"{recompute_note['previous_size']}, computed {recompute_note['computed_size']}); excluded for missing "
              f"cache files: {len(recompute_note['excluded_for_missing_cache'])} "
              f"{recompute_note['excluded_for_missing_cache']}", flush=True)

    prior = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        results = run_daily_backtest(config)
    finally:
        logging.disable(prior)

    got = {m: results.get(m) for m in SUMMARY}
    trades = results.get("trades") or []
    symbols = sorted({t.get("symbol") for t in trades if isinstance(t, dict) and t.get("symbol")})
    got["symbols_traded"] = len(symbols)
    # Telemetry the summary used to drop: how many analysis passes failed, what the intraday-clock rule
    # excluded (undecidable symbol-days / prices, skipped manage steps ...), and the decision-time record.
    for extra in ("analysis_failures", "intraday_clock", "decision_time", "screener_universe"):
        got[extra] = results.get(extra)
    print(f"{'metric':<20}{'stored':>14}{'re-run':>14}")
    for m in SUMMARY:
        print(f"{m:<20}{str(stored[m]):>14}{str(got[m]):>14}")
    print(f"symbols traded (re-run): {len(symbols)}")
    if ns.out:
        Path(ns.out).write_text(json.dumps(
            {"row": ns.backtest_id, "name": name, "window": ns.window or stored_window,
             "estimates_source": config.get("estimates_source"), "overrides": ns.set,
             "ohlcv_history_source": config.get("ohlcv_history_source"), "source_db": ns.source_db,
             "excluded_symbols": ns.exclude_symbols,
             "decision_time": ns.decision_time, "stored_times": stored_times,
             "stored": stored, "stored_window": stored_window, "rerun": got,
             "preload_symbols": results.get("symbol_count"),
             "universe_recompute": recompute_note}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
