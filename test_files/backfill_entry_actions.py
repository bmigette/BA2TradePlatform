"""One-off backfill: populate strategy_params["entryActions"] for optimization-derived Backtest
rows persisted BEFORE the ba2test_launcher.py:_persist_top_backtests fix (which forgot to mirror
decoded["entry_rules"] the same way buy/sell/exit trees were mirrored). The underlying backtest
NUMBERS for these rows are correct (the trial that actually ran used the real entry_rules) -- this
only repairs the DISPLAY/EXPORT copy in strategy_params by recomputing entry_rules the exact same
way rerun_handler._build_optimization_rerun_config does: decode_params(strat, gene-only params).

Usage: <test venv>/python.exe test_files/backfill_entry_actions.py [--apply]
Without --apply, runs as a dry-run and only prints what WOULD change.
"""
import argparse
import sys

sys.path.insert(0, "testplatform/backend")

from app.models.database import SessionLocal  # noqa: E402
from app.models.backtest import Backtest  # noqa: E402
from app.models.strategy import Strategy  # noqa: E402
from app.models.strategy_optimization import StrategyOptimization  # noqa: E402
from app.services.strategy_param_space import decode_params  # noqa: E402
from app.services.backtest.rerun_handler import _gene_params  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(Backtest).filter(Backtest.optimization_id.isnot(None)).all()
        opt_cache: dict = {}
        strat_cache: dict = {}
        scanned = fixable = updated = 0
        for bt in rows:
            scanned += 1
            sp = bt.strategy_params or {}
            if sp.get("entryActions"):
                continue  # already has it (either always did, or persisted after the fix)

            opt = opt_cache.get(bt.optimization_id)
            if opt is None and bt.optimization_id not in opt_cache:
                opt = db.query(StrategyOptimization).filter_by(id=bt.optimization_id).first()
                opt_cache[bt.optimization_id] = opt
            if opt is None:
                continue  # orphaned optimization_id

            strat = strat_cache.get(opt.strategy_id)
            if strat is None and opt.strategy_id not in strat_cache:
                strat = db.query(Strategy).filter_by(id=opt.strategy_id).first()
                strat_cache[opt.strategy_id] = strat
            if strat is None or not strat.entry_actions:
                continue  # this strategy template has no entry_actions at all (S1/S2/S3/S5/S6) -> correctly empty

            decoded = decode_params(strat, _gene_params(sp))
            entry_rules = decoded.get("entry_rules") or []
            if not entry_rules:
                continue  # genes decoded to all-toggled-off -> correctly empty

            fixable += 1
            print(f"  bt#{bt.id} '{bt.name}' (opt={bt.optimization_id}, strategy={strat.name}): "
                  f"+{len(entry_rules)} entry action(s)")
            if args.apply:
                new_sp = dict(sp)
                new_sp["entryActions"] = entry_rules
                bt.strategy_params = new_sp
                updated += 1

        if args.apply:
            db.commit()
        print(f"\nscanned={scanned} fixable={fixable} "
              f"{'updated' if args.apply else 'would_update'}={updated if args.apply else fixable}")
        if not args.apply:
            print("dry-run only -- re-run with --apply to write changes")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
