#!/usr/bin/env python
"""Re-run saved backtests and check they still reproduce the metrics recorded on their rows.

    .venv/Scripts/python.exe tools/verify_atr_migration_reproduces.py 1107 1088 1173

WHAT IS BEING CHECKED. Two changes landed that alter what a STORED genome means, and both were
argued rather than measured:

1. The ATR gene swap. dd1f912e wired ``atr_risk_budget_pct`` to the stop distance and
   ``risk_per_trade_pct`` to the sizing -- each doing the other's job. c4e2f432 corrected the
   wiring, so tools/migrate_atr_budget_swap.py swapped the two values in every affected row. The
   premise is that a swapped genome on corrected code behaves as the original genome did on the
   broken code.

2. The inert toggles. ``use_atr_stop`` and ``regime_overlay_enabled`` never took effect (a bool
   gene arriving as int 1 was stored as the string "1" and read as False). coerce_bool fixed the
   encoding, so a stored ``model:use_atr_stop: 1`` would now switch the feature ON when decoded
   -- which is why INERT_RM_TOGGLES pins them off above the decoded genes.

Either could silently change what a saved row reproduces. This measures it: rebuild the row's
config, run it, and compare against the metrics stored on the row.

Prefer MONDAY-ONLY genomes so the concurrent schedule-gene fix cannot move the result and any
difference is attributable to the two changes above. Source rows are never touched -- each
re-run goes to a new row, labelled ``atr-migration-verify``.
"""
import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()

METRICS = ("total_return", "total_trades", "win_rate", "max_drawdown",
           "annualized_return", "calmar_ratio", "profit_factor")


def _same(a, b) -> bool:
    """Equal within 0.1%, which absorbs float formatting without hiding a real divergence."""
    if a is None and b is None:
        return True
    try:
        return abs(float(a) - float(b)) <= max(0.01, abs(float(a)) * 0.001)
    except (TypeError, ValueError):
        return a == b


def verify_one(db, bid: int) -> bool:
    from app.models.backtest import Backtest
    from app.services.backtest.daily_backtest_handler import _persist_results, run_daily_backtest
    from app.services.backtest.rerun_handler import build_rerun_config

    parent = db.query(Backtest).filter(Backtest.id == bid).first()
    if parent is None:
        print(f"  !! backtest {bid} not found")
        return False
    sp = parent.strategy_params or {}
    before = {m: getattr(parent, m, None) for m in METRICS}
    mig = sp.get("_atr_swap_migration")

    print(f"\n=== {bid}  {parent.name}")
    print(f"  atr swap migrated  : {mig.get('from') if mig else 'no (row not affected)'}")
    print(f"  genome toggles     : use_atr_stop={sp.get('model:use_atr_stop')} "
          f"regime_overlay_enabled={sp.get('model:regime_overlay_enabled')}")

    config = copy.deepcopy(build_rerun_config(db, parent))
    child = Backtest(
        name=f"VERIFY-{parent.name}"[:255],
        engine_type="daily_expert", expert_name=parent.expert_name,
        optimization_id=None, strategy_id=parent.strategy_id,
        strategy_params=dict(sp),
        start_date=parent.start_date, end_date=parent.end_date,
        initial_capital=parent.initial_capital,
        position_sizing_type=parent.position_sizing_type,
        position_sizing_value=parent.position_sizing_value,
        commission=parent.commission, slippage=parent.slippage,
        fitness_metric=parent.fitness_metric, status="running", is_saved=False,
    )
    child.labels = ["atr-migration-verify"]
    db.add(child)
    db.commit()
    db.refresh(child)
    config["backtest_id"] = child.id
    config["name"] = child.name

    print(f"  re-running as row {child.id} ...", flush=True)
    try:
        results = run_daily_backtest(config)
    except Exception as e:  # noqa: BLE001 - report and carry on to the next row
        child.status = "failed"
        child.error_message = str(e)[:900]
        db.commit()
        print(f"  FAILED: {e}")
        return False
    _persist_results(db, child, results)
    child.status = "completed"
    db.commit()

    after = {m: results.get(m) for m in METRICS}
    print(f"  {'metric':<22}{'recorded':>14}{'re-run':>14}   verdict")
    ok = True
    for m in METRICS:
        a, b = before[m], after[m]
        if a is None and b is None:
            continue
        good = _same(a, b)
        ok &= good
        print(f"  {m:<22}{str(a):>14}{str(b):>14}   {'match' if good else 'DIFFERS'}")
    print(f"  --> {'REPRODUCES' if ok else 'DOES NOT REPRODUCE'} (row {child.id} kept)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int, nargs="+")
    ns = ap.parse_args()

    from app.models.database import SessionLocal

    db = SessionLocal()
    results = {}
    try:
        for bid in ns.backtest_id:
            results[bid] = verify_one(db, bid)
    finally:
        db.close()

    print("\n=== summary ===")
    for bid, ok in results.items():
        print(f"  {bid}: {'REPRODUCES' if ok else 'DOES NOT REPRODUCE'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
