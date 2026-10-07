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
    ns = ap.parse_args()

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == ns.backtest_id).first()
        if bt is None:
            print(f"backtest {ns.backtest_id} not found")
            return 1
        stored = {m: getattr(bt, m, None) for m in SUMMARY}
        stored_window = (str(bt.start_date)[:10], str(bt.end_date)[:10])
        config = rebuild_config_for_backtest(bt, db, window=ns.window)
        stored_times = ((config.get("run_schedule_override") or {}).get("times"))
        if ns.decision_time:
            from ba2_common.core.schedule_genes import retime_schedules
            config = retime_schedules(config, ns.decision_time)
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
    print(f"{'metric':<20}{'stored':>14}{'re-run':>14}")
    for m in SUMMARY:
        print(f"{m:<20}{str(stored[m]):>14}{str(got[m]):>14}")
    print(f"symbols traded (re-run): {len(symbols)}")
    if ns.out:
        Path(ns.out).write_text(json.dumps(
            {"row": ns.backtest_id, "name": name, "window": ns.window or stored_window,
             "decision_time": ns.decision_time, "stored_times": stored_times,
             "stored": stored, "stored_window": stored_window, "rerun": got,
             "preload_symbols": results.get("symbol_count")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
