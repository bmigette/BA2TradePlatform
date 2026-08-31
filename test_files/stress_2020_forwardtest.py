"""Re-run every ForwardTest-labelled backtest over 2020-01-01 -> 2026-06-30.

The deployed configs were all selected on a 2023+ window, which contains no COVID crash, no
2022 bear market and no 2022 rate shock. This re-runs each one over the longer window to see
which survive a regime they were never fitted on.

    .venv\\Scripts\\python.exe test_files\\stress_2020_forwardtest.py --dry-run
    .venv\\Scripts\\python.exe test_files\\stress_2020_forwardtest.py --limit 3
    .venv\\Scripts\\python.exe test_files\\stress_2020_forwardtest.py

WRITES NEW ROWS, NEVER OVERWRITES. Each result is a NEW Backtest named ``<original>-2020`` with
``Stress2020`` appended to the source labels. The originals are the only record of what each
deployed config scored on its selection window, and a longer window is a DIFFERENT experiment --
it must sit beside them, not replace them. This is also why ``rerun_handler``'s
optimization-derived path is NOT reused: it overwrites IN PLACE by design. Same rule the
2026-07-28 Senate re-run followed (sen5min2-* alongside sen5min-*).

PREREQUISITES -- both are silent-wrongness traps, so they are CHECKED, not assumed:
  1. 1d OHLCV back to 2020 (fetched 2026-07-27).
  2. The screener metric_store extended back to 2020. It covered only ym=2022-01+ for a long
     time, and a screener-driven config would then silently run a SHORTER window than the rest
     of the sweep while still reporting a 2020 start date. --require-metric-store (default on)
     refuses to run screener-backed configs until the store actually reaches the start year.

OPTIONS EXPERTS ARE SKIPPED: the options cache is Alpaca-sourced and floors at 2024-01-18, so a
2020 start is impossible for them -- see docs/plans/2026-07-25-options-data-and-intraday-roadmap.md.

logging.disable(INFO) is deliberate: a direct run_daily_backtest() bypasses the GA's own log
suppression and is 10x+ slower without it.
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "testplatform"))

from ba2test_launcher import _enter_backend  # noqa: E402

START = "2020-01-01"
END = "2026-06-30"
NEW_SUFFIX = "-2020"
NEW_LABEL = "Stress2020"

# Experts whose data cannot reach 2020 (options cache floors at 2024-01-18).
# PremiumSeller was DELETED 2026-08-31 (plan Task 12); it stays listed so HISTORICAL source
# backtest rows naming it are still skipped here instead of failing expert resolution.
OPTION_EXPERTS = {"PremiumSeller"}


def metric_store_first_ym():
    """Earliest ym= partition present in the screener metric_store, or None if absent."""
    d = os.path.expanduser("~/Documents/ba2/common/cache/screener/metric_store")
    yms = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "ym=*")))
    return yms[0] if yms else None


def ohlcv_coverage(symbols, interval):
    """Split a universe by what its OHLCV parquet cache actually holds.

    Returns ``(absent, starts_late, ok)`` symbol lists, where ``starts_late`` means the
    cache EXISTS but its first bar postdates START.

    PREREQUISITE #1 in the docstring above says the 1d OHLCV cache reaching 2020 is
    "CHECKED, not assumed" -- it was not. Only the metric_store was. That mattered
    twice over:

      * ``absent``      -> the run silently drops those symbols (``AsOfPriceSource.
                           preload`` tolerates up to 5 / 1% before it hard-fails), so
                           the stress runs a DIFFERENT universe than the baseline it
                           is being compared against.
      * ``starts_late`` -> normal and fine (the instrument had not listed yet), but it
                           is exactly the state that, until the 2026-08-24 as_of-cache
                           fix, made every read below the first bar re-download the
                           symbol's whole history from FMP. Reported so the split is
                           visible rather than inferred from a bill.
    """
    from ba2_common.core import native_cache
    cutoff = datetime.fromisoformat(START)
    absent, late, ok = [], [], []
    for sym in symbols:
        path = native_cache.find_timeseries_path("FMPOHLCVProvider", sym, interval)
        if path is None:
            absent.append(sym)
            continue
        try:
            import pandas as pd
            import pyarrow.parquet as pq
            col = pq.read_table(path, columns=["Date"]).column("Date").to_pandas()
            if not len(col):
                absent.append(sym)
                continue
            first = pd.Timestamp(col.min())
            first = first.tz_localize(None) if first.tz is not None else first
        except Exception:
            absent.append(sym)
            continue
        (late if first > cutoff else ok).append(sym)
    return absent, late, ok


def _uses_metric_store(cfg) -> bool:
    """True if this config reads the screener metric_store, by ANY of the paths it can appear on.

    Checked the hard way because the first version of this guard only looked at
    ``cfg["screener_store"]`` / ``screener_opt.store`` and therefore reported False for EVERY
    real ForwardTest config -- they carry it as ``screener_runtime.store`` (and the expert's own
    ``settings.screener_store``). A guard that silently never fires is worse than no guard: the
    whole sweep would have run against a 2022-limited store while REPORTING a 2020 start.
    """
    if cfg.get("screener_store"):
        return True
    for block in ("screener_runtime", "screener_opt"):
        if (cfg.get(block) or {}).get("store"):
            return True
    for exp in (cfg.get("experts") or []):
        if ((exp or {}).get("settings") or {}).get("screener_store"):
            return True
    return False


def build_cfg(db, bt):
    """Rebuild the original run config, then widen ONLY the window."""
    from app.services.backtest.rerun_handler import _build_optimization_rerun_config

    cfg = _build_optimization_rerun_config(db, bt)
    cfg["start_date"] = START
    cfg["end_date"] = END
    cfg.pop("backtest_id", None)
    # A sweep of 40+ runs; the per-run trading sub-DBs are large and nothing here reads them.
    cfg["persist_trading_db"] = False
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="rebuild + print configs, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="only the first N (smoke test)")
    ap.add_argument("--ids", default="", help="comma-separated source backtest ids (overrides label scan)")
    ap.add_argument("--allow-stale-metric-store", action="store_true",
                    help="run screener-backed configs even if the store does not reach the start year")
    a = ap.parse_args()

    _enter_backend()
    import logging
    logging.disable(logging.INFO)

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import _persist_results, run_daily_backtest

    start_year = START[:4]
    first_ym = metric_store_first_ym()
    store_ok = bool(first_ym) and first_ym <= f"ym={start_year}-01"
    print(f"metric_store earliest partition: {first_ym or '<absent>'}  "
          f"(need <= ym={start_year}-01)  -> {'OK' if store_ok else 'TOO SHORT'}")

    db = SessionLocal()
    try:
        if a.ids:
            ids = [int(x) for x in a.ids.split(",") if x.strip()]
            srcs = [db.query(Backtest).filter(Backtest.id == i).first() for i in ids]
            srcs = [s for s in srcs if s]
        else:
            srcs = []
            for bt in db.query(Backtest).all():
                labels = bt.labels
                labels = json.loads(labels) if isinstance(labels, str) else (labels or [])
                if "ForwardTest" in labels and NEW_LABEL not in labels:
                    srcs.append(bt)
            srcs.sort(key=lambda b: b.id)
        if a.limit:
            srcs = srcs[:a.limit]

        print(f"{len(srcs)} source backtest(s) to stress\n")
        done = skipped = failed = 0

        for src in srcs:
            print(f"=== [{src.id}] {src.name}  ({src.expert_name})")
            if src.expert_name in OPTION_EXPERTS:
                print("  SKIP: options expert -- cache floors at 2024-01-18, 2020 impossible\n")
                skipped += 1
                continue

            cfg = build_cfg(db, src)
            uses_store = _uses_metric_store(cfg)
            if uses_store and not store_ok and not a.allow_stale_metric_store:
                print("  SKIP: screener-backed and the metric_store does not reach "
                      f"{start_year}. It would silently run a shorter window while REPORTING a "
                      f"{start_year} start. Extend the store, or pass --allow-stale-metric-store.\n")
                skipped += 1
                continue

            print(f"  baseline: {str(src.start_date)[:10]}->{str(src.end_date)[:10]}  "
                  f"return={src.total_return}%  trades={src.total_trades}  sharpe={src.sharpe_ratio}")
            universe = list(cfg.get("enabled_instruments") or [])
            print(f"  stress  : {START}->{END}  universe={len(universe)}  "
                  f"interval={cfg.get('execution_interval')}  screener_store={uses_store}")
            # PREREQUISITE #1, actually checked (see ohlcv_coverage).
            if universe:
                absent, late, ok = ohlcv_coverage(universe, cfg.get("execution_interval") or "1d")
                print(f"  ohlcv   : {len(ok)} reach {START}, {len(late)} listed later, "
                      f"{len(absent)} NOT CACHED")
                if absent:
                    print(f"  !! {len(absent)} symbol(s) have no cached bars and will be DROPPED "
                          f"from the run -- its results are NOT comparable with the baseline. "
                          f"Cache them first: ba2-test fetch-cache --provider fmp --timeframes "
                          f"{cfg.get('execution_interval') or '1d'} --symbols "
                          f"{' '.join(absent[:10])}{' ...' if len(absent) > 10 else ''}")
            if a.dry_run:
                print()
                continue

            labels = list(json.loads(src.labels) if isinstance(src.labels, str) else (src.labels or []))
            if NEW_LABEL not in labels:
                labels.append(NEW_LABEL)
            new = Backtest(
                name=f"{src.name}{NEW_SUFFIX}", engine_type="daily_expert",
                strategy_params=src.strategy_params,
                start_date=START, end_date=END,
                initial_capital=src.initial_capital,
                position_sizing_type=src.position_sizing_type,
                position_sizing_value=src.position_sizing_value,
                commission=src.commission, slippage=src.slippage,
                expert_name=src.expert_name, optimization_id=src.optimization_id,
                labels=json.dumps(labels), status="running",
                started_at=datetime.now(), is_saved=True,
                description=(f"2020-2026 out-of-sample stress of backtest {src.id} "
                             f"(selected on {str(src.start_date)[:10]}->{str(src.end_date)[:10]})"),
            )
            db.add(new); db.commit(); db.refresh(new)
            cfg["backtest_id"] = new.id
            cfg["name"] = new.name
            print(f"  -> new row {new.id}; running...", flush=True)
            try:
                res = run_daily_backtest(cfg)
                _persist_results(db, new, res)
                new.status = "completed"; new.completed_at = datetime.now()
                db.commit()
                print(f"  DONE  return={new.total_return}%  trades={new.total_trades}  "
                      f"maxDD={new.max_drawdown}%  sharpe={new.sharpe_ratio}\n")
                done += 1
            except Exception as e:
                # One bad config must not abandon the sweep: record it and continue, so a
                # 40-run pass still yields 39 results instead of stopping at the first failure.
                new.status = "failed"; new.error_message = str(e)[:900]; db.commit()
                print(f"  FAILED: {type(e).__name__}: {e}\n")
                failed += 1

        print(f"=== stress pass complete: {done} done, {skipped} skipped, {failed} failed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
