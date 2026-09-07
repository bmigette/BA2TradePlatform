#!/usr/bin/env python
"""Prove a migrated ATR genome still reproduces the metrics recorded before the fix.

    .venv/Scripts/python.exe tools/verify_atr_migration_reproduces.py 1107

THE CLAIM UNDER TEST. Commit dd1f912e wired ``atr_risk_budget_pct`` to the stop distance and
``risk_per_trade_pct`` to the sizing -- each gene doing the other's job. c4e2f432 corrected the
wiring, which would have changed what every stored genome MEANS, so
tools/migrate_atr_budget_swap.py swapped the two values in every affected row. The premise is
that a swapped genome on corrected code behaves exactly as the original genome did on the
broken code -- i.e. the recorded metrics stay reproducible.

That premise has been argued, not measured. This measures it: rebuild the row's config, run it,
and compare against the metrics stored on the row (which were produced pre-fix).

Pick a MONDAY-ONLY genome (e.g. 1107) so the concurrent schedule-gene fix cannot move the
result: any difference is then attributable to the ATR swap alone. The source row is never
touched -- the re-run goes to a new row.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("backtest_id", type=int)
    ns = ap.parse_args()

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import _persist_results, run_daily_backtest
    from app.services.backtest.rerun_handler import build_rerun_config

    db = SessionLocal()
    try:
        parent = db.query(Backtest).filter(Backtest.id == ns.backtest_id).first()
        if parent is None:
            print(f"backtest {ns.backtest_id} not found")
            return 1
        sp = parent.strategy_params or {}
        if not sp.get("_atr_swap_migration"):
            print(f"backtest {ns.backtest_id} carries no _atr_swap_migration marker -- this "
                  f"tool only means something for a MIGRATED row")
            return 1
        before = {m: getattr(parent, m, None) for m in METRICS}
        print(f"{parent.name}")
        print(f"  migrated from: {sp['_atr_swap_migration'].get('from')}")
        print(f"  recorded (pre-fix): {before}\n")

        config = copy.deepcopy(build_rerun_config(db, parent))
        child = Backtest(
            name=f"VERIFY-ATR-{parent.name}"[:255],
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
        results = run_daily_backtest(config)
        _persist_results(db, child, results)
        child.status = "completed"
        db.commit()

        after = {m: results.get(m) for m in METRICS}
        print(f"\n  {'metric':<20}{'recorded':>14}{'re-run':>14}   verdict")
        ok = True
        for m in METRICS:
            a, b = before[m], after[m]
            if a is None and b is None:
                continue
            try:
                same = abs(float(a) - float(b)) <= max(0.01, abs(float(a)) * 0.001)
            except (TypeError, ValueError):
                same = a == b
            ok &= same
            print(f"  {m:<20}{str(a):>14}{str(b):>14}   {'match' if same else 'DIFFERS'}")
        print(f"\n  {'REPRODUCES' if ok else 'DOES NOT REPRODUCE'} "
              f"(row {child.id} kept for inspection)")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
