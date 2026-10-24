"""Ad-hoc probe (NOT collected by pytest): do FMP and Alpaca SPY daily closes give the SAME SPY regime?

Backtests default to Alpaca history, live reads SPY from FMP (owner decision 2026-10-24). The gate
flips on the SMA200 boundary / SMA50 slope sign / a 504-day vol percentile, so a small close
difference can flip a state on a few days. This reads the two LOCAL caches READ-ONLY, classifies both
series with the SAME ``BenchmarkRegimeCalendar`` (each from its own full history, as a run would),
and reports how often the trend / vol state differ on the dates both sources have.

    python test_files/probe_spy_regime_source_disagreement.py [--start 2020-01-01] [--end 2025-12-31]
        [--cache C:/Users/basti/Documents/ba2/common/cache]
"""
import argparse
import logging
import os
import sys
from datetime import date

logging.disable(logging.CRITICAL)   # standalone scripts are 10x slower with logging on

import pandas as pd

from ba2_common.core.benchmark_regime import BenchmarkRegimeCalendar

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "common", "cache")


def load_closes(cache: str, provider: str) -> pd.Series:
    path = os.path.join(cache, provider, "SPY_1d.parquet")
    df = pd.read_parquet(path, columns=["Date", "Close"])          # read-only
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    return df.dropna().drop_duplicates("Date", keep="last").set_index("Date")["Close"].sort_index()


def regimes(closes: pd.Series) -> pd.DataFrame:
    cal = BenchmarkRegimeCalendar(list(closes.index), closes.tolist())
    rows = [(d, cal.at(d)) for d in closes.index]
    return pd.DataFrame({"trend": [r.trend for _, r in rows], "vol": [r.vol for _, r in rows]},
                        index=closes.index)


def longest_run(flags: pd.Series) -> tuple:
    best, cur, start, best_span = 0, 0, None, None
    for d, f in flags.items():
        if f:
            if cur == 0:
                start = d
            cur += 1
            if cur > best:
                best, best_span = cur, (start, d)
        else:
            cur = 0
    return best, best_span


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    a = ap.parse_args(argv)
    lo, hi = date.fromisoformat(a.start), date.fromisoformat(a.end)

    fmp, alp = load_closes(a.cache, "FMPOHLCVProvider"), load_closes(a.cache, "AlpacaHistoryOHLCVProvider")
    print(f"FMP    {fmp.index[0]} -> {fmp.index[-1]}  ({len(fmp)} closes)")
    print(f"Alpaca {alp.index[0]} -> {alp.index[-1]}  ({len(alp)} closes)")
    rf, ra = regimes(fmp), regimes(alp)

    window = [d for d in fmp.index if lo <= d <= hi]
    only_fmp = [d for d in window if d not in alp.index]
    only_alp = [d for d in alp.index if lo <= d <= hi and d not in fmp.index]
    common = [d for d in window if d in alp.index]
    print(f"window {lo} -> {hi}: {len(common)} common days; only in FMP {len(only_fmp)}, "
          f"only in Alpaca {len(only_alp)}")

    diff = (fmp.loc[common] - alp.loc[common]).abs()
    rel = diff / fmp.loc[common] * 100
    print(f"max abs close diff {diff.max():.4f} on {diff.idxmax()} ({rel.max():.3f}%); "
          f"days with diff > 0.01: {int((diff > 0.01).sum())}, > 0.50: {int((diff > 0.5).sum())}")

    both_known = {}
    for axis in ("trend", "vol"):
        known = [d for d in common if rf.at[d, axis] is not None and ra.at[d, axis] is not None]
        unknown = len(common) - len(known)
        dis = pd.Series({d: rf.at[d, axis] != ra.at[d, axis] for d in known})
        both_known[axis] = dis
        n = int(dis.sum())
        run, span = longest_run(dis)
        pct = 100.0 * n / len(known) if known else float("nan")
        print(f"{axis:5s}: compared {len(known)} days (unknown on either side: {unknown}); "
              f"differ on {n} = {pct:.2f}%; longest disagreeing run {run} days {span}")
        by_year = dis.groupby([d.year for d in dis.index]).agg(["sum", "count"])
        print("   by year: " + ", ".join(f"{y}: {int(r['sum'])}/{int(r['count'])} "
                                        f"({100.0 * r['sum'] / r['count']:.1f}%)"
                                        for y, r in by_year.iterrows()))
        by_month = dis.groupby([f"{d.year}-{d.month:02d}" for d in dis.index]).sum().sort_values(ascending=False)
        worst = [f"{ym}: {int(v)}d" for ym, v in by_month.head(5).items() if v > 0]
        print("   worst months: " + (", ".join(worst) if worst else "none"))
        if n:
            print("   first/last differing day: " + f"{dis[dis].index[0]} / {dis[dis].index[-1]}")
    either = pd.Series({d: bool(both_known["trend"].get(d, False) or both_known["vol"].get(d, False))
                        for d in common})
    print(f"either axis differs: {int(either.sum())} of {len(common)} = {100.0 * either.mean():.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
