"""Feasibility probe: short-horizon PULLBACK strategy, long AND short, on cached daily OHLCV.

Question: is there still an edge in "buy a short-term oversold dip in an uptrend" (and its mirror,
"sell a short-term overbought rally in a downtrend") on the large-cap option universe, after
spreads, 2020-2026 -- and do the market-condition calculators (ohlcv-v1 / ta-structure-v1) make
better exits or entry gates than the textbook SMA5 / RSI exits?

Causality: every signal uses bars 0..t and executes at the OPEN of t+1 (the live analysis runs at
09:30 on the prior close). The stop is a resting order: it fills at the stop, or at the open when
the bar gaps through it. Market-condition values come from the platform's own pure calculators
over the 128-bar window ending at t, exactly as the live gate would read them.

Split: parameters are compared IN-SAMPLE 2020-2022; 2023-2026 is reported OUT-OF-SAMPLE only.

Run:  .venv\\Scripts\\python.exe test_files/pullback_feasibility_20260924.py
"""
from __future__ import annotations

import itertools
import math
import os
import sys
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "common"))
from ba2_common.core.market_conditions import (  # noqa: E402
    WINDOW, STATUS_VALID, STRUCTURE_STATE_CODES,
    compute_market_conditions, compute_chart_structure,
)

CACHE = os.path.expanduser("~/Documents/ba2/common/cache/FMPOHLCVProvider")
UNIVERSE_FILE = os.path.join(ROOT, "tools", "options_universe_top100.txt")
START, IS_END, END = pd.Timestamp("2020-01-01"), pd.Timestamp("2022-12-31"), pd.Timestamp("2026-07-31")
SPREAD_RT = 0.0003          # measured large-band round trip (3 bps)
BORROW_PA = 0.0025          # short borrow, general-collateral large caps
SLOTS = 10                  # max concurrent positions, 1/SLOTS of equity each
STOP_ATR = 3.0
MAX_HOLD = 10
BULL, BEAR = float(STRUCTURE_STATE_CODES["bull"]), float(STRUCTURE_STATE_CODES["bear"])


# ----------------------------------------------------------------------------- data
def load():
    syms = open(UNIVERSE_FILE).read().split()
    data, dropped = {}, {}
    for s in syms + ["SPY"]:
        p = os.path.join(CACHE, f"{s}_1d.parquet")
        if not os.path.exists(p):
            dropped[s] = "no cache file"
            continue
        d = pd.read_parquet(p)[["Date", "Open", "High", "Low", "Close", "Volume"]]
        d = d.drop_duplicates("Date").sort_values("Date").set_index("Date")
        d = d[d.index >= START - pd.Timedelta(days=500)]
        d = d[d.index <= END]
        if len(d) and d["Volume"].iloc[-1] < 0.1 * d["Volume"].tail(60).median():
            d = d.iloc[:-1]  # partial last bar
        r = d["Close"].pct_change().abs()
        bad = r[(r.index >= START) & (r > 0.45)]
        if s != "SPY" and len(bad):
            dropped[s] = f"{len(bad)} daily move(s) >45% (first {bad.index[0].date()}: {bad.iloc[0]:.0%}) - suspect data"
            continue
        if s != "SPY" and (d.index < START).sum() < 260:
            dropped[s] = "under a year of history before 2020 (no SMA200 warm-up)"
            # still usable from later; keep it, the warm-up gate below handles it
        data[s] = d
    return data, dropped


def rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


class Features:
    """Per-symbol arrays + lazily memoised market-condition rows (platform calculators)."""

    def __init__(self, d: pd.DataFrame):
        self.d = d
        self.idx = d.index
        self.o, self.h, self.l, self.c, self.v = (d[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))
        c = d["Close"]
        self.sma200 = c.rolling(200).mean().to_numpy()
        self.sma5 = c.rolling(5).mean().to_numpy()
        self.rsi = {n: rsi(c, n).to_numpy() for n in (2, 3)}
        tr = pd.concat([d["High"] - d["Low"], (d["High"] - c.shift()).abs(), (d["Low"] - c.shift()).abs()], axis=1).max(axis=1)
        self.atr = tr.ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        self._mc, self._st = {}, {}

    def _win(self, t):
        a = t - WINDOW + 1
        return (self.o[a:t + 1], self.h[a:t + 1], self.l[a:t + 1], self.c[a:t + 1], self.v[a:t + 1])

    def trend_slope(self, t):
        if t + 1 < WINDOW:
            return None
        if t not in self._mc:
            ob = compute_market_conditions(*self._win(t)).trend_slope
            self._mc[t] = ob.value if ob.status == STATUS_VALID else None
        return self._mc[t]

    def structure(self, t):
        """(channel_pos, structure_state code) or (None, None)."""
        if t + 1 < WINDOW:
            return None, None
        if t not in self._st:
            sv = compute_chart_structure(*self._win(t))
            pos = sv.channel_pos.value if sv.channel_pos.status == STATUS_VALID else None
            st = sv.structure_state.value if sv.structure_state.status == STATUS_VALID else None
            self._st[t] = (pos, st)
        return self._st[t]


# ----------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Variant:
    side: str      # "L" | "S"
    gate: str      # "sma200" | "mcslope" (ohlcv-v1 trend slope sign)
    rsi_n: int
    th: float      # entry extremity: long RSI < th, short RSI > 100-th
    exit: str      # "sma5" | "rsi" | "chan" | "sma5+choch" | "time5"

    @property
    def name(self):
        return f"{self.side}-{self.gate}-rsi{self.rsi_n}<{self.th:g}-{self.exit}"


def entry_ok(f: Features, t: int, v: Variant):
    c, r = f.c[t], f.rsi[v.rsi_n][t]
    if not np.isfinite(r) or not np.isfinite(f.sma200[t]):
        return None
    if v.gate == "sma200":
        trend_up = c > f.sma200[t]
    elif v.gate == "spyregime":      # short only when the stock AND the market (SPY < SMA200) are down
        if v.side == "L":
            raise ValueError("spyregime is a short-side gate")
        trend_up = not (c < f.sma200[t] and f.spy_bear[t])
    else:
        s = f.trend_slope(t)
        if s is None:
            return None
        trend_up = s > 0
    if v.side == "L" and trend_up and r < v.th:
        return r                    # priority: lower = more oversold
    if v.side == "S" and not trend_up and r > 100 - v.th:
        return 100 - r
    return None


def exit_signal(f: Features, t: int, v: Variant, held: int) -> bool:
    long = v.side == "L"
    if held >= MAX_HOLD:
        return True
    if v.exit == "time5":
        return held >= 5
    if v.exit == "sma5":
        return f.c[t] > f.sma5[t] if long else f.c[t] < f.sma5[t]
    if v.exit == "rsi":
        r = f.rsi[v.rsi_n][t]
        return r > 70 if long else r < 30
    if v.exit == "chan":            # ta-structure-v1: reverted to the far side of the 20-bar channel
        pos, _ = f.structure(t)
        return pos is not None and (pos >= 0.75 if long else pos <= 0.25)
    if v.exit == "sma5+choch":      # textbook exit OR the swing structure turned against us
        _, st = f.structure(t)
        against = st == (BEAR if long else BULL)
        return against or (f.c[t] > f.sma5[t] if long else f.c[t] < f.sma5[t])
    raise ValueError(v.exit)


def gen_trades(feats, v: Variant):
    out = []
    for s, f in feats.items():
        n = len(f.c)
        t = 0
        first = np.searchsorted(f.idx.values, np.datetime64(START))
        t = max(first, 200)
        while t < n - 1:
            pr = entry_ok(f, t, v)
            if pr is None:
                t += 1
                continue
            e = t + 1
            px = f.o[e]
            stop = px - STOP_ATR * f.atr[t] if v.side == "L" else px + STOP_ATR * f.atr[t]
            exit_i, exit_px, why = None, None, None
            k = e
            while k < n:
                if v.side == "L" and f.l[k] <= stop:
                    exit_i, exit_px, why = k, min(f.o[k], stop) if k > e else stop, "stop"
                    break
                if v.side == "S" and f.h[k] >= stop:
                    exit_i, exit_px, why = k, max(f.o[k], stop) if k > e else stop, "stop"
                    break
                if k + 1 < n and exit_signal(f, k, v, k - e + 1):
                    exit_i, exit_px, why = k + 1, f.o[k + 1], "signal"
                    break
                k += 1
            if exit_i is None:
                break                   # open at the end of data: not counted
            sign = 1 if v.side == "L" else -1
            gross = sign * (exit_px / px - 1)
            days = (f.idx[exit_i] - f.idx[e]).days
            net = gross - SPREAD_RT - (BORROW_PA * days / 365 if v.side == "S" else 0)
            out.append((s, f.idx[t], f.idx[e], f.idx[exit_i], e, exit_i, px, exit_px, sign, net, pr, why))
            t = exit_i                  # no re-entry while in the position
    return pd.DataFrame(out, columns=["sym", "sig", "entry", "exit", "ei", "xi", "px", "xpx", "sign", "net", "prio", "why"])


# ----------------------------------------------------------------------------- portfolio
def simulate(trades: pd.DataFrame, feats, dates: pd.DatetimeIndex):
    """Slot-limited book: each entry takes 1/SLOTS of current equity; more signals than free
    slots -> most extreme RSI first. Daily mark-to-market on closes."""
    if trades.empty:
        return pd.Series(1.0, index=dates), pd.Series(0.0, index=dates)
    by_entry = {d: g.sort_values("prio") for d, g in trades.groupby("entry")}
    equity, open_pos = 1.0, []
    eq, used = [], []
    closes = {s: f.d["Close"] for s, f in feats.items()}
    for d in dates:
        # exits first (they execute at the open / intraday, freeing the slot)
        still = []
        for p in open_pos:
            if p["exit"] == d:
                equity += p["alloc"] * p["net"]
            else:
                still.append(p)
        open_pos = still
        for _, tr in by_entry.get(d, pd.DataFrame()).iterrows():
            if len(open_pos) >= SLOTS:
                break
            open_pos.append({"exit": tr.exit, "alloc": equity / SLOTS, "net": tr.net,
                             "sym": tr.sym, "px": tr.px, "sign": tr.sign})
        mtm = 0.0
        for p in open_pos:
            c = closes[p["sym"]].get(d)
            if c is not None and np.isfinite(c):
                mtm += p["alloc"] * p["sign"] * (c / p["px"] - 1)
        eq.append(equity + mtm)
        used.append(len(open_pos) / SLOTS)
    return pd.Series(eq, index=dates), pd.Series(used, index=dates)


def stats(eq: pd.Series, used: pd.Series, spy_ret: pd.Series):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    car = eq.iloc[-1] ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1
    dd = (eq / eq.cummax() - 1).min()
    r = eq.pct_change().dropna()
    sharpe = r.mean() / r.std() * math.sqrt(252) if r.std() > 0 else 0
    corr = r.corr(spy_ret.reindex(r.index))
    return dict(CAR=car, MaxDD=dd, Sharpe=sharpe, CapUsed=used.mean(), CorrSPY=corr)


def trade_stats(tr: pd.DataFrame):
    if tr.empty:
        return dict(N=0, Win=np.nan, AvgPct=np.nan, PF=np.nan, Hold=np.nan, Stops=np.nan)
    w, l = tr.net[tr.net > 0].sum(), -tr.net[tr.net <= 0].sum()
    return dict(N=len(tr), Win=(tr.net > 0).mean(), AvgPct=tr.net.mean(), PF=w / l if l > 0 else np.inf,
                Hold=(tr.exit - tr.entry).dt.days.mean(), Stops=(tr.why == "stop").mean())


# ----------------------------------------------------------------------------- main
def main():
    t0 = time.time()
    data, dropped = load()
    spy = data.pop("SPY")
    feats = {s: Features(d) for s, d in data.items()}
    spy_bear = (spy["Close"] < spy["Close"].rolling(200).mean())
    for f in feats.values():
        f.spy_bear = spy_bear.reindex(f.idx).ffill().fillna(False).to_numpy(bool)
    dates = spy.index[(spy.index >= START) & (spy.index <= END)]
    spy_ret = spy["Close"].pct_change()
    print(f"universe {len(feats)} symbols, {dates[0].date()}..{dates[-1].date()}; dropped:")
    for s, why in dropped.items():
        print(f"  {s}: {why}")

    if "--spy-shorts" in sys.argv:
        variants = [Variant(*x) for x in itertools.product(
            ("S",), ("spyregime",), (2, 3), (5, 10, 15), ("sma5", "rsi", "chan", "sma5+choch", "time5"))]
    else:
        variants = [Variant(*x) for x in itertools.product(
            ("L", "S"), ("sma200", "mcslope"), (2, 3), (5, 10, 15), ("sma5", "rsi", "chan", "sma5+choch", "time5"))]
    rows, books = [], {}
    for i, v in enumerate(variants):
        tr = gen_trades(feats, v)
        for label, a, b in (("IS", START, IS_END), ("OOS", IS_END + pd.Timedelta(days=1), END)):
            sub = tr[(tr.entry >= a) & (tr.entry <= b)]
            dd = dates[(dates >= a) & (dates <= b)]
            eq, used = simulate(sub, feats, dd)
            rows.append(dict(variant=v.name, side=v.side, gate=v.gate, rsi_n=v.rsi_n, th=v.th, exit=v.exit,
                             period=label, **trade_stats(sub), **stats(eq, used, spy_ret)))
            if label == "OOS":
                books[v.name] = eq
        if i % 10 == 0:
            print(f"  {i + 1}/{len(variants)} {v.name}  ({time.time() - t0:.0f}s)", flush=True)
    res = pd.DataFrame(rows)
    out_dir = os.path.join(ROOT, "reports")
    os.makedirs(out_dir, exist_ok=True)
    res.to_csv(os.path.join(out_dir, "pullback_feasibility_20260924%s.csv" % ("_spyshorts" if "--spy-shorts" in sys.argv else "")), index=False)

    pd.set_option("display.width", 250, "display.max_columns", 30, "display.max_rows", 200)
    fmt = {"Win": "{:.0%}".format, "AvgPct": "{:+.2%}".format, "CAR": "{:+.1%}".format, "MaxDD": "{:.1%}".format,
           "CapUsed": "{:.0%}".format, "PF": "{:.2f}".format, "Sharpe": "{:.2f}".format, "CorrSPY": "{:+.2f}".format,
           "Hold": "{:.1f}".format, "Stops": "{:.0%}".format}
    wide = res.pivot_table(index=["variant", "side", "gate", "rsi_n", "th", "exit"], columns="period",
                           values=["N", "Win", "AvgPct", "PF", "CAR", "MaxDD", "Sharpe", "CapUsed", "CorrSPY"]).reset_index()
    wide.columns = ["_".join(c).strip("_") for c in wide.columns]
    for side in sorted(set(wide.side)):
        w = wide[wide.side == side].sort_values("Sharpe_IS", ascending=False)
        print(f"\n=== {side}: top 12 by IN-SAMPLE Sharpe (2020-2022), with their OUT-OF-SAMPLE (2023-2026) ===")
        cols = ["variant", "N_IS", "AvgPct_IS", "CAR_IS", "MaxDD_IS", "Sharpe_IS",
                "N_OOS", "Win_OOS", "AvgPct_OOS", "PF_OOS", "CAR_OOS", "MaxDD_OOS", "Sharpe_OOS", "CapUsed_OOS", "CorrSPY_OOS"]
        print(w[cols].head(12).to_string(index=False, formatters={c: fmt[c.split("_")[0]] for c in cols if c.split("_")[0] in fmt}))

    print("\n=== exit rule, averaged over all entry settings (OOS) ===")
    print(res[res.period == "OOS"].groupby(["side", "exit"])[["AvgPct", "PF", "Sharpe", "CAR", "MaxDD"]].mean().to_string())
    print("\n=== trend gate: SMA200 vs ohlcv-v1 trend slope, averaged (OOS) ===")
    print(res[res.period == "OOS"].groupby(["side", "gate"])[["N", "AvgPct", "Sharpe", "CAR", "MaxDD"]].mean().to_string())

    spy_d = spy["Close"].reindex(dates)
    for label, a, b in (("IS", START, IS_END), ("OOS", IS_END + pd.Timedelta(days=1), END)):
        s = spy_d[(spy_d.index >= a) & (spy_d.index <= b)]
        e = s / s.iloc[0]
        print(f"SPY buy&hold {label}: {stats(e, pd.Series(1.0, index=e.index), spy_ret)}")
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
