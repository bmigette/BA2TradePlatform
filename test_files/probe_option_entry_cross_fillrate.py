"""MEASURE, on the REAL local TastyTrade parquet store, how much the F3 entry-quote
concession changes option ENTRY fill rates.

Not a pytest test -- an ad-hoc probe (see CLAUDE.md on test_files/ vs tests/). It reproduces
the engine's arithmetic exactly:

  quote (analysis bar d):   c = close(d)            # the store's bid == ask == close, a MID
  fill  (next bar d+1):     buy  fills iff open(d+1) + half(d+1) <= limit
                            sell fills iff open(d+1) - half(d+1) >= limit
  limit at concession f:    buy  = c + f * half(d)
                            sell = c - f * half(d)
  half(bar) = max(option_spread_min_tick, close * option_spread_pct/100)
              * (2 if volume < 100 else 1) / 2          # _OPTION_SPREAD_THIN_MULT

APPROXIMATION, stated so the numbers are not over-read: the engine's fill day is the
UNDERLYING's next trading day, while this walks each CONTRACT's own consecutive bars (gap of
1-4 calendar days). A contract that does not print on the fill day cannot fill in the engine
either, so this measures the SUBSET where a fill was possible at all -- which is the subset
the gene can move.

    python test_files/probe_option_entry_cross_fillrate.py [SYM ...]
"""
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.expanduser("~/Documents/ba2_trade_platform/cache/TastyTradeOptionsProvider")
SPREAD_PCT = 5.0          # --option-spread-pct default
MIN_TICK = 0.02           # --option-spread-min-tick default
LIQUID_VOLUME = 100.0     # _OPTION_SPREAD_LIQUID_VOLUME
THIN_MULT = 2.0           # _OPTION_SPREAD_THIN_MULT
MIN_PREMIUM = 0.10        # option_selector._MIN_TRADEABLE_PREMIUM
MIN_VOLUME = 25           # _OPTION_MIN_VOLUME_DEFAULT
DTE_LO, DTE_HI = 25, 45   # the grid's authored DTE window
DEFAULT_SYMS = ["GOOG", "GOOGL", "BABA", "AMZN", "BAC", "SCHW", "INTC", "F", "T"]


def half_spread(close, volume):
    full = np.maximum(MIN_TICK, np.abs(close) * SPREAD_PCT / 100.0)
    full = np.where((volume < LIQUID_VOLUME) | ~np.isfinite(volume), full * THIN_MULT, full)
    return full / 2.0


def load(sym):
    parts = []
    base = os.path.join(ROOT, sym)
    for d in sorted(os.listdir(base)):
        for f in sorted(os.listdir(os.path.join(base, d))):
            if f.endswith(".parquet"):
                parts.append(pd.read_parquet(os.path.join(base, d, f)))
    df = pd.concat(parts, ignore_index=True)
    df["bar_date"] = pd.to_datetime(df["bar_date"])
    df["expiry"] = pd.to_datetime(df["expiry"])
    return df


def measure(sym, fractions=(0.0, 0.25, 0.5, 0.75, 1.0)):
    df = load(sym)
    df["dte"] = (df["expiry"] - df["bar_date"]).dt.days
    df = df.sort_values(["occ_symbol", "bar_date"])
    g = df.groupby("occ_symbol", sort=False)
    df["n_open"] = g["open"].shift(-1)
    df["n_volume"] = g["volume"].shift(-1)
    df["gap"] = (g["bar_date"].shift(-1) - df["bar_date"]).dt.days

    ok = (df["gap"].between(1, 4) & df["n_open"].notna() & (df["close"] >= MIN_PREMIUM)
          & (df["volume"] >= MIN_VOLUME) & df["dte"].between(DTE_LO, DTE_HI))
    d = df[ok]
    if d.empty:
        return None
    c = d["close"].to_numpy(float)
    h_q = half_spread(c, d["volume"].to_numpy(float))          # as-of bar (the quote)
    h_f = half_spread(d["n_open"].to_numpy(float), d["n_volume"].to_numpy(float))
    o = d["n_open"].to_numpy(float)

    out = {"rows": len(d)}
    for f in fractions:
        out[("sell", f)] = float(np.mean((o - h_f) >= (c - f * h_q)))
        out[("buy", f)] = float(np.mean((o + h_f) <= (c + f * h_q)))
    return out


def main(syms):
    tot = defaultdict(float)
    rows = 0
    print(f"{'sym':<7}{'rows':>9}  " + "  ".join(f"S{f:<5g}" for f in (0.0, 0.5, 1.0))
          + "  " + "  ".join(f"B{f:<5g}" for f in (0.0, 0.5, 1.0)))
    for sym in syms:
        r = measure(sym)
        if r is None:
            print(f"{sym:<7}{'-':>9}")
            continue
        rows += r["rows"]
        for k, v in r.items():
            if isinstance(k, tuple):
                tot[k] += v * r["rows"]
        print(f"{sym:<7}{r['rows']:>9}  "
              + "  ".join(f"{r[('sell', f)]:>6.1%}" for f in (0.0, 0.5, 1.0)) + "  "
              + "  ".join(f"{r[('buy', f)]:>6.1%}" for f in (0.0, 0.5, 1.0)))
    print("-" * 60)
    print(f"{'ALL':<7}{rows:>9}  "
          + "  ".join(f"{tot[('sell', f)] / rows:>6.1%}" for f in (0.0, 0.5, 1.0)) + "  "
          + "  ".join(f"{tot[('buy', f)] / rows:>6.1%}" for f in (0.0, 0.5, 1.0)))
    print("\nS = SELL entry fill rate, B = BUY entry fill rate, at concession f=0/0.5/1.0")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_SYMS)
