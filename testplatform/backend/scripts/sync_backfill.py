#!/usr/bin/env python3
"""Manual, one-off backfill: push pre-existing saved backtests and terminal optimizations to
every synced worker.

Sync (Tasks 5-8 of docs/plans/2026-07-07-remote-result-sync-implementation.md) only fires for
NEW rows going forward. Rows that already existed before this feature shipped are never
backfilled automatically (see the design doc's "Migration and Rollout" section) — run this
script once, on demand, if you want history replicated too.

Usage:
    ./venv/bin/python scripts/sync_backfill.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sync_client import push_optimization, push_backtest  # noqa: E402 (see sys.path.insert above)


def run(db) -> None:
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest

    terminal_opts = (
        db.query(StrategyOptimization)
        .filter(StrategyOptimization.status.in_(["completed", "failed"]))
        .all()
    )
    print(f"Backfilling {len(terminal_opts)} terminal-status optimization(s)...")
    for opt in terminal_opts:
        push_optimization(opt, db)
        print(f"  pushed optimization: {opt.name} (id={opt.id})")

    saved_backtests = db.query(Backtest).filter(Backtest.is_saved == True).all()  # noqa: E712
    print(f"Backfilling {len(saved_backtests)} saved backtest(s)...")
    for bt in saved_backtests:
        push_backtest(bt, db)
        print(f"  pushed backtest: {bt.name} (id={bt.id})")


def main() -> int:
    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        run(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
