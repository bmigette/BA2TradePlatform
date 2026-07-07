#!/usr/bin/env python3
"""Manual, one-off backfill: push pre-existing saved backtests and terminal optimizations to
every synced worker.

Sync (Tasks 5-8 of docs/plans/2026-07-07-remote-result-sync-implementation.md) only fires for
NEW rows going forward. Rows that already existed before this feature shipped are never
backfilled automatically (see the design doc's "Migration and Rollout" section) — run this
script once, on demand, if you want history replicated too.

Default is a DRY RUN — pass ``--apply`` to actually push. ``push_optimization``/
``push_backtest`` are fire-and-forget per worker: a failed POST is logged (via
``app.services.sync_client``'s own logger) and swallowed, never raised, so this script cannot
tell from the return value alone whether any worker actually received a row. Logging is
configured below so those warnings are visible on stderr instead of silently relying on
Python's unconfigured "handler of last resort".

Usage:
    ./venv/bin/python scripts/sync_backfill.py            # dry run, reports what would be pushed
    ./venv/bin/python scripts/sync_backfill.py --apply
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sync_client import push_optimization, push_backtest  # noqa: E402 (see sys.path.insert above)


def run(db, dry_run: bool = True) -> None:
    from app.models.strategy_optimization import StrategyOptimization
    from app.models.backtest import Backtest

    mode = "DRY-RUN" if dry_run else "APPLY"

    terminal_opts = (
        db.query(StrategyOptimization)
        .filter(StrategyOptimization.status.in_(["completed", "failed"]))
        .all()
    )
    print(f"[{mode}] Backfilling {len(terminal_opts)} terminal-status optimization(s)...")
    for opt in terminal_opts:
        if dry_run:
            print(f"  would push optimization: {opt.name} (id={opt.id})")
        else:
            push_optimization(opt, db)
            print(f"  pushed optimization: {opt.name} (id={opt.id})")

    saved_backtests = db.query(Backtest).filter(Backtest.is_saved == True).all()  # noqa: E712
    print(f"[{mode}] Backfilling {len(saved_backtests)} saved backtest(s)...")
    for bt in saved_backtests:
        if dry_run:
            print(f"  would push backtest: {bt.name} (id={bt.id})")
        else:
            push_backtest(bt, db)
            print(f"  pushed backtest: {bt.name} (id={bt.id})")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                     help="Actually push to synced workers (default is a dry run that only reports).")
    args = ap.parse_args()

    from app.models.database import SessionLocal

    db = SessionLocal()
    try:
        run(db, dry_run=not args.apply)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
