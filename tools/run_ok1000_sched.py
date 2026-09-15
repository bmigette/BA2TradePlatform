#!/usr/bin/env python
"""Re-run the DEPLOYED settings under the static $1000 base WITH THE GENOME'S OWN ENTRY DAYS.

    .venv/Scripts/python.exe tools/run_ok1000_sched.py --list
    .venv/Scripts/python.exe tools/run_ok1000_sched.py --only 1088
    .venv/Scripts/python.exe tools/run_ok1000_sched.py --all

WHY THIS EXISTS, separately from run_ok1000.py. The GA searches the entry WEEKDAY per
individual (``schedule:<day>`` genes) and a decoded cadence replaces the run-level one for
that individual. ``rerun_handler._gene_params`` filtered those genes out before
``decode_params`` ever saw them, so every re-run silently fell back to the run-level
Monday-only override -- including all 21 ok1000 runs. Fixed 2026-09-07 by adding "schedule"
to ``_GENE_PREFIXES``; this script re-measures the six DEPLOYED settings on the corrected
path so the live cadence can be judged against something real.

The old ok1000 rows are NOT deleted or overwritten. They are a genuine measurement of the
Monday-only cadence -- which is exactly what prod is running right now -- so they are the
control, and these new rows are the treatment. Both are needed to answer "should prod move
to the genome's days?".

FAIL LOUDLY. Each run asserts the rebuilt config's cadence actually equals the genome's
before spending the compute; a silent fallback is the whole defect being repaired here.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))


def _bootstrap():
    from ba2test_launcher import _enter_backend
    return _enter_backend()


_bootstrap()

CAP = 1000.0
LABEL = "ok1000-sched"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: The six settings deployed to prod instances 7-12, as (live instance id, parent backtest id).
DEPLOYED = [(7, 1107), (8, 1298), (9, 1363), (10, 1173), (11, 1088), (12, 1330)]


def _labels(raw):
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return list(v) if isinstance(v, list) else [str(v)]


def _add_label(row, label):
    ls = _labels(row.labels)
    if label not in ls:
        ls.append(label)
        row.labels = ls
    return ls


def _genome_days(parent):
    """The weekdays the GA actually selected, from the stored genes."""
    sp = parent.strategy_params or {}
    return {d: bool(sp.get(f"schedule:{d}", 0)) for d in DAYS}


def _trading_days(days):
    return [d for d in DAYS[:5] if days.get(d)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="run ONE parent backtest id")
    ap.add_argument("--all", action="store_true", help="run all six deployed settings")
    ap.add_argument("--list", action="store_true")
    ns = ap.parse_args()

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import _persist_results, run_daily_backtest
    from app.services.backtest.rerun_handler import build_rerun_config

    db = SessionLocal()
    if ns.list or not (ns.only or ns.all):
        for inst, pid in DEPLOYED:
            p = db.query(Backtest).filter(Backtest.id == pid).first()
            print(f"  inst {inst:<3} bt {pid:<6} genome days = {_trading_days(_genome_days(p))}"
                  f"   {p.name[:60]}")
        return 0

    targets = [ns.only] if ns.only else [pid for _i, pid in DEPLOYED]
    done = []
    try:
        for pid in targets:
            parent = db.query(Backtest).filter(Backtest.id == pid).first()
            if parent is None:
                print(f"  !! parent {pid} not found")
                continue
            # RESUMABLE, and duplicate-proof: --only 1298 as a smoke test then --all must not
            # leave two OK1000S rows for the same parent (a later comparison would silently
            # pick whichever it found first).
            existing = db.query(Backtest).filter(
                Backtest.name == f"OK1000S-{parent.name}"[:255]).first()
            if existing is not None and not ns.only:
                print(f"  -- parent {pid} already has row {existing.id} "
                      f"({existing.status}), skipping")
                continue
            want = _genome_days(parent)
            config = copy.deepcopy(build_rerun_config(db, parent))

            # THE ASSERTION THIS SCRIPT EXISTS FOR. A cadence that quietly reverts to the
            # run-level override is the defect; refuse to burn an hour producing another
            # mislabelled number.
            got = (config.get("run_schedule_override") or {}).get("days") or {}
            if _trading_days(got) != _trading_days(want):
                print(f"  !! {pid}: rebuilt cadence {_trading_days(got)} != genome "
                      f"{_trading_days(want)} -- schedule genes are STILL being dropped")
                continue

            acct = dict(config.get("account_settings") or {})
            acct["equity_cap"] = CAP
            config["account_settings"] = acct

            child = Backtest(
                name=f"OK1000S-{parent.name}"[:255],
                engine_type="daily_expert",
                expert_name=parent.expert_name,
                optimization_id=None,
                strategy_id=parent.strategy_id,
                strategy_params={**(parent.strategy_params or {}), "equityCap": CAP,
                                 "runScheduleOverride": config["run_schedule_override"]},
                start_date=parent.start_date,
                end_date=parent.end_date,
                initial_capital=parent.initial_capital,
                position_sizing_type=parent.position_sizing_type,
                position_sizing_value=parent.position_sizing_value,
                commission=parent.commission,
                slippage=parent.slippage,
                fitness_metric=parent.fitness_metric,
                status="running",
                is_saved=False,
            )
            _add_label(child, LABEL)
            db.add(child)
            db.commit()
            db.refresh(child)
            config["backtest_id"] = child.id
            config["name"] = child.name

            print(f"  -> parent {pid} => row {child.id}  days={_trading_days(want)} "
                  f"cap=${CAP:,.0f} ...", flush=True)
            try:
                results = run_daily_backtest(config)
            except Exception as e:  # noqa: BLE001 - report and continue
                child.status = "failed"
                child.error_message = str(e)[:900]
                db.commit()
                print(f"     FAILED: {e}")
                continue
            _persist_results(db, child, results)
            child.status = "completed"
            child.is_saved = True
            db.commit()
            done.append((pid, child.id, results))
            print(f"     ret={results.get('total_return')}%  trades={results.get('total_trades')}"
                  f"  WR={results.get('win_rate')}%  maxDD={results.get('max_drawdown')}%")
    finally:
        db.close()

    print(f"\n=== {len(done)} run(s) complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
