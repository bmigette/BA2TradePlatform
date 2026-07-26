"""A/B the two Senate ForwardTest configs on a 5-MINUTE execution clock.

WHY: both Senate ForwardTest rows (739 = S3, 743 = S5) ran with execution_interval="1d".
Senate's ENTRY cadence is genuinely low-frequency — congressional disclosure filings, scheduled
Mondays via run_schedule_override — but its TP/SL EXITS are not: on a daily clock a protective
stop or take-profit can only be evaluated once per bar, so an intraday touch that would have
exited in reality is invisible and the trade is marked at a later close instead. This measures
how much that clock choice is worth.

STRICT A/B: the config is rebuilt through the SAME path rerun_handler uses for an
optimization-derived row — decode_params(strategy, genes) -> _build_daily_trial_config(bt_block,
decoded, hoisted) — so the genome, universe, schedules, capital and cost settings are identical
to the original. The ONLY change is execution_interval, plus the warmup recomputation that
implies. Results are written to NEW rows; the 1d originals are left untouched as the baseline.

Run:  .venv/Scripts/python.exe test_files/rerun_senate_at_5min.py [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "testplatform"))
from ba2test_launcher import _enter_backend  # noqa: E402

_enter_backend()

SOURCE_BACKTESTS = [739, 743]      # Senate S3 / S5, labelled ForwardTest
NEW_INTERVAL = "5min"


def build_cfg(db, bt, interval):
    """Rebuild the original run config, then swap ONLY the execution clock."""
    from app.services.backtest.rerun_handler import _build_optimization_rerun_config

    cfg = _build_optimization_rerun_config(db, bt)
    cfg["execution_interval"] = interval
    # backtest_id/name are set by the caller to point at the NEW row; persist_trading_db off
    # (this is a comparison run, not a post-mortem, and the sub-DBs are large).
    cfg.pop("backtest_id", None)
    cfg["persist_trading_db"] = False
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Rebuild + print the configs without running or writing rows.")
    a = ap.parse_args()

    import logging
    logging.disable(logging.INFO)   # keep the long run readable

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import (
        _persist_results, run_daily_backtest,
    )

    db = SessionLocal()
    try:
        for src_id in SOURCE_BACKTESTS:
            src = db.query(Backtest).filter(Backtest.id == src_id).first()
            if src is None:
                print(f"!! backtest {src_id} not found"); continue
            cfg = build_cfg(db, src, NEW_INTERVAL)
            uni = len(cfg.get("enabled_instruments") or [])
            print(f"\n=== {src.name} (id {src_id}) ===")
            print(f"  baseline  : interval=1d  return={src.total_return}%  "
                  f"trades={src.total_trades}  maxDD={src.max_drawdown}%  sharpe={src.sharpe_ratio}")
            print(f"  rerun as  : interval={cfg.get('execution_interval')}  universe={uni}  "
                  f"{str(cfg.get('start_date'))[:10]} -> {str(cfg.get('end_date'))[:10]}  "
                  f"warmup={cfg.get('warmup_days')}")
            if a.dry_run:
                continue

            labels = list(json.loads(src.labels) if isinstance(src.labels, str) else (src.labels or []))
            for tag in ("Senate5min", "ClockAB"):
                if tag not in labels:
                    labels.append(tag)
            new = Backtest(
                name=f"{src.name}-5min", engine_type="daily_expert",
                strategy_params=src.strategy_params, start_date=src.start_date,
                end_date=src.end_date, initial_capital=src.initial_capital,
                position_sizing_type=src.position_sizing_type,
                position_sizing_value=src.position_sizing_value,
                commission=src.commission, slippage=src.slippage,
                expert_name=src.expert_name, optimization_id=src.optimization_id,
                labels=json.dumps(labels), status="running",
                started_at=datetime.now(), is_saved=True,
                description=f"5min-execution A/B of backtest {src_id} (baseline ran 1d)",
            )
            db.add(new); db.commit(); db.refresh(new)
            cfg["backtest_id"] = new.id
            cfg["name"] = new.name
            print(f"  -> new row id {new.id}; running...", flush=True)
            try:
                res = run_daily_backtest(cfg)
                _persist_results(db, new, res)
                new.status = "completed"; new.completed_at = datetime.now()
                db.commit()
                print(f"  DONE  return={new.total_return}%  trades={new.total_trades}  "
                      f"maxDD={new.max_drawdown}%  sharpe={new.sharpe_ratio}")
            except Exception as e:
                new.status = "failed"; new.error_message = str(e)[:900]; db.commit()
                print(f"  FAILED: {e}")
                raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
