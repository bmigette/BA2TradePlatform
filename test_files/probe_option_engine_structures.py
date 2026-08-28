#!/usr/bin/env python3
"""Option backtest engine probe — single (non-GA) backtests against the REAL cache.

Goal: prove the daily backtest engine opens/manages EVERY option structure on real data,
with timing, and report failures precisely. NO genetic optimization, NO downloads,
NO writes to the shared cache (opened read-only via ?mode=ro in the provider).

Drives the EXACT GA-trial path (the one the grid will use):
    launcher._build_strategy(kind) -> collect_param_space -> defaults ->
    decode_params -> _build_daily_trial_config(backtest_cfg, decoded) ->
    run_daily_backtest(config)

Usage (on the worker box, isolated env):
  BA2_HOME=/tmp/ba2-gridtest-home \
  PYTHONPATH=/tmp/ba2-gridtest-wt/packages/common:/tmp/ba2-gridtest-wt/packages/providers:\
/tmp/ba2-gridtest-wt/packages/experts:/tmp/ba2-gridtest-wt/testplatform/backend \
  /opt/ba2worker/ba2-venvs/test/bin/python test_files/probe_option_engine_structures.py \
      [--kinds O_STK,O_LC,...] [--symbols AAPL] [--start 2024-02-05] [--end 2024-12-31] \
      [--gates-off] [--out /tmp/ba2-gridtest-home/test/probe_results.json]

Concurrency: run at most 1-2 instances at a time (this box shares CPU/RAM with the grid).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

logging.disable(logging.INFO)  # same suppression the GA applies (dominant wall-time cost)

BACKEND = Path(__file__).resolve().parent.parent / "testplatform" / "backend"
sys.path.insert(0, str(BACKEND))


def _init_scratch_db():
    """Create the scratch test DB (under $BA2_HOME/test/) and seed FMP_API_KEY as an
    AppSetting row — the exact source FMPOHLCVProvider reads. No network: runs are hermetic
    off the on-disk parquet/fmp_history caches; the key only satisfies construction.

    AppSetting is a ba2_common SQLModel table, NOT on the backend's Base.metadata, so
    init_db()'s create_all skips it — create the shared tables from SQLModel.metadata too."""
    import os
    from app.models.database import init_db, engine, SessionLocal, DATABASE_URL
    # Point ba2_common's (separate) engine at the SAME scratch DB, exactly like
    # app.main / strategy_optimization_handler do at startup — get_app_setting reads
    # through it, and a direct run_daily_backtest call never reaches those startup hooks.
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(DATABASE_URL.replace("sqlite:///", "", 1))
    init_db()
    import ba2_common.core.models  # noqa: F401 — register the shared SQLModel tables
    from sqlmodel import SQLModel
    SQLModel.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        from ba2_common.core.models import AppSetting
        existing = db.query(AppSetting).filter(AppSetting.key == "FMP_API_KEY").first()
        key = os.environ.get("FMP_API_KEY")
        if not key:
            raise SystemExit("FMP_API_KEY missing from env — cannot seed the app settings")
        if existing is None:
            db.add(AppSetting(key="FMP_API_KEY", value_str=key))
            db.commit()
    finally:
        db.close()


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "ba2test_launcher", str(Path(__file__).resolve().parent.parent / "testplatform" / "ba2test_launcher.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["ba2test_launcher"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


ALL_KINDS = [
    "O_STK", "O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS", "O_BEARCS", "O_BULLPS",
    "O_CSP", "O_IC", "O_JL", "O_RS", "O_SSTD", "O_SSTG", "O_STRD", "O_STRG",
    "O_CC", "O_PP", "O_WHEEL",
]

EXPERT = "FMPRating"


def build_trial_config(m, kind: str, symbols, start, end, capital, seed, gates_off,
                       ride_to_expiry=False):
    """Replicate _cmd_optimize's trial path without any GA / DB / queue."""
    from app.services.strategy_param_space import decode_params
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    # --gates-off semantics: the module toggle is set at COMMAND ENTRY, before the strategy
    # is built (the entry rule reads it at build time). Mirror that exactly, then restore.
    prior_gates = getattr(m, "_OPTION_GATES_OFF", False)
    m._OPTION_GATES_OFF = bool(gates_off)
    try:
        strat = m._build_strategy(kind, f"probe-{kind}", EXPERT)
    finally:
        m._OPTION_GATES_OFF = prior_gates
    entry_action = getattr(strat, "entry_action", None)

    # The AUTHORED genome: decode_params with an empty flat dict keeps every authored
    # value/toggle as declared (cond values from the template, toggles default-enabled,
    # action genes at authored params). That is exactly the seed individual every grid
    # warm-start begins from.
    decoded = decode_params(strat, {})

    # RIDE-TO-EXPIRY mode (wheel mechanics probe): drop every option TIME/profit exit rule so
    # an entry is only ever closed by NATURAL EXPIRY — the only path that produces assignment,
    # which is what the wheel's second leg needs. Not a strategy recommendation; a microscope.
    if ride_to_expiry and decoded.get("exit_rules") is not None:
        decoded["exit_rules"] = [
            r for r in decoded["exit_rules"]
            if not any(str(a.get("action_type")) == "close_option"
                       for a in (r.get("actions") or []))
        ]

    spec = m._EXPERT_OPT[EXPERT]
    account_settings = {
        "starting_cash": float(capital),
        "commission_per_trade": 1.0,
        "slippage_bps": 0.0,
        "spread_bps": 0.0,
        "option_spread_pct": 0.0,
        "option_spread_min_tick": 0.0,
        "fill_model": "next_bar_open",
        "equity_cap": None,
        # Mirror the launcher's optimize path (ba2test_launcher._hold_assigned_stock): the
        # wheel's run holds assigned stock instead of liquidating it next bar.
        "hold_assigned_stock": bool(m._hold_assigned_stock(kind))
        if hasattr(m, "_hold_assigned_stock") else False,
    }
    backtest_cfg = {
        "engine": "daily",
        "enabled_instruments": list(symbols),
        "experts": [{"class": EXPERT,
                     "settings": m._expert_run_settings(spec, list(symbols), {})}],
        "start_date": start,
        "end_date": end,
        "initial_capital": float(capital),
        "account_settings": account_settings,
        "warmup_days": 90,
        "seed": int(seed),
        "subtype": "daily_expert",
        "run_schedule_override": None,   # analyse every bar (simplest, densest signal)
        "manage_schedule_override": m._daily_manage_schedule(),
        "execution_interval": "1d",
        "profit_cap_pct": None,
        "profit_share_cap_pct": None,
        "stress_spread_bps": 0.0,
        "robust_fitness": False,
        "backtest_id": int(datetime.now().timestamp()),
        "name": f"probe-{kind}",
        "entry_action": entry_action,
    }
    m._apply_options_seam(spec, backtest_cfg)
    cfg = _build_daily_trial_config(backtest_cfg, decoded, None)
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--kinds", default="O_STK,O_LC")
    p.add_argument("--symbols", default="AAPL")
    p.add_argument("--start", default="2024-02-05")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--capital", type=float, default=20_000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--gates-off", action="store_true")
    p.add_argument("--ride-to-expiry", action="store_true",
                   help="Drop option close exits so entries ride to natural expiry (wheel probe).")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    _init_scratch_db()
    m = _launcher()
    results = []
    for kind in kinds:
        t0 = time.perf_counter()
        row = {"kind": kind, "symbols": symbols, "start": args.start, "end": args.end,
               "gates_off": bool(args.gates_off)}
        try:
            cfg = build_trial_config(m, kind, symbols, args.start, args.end,
                                     args.capital, args.seed, args.gates_off,
                                     args.ride_to_expiry)
            from app.services.backtest.daily_backtest_handler import run_daily_backtest
            res = run_daily_backtest(cfg)
            row.update({
                "status": "ok",
                "total_trades": res.get("total_trades"),
                "total_return": res.get("total_return"),
                "annualized_return": res.get("annualized_return"),
                "max_drawdown": res.get("max_drawdown"),
                "final_equity": res.get("final_equity"),
                "win_rate": res.get("win_rate"),
                "option_trades": sum(
                    1 for t in (res.get("trades") or [])
                    if "C0" in str(t.get("symbol", "")) or "P0" in str(t.get("symbol", ""))
                    or len(str(t.get("symbol", ""))) > 20),
            })
        except Exception as e:  # noqa: BLE001 — probe: report, keep going
            row.update({"status": "error", "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc(limit=6)})
        row["wall_seconds"] = round(time.perf_counter() - t0, 1)
        results.append(row)
        print(f"[{row['status']:5s}] {kind:8s} trades={row.get('total_trades')!s:>5} "
              f"ret={row.get('total_return')!s:>10} wall={row['wall_seconds']}s", flush=True)
        if row["status"] == "error":
            print(f"        {row['error']}", flush=True)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"\nwrote {args.out}")
    failed = [r["kind"] for r in results if r["status"] == "error"]
    traded = [r["kind"] for r in results if (r.get("total_trades") or 0) > 0]
    print(f"\nSUMMARY: {len(results)} runs | {len(failed)} errors {failed} | "
          f"{len(traded)} with trades {traded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
