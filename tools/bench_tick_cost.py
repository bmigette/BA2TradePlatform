"""Micro-benchmark of the per-TICK cost of the intraday-clock hot path (synthetic, seconds).

    PYTHONPATH=<tree>/packages/common;<tree>/packages/providers;<tree>/packages/experts \
        python tools/bench_tick_cost.py [--symbols 800] [--ticks 3000] [--candidates 100]

Runs against whatever tree it is started from (``sys.path`` -> ``testplatform/backend`` of THIS file's
checkout), so the same script measures the pre-fix commit and the fixed tip. Per tick it does what the
engine does for every stepped bar on a 5-minute clock:
  1. ``set_clock``                       (per-tick cut-offs on the fixed tree)
  2. ``resolve_universe``                 (all symbols: bar exactly at T pre-fix, knowable price after)
  3. the bulk price read for ``--candidates`` symbols (what sizing / the account does: pre-fix
     ``close_at``, after ``decision_price`` incl. its DecisionPrice object) + the anchor guard
     ``require_decision_price`` per candidate where the tree has it.
Prints microseconds per tick for each step and in total.
"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform" / "backend"))


def build(n_sym, n_ticks, thin_share=0.15):
    from app.services.backtest.price_source import AsOfPriceSource

    ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
    days = pd.bdate_range("2024-01-02", periods=n_ticks // 78 + 2)
    stamps = np.concatenate([d.to_datetime64() + (np.arange(78) * 5 + 570).astype("timedelta64[m]") for d in days])
    stamps = stamps[:n_ticks]
    rng = np.random.default_rng(7)
    for i in range(n_sym):
        keep = np.ones(len(stamps), bool)
        if i < n_sym * thin_share:                       # thin names miss ~40% of the bars
            keep = rng.random(len(stamps)) > 0.4
        t = stamps[keep]
        px = 20 + np.cumsum(rng.normal(0, 0.05, len(t)))
        df = pd.DataFrame({"Date": t, "Open": px, "High": px + .1, "Low": px - .1, "Close": px + .01,
                           "Volume": np.full(len(t), 1000.0)})
        ps.load_bars_df(f"S{i:04d}", df)
    return ps, stamps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=800)
    ap.add_argument("--ticks", type=int, default=3000)
    ap.add_argument("--candidates", type=int, default=100)
    ap.add_argument("--profile", action="store_true", help="cProfile the tick loop (top 25 by tottime)")
    ns = ap.parse_args()

    from datetime import timezone
    from app.services.backtest.daily_engine import resolve_universe
    ps, stamps = build(ns.symbols, ns.ticks)
    syms = [f"S{i:04d}" for i in range(ns.symbols)]
    cfg = {"enabled_instruments": syms}
    cand = syms[: ns.candidates]
    new = hasattr(ps, "decision_price")
    try:
        from ba2_common.core.knowability import require_decision_price
    except ImportError:
        require_decision_price = None

    clocks = [pd.Timestamp(s).to_pydatetime().replace(tzinfo=timezone.utc) for s in stamps]
    if hasattr(ps, "knowable_daily_end"):
        # ONE-TIME per process: the NYSE session table is built on the first calendar question
        # (seconds); a GA worker pays it once for all its trials, so it is reported, not timed per tick.
        w0 = time.perf_counter(); ps.knowable_daily_end(clocks[0]); w1 = time.perf_counter()
        print(f"one-time calendar warm-up: {w1 - w0:.1f} s")
    t_clock = t_univ = t_price = 0.0
    n_univ = 0
    prof = None
    if ns.profile:
        import cProfile
        prof = cProfile.Profile(); prof.enable()
    for as_of in clocks:
        a = time.perf_counter(); ps.set_clock(as_of); b = time.perf_counter()
        u = resolve_universe(as_of, cfg, ps); c = time.perf_counter()
        if new:
            for s in cand:
                p = ps.decision_price(s, as_of)
                if p is not None and require_decision_price is not None:
                    require_decision_price(p, what="bench", symbol=s)
        else:
            for s in cand:
                ps.close_at(s)
        d = time.perf_counter()
        t_clock += b - a; t_univ += c - b; t_price += d - c; n_univ += len(u)
    if prof is not None:
        import pstats
        prof.disable(); pstats.Stats(prof).sort_stats("tottime").print_stats(25)
    n = len(clocks)
    tot = (t_clock + t_univ + t_price) / n * 1e6
    print(f"tree={ROOT.name} new_api={new} symbols={ns.symbols} ticks={n} candidates={ns.candidates} "
          f"avg universe={n_univ / n:.0f}")
    print(f"  set_clock        {t_clock / n * 1e6:8.1f} us/tick")
    print(f"  resolve_universe {t_univ / n * 1e6:8.1f} us/tick  ({t_univ / n / ns.symbols * 1e6:.2f} us/symbol)")
    print(f"  price+guard x{ns.candidates:<4d} {t_price / n * 1e6:8.1f} us/tick  ({t_price / n / ns.candidates * 1e6:.2f} us/call)")
    print(f"  TOTAL            {tot:8.1f} us/tick")


if __name__ == "__main__":
    main()
