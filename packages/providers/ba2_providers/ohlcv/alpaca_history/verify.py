"""DATA-QUALITY GATE over a BUILT history cache (``<SYM>_1d.parquet`` / ``<SYM>_5min.parquet``).  Read-only.

Runbook: docs/REPRODUCE-BACKTESTS.md section 4.  Pipeline step ``verify`` (both sources):

    python tools/prepare_history.py --source alpaca --steps verify --panel-symbols panel_symbols.json
    python tools/prepare_history.py --source fmp --steps verify --panel-symbols panel_symbols.json

It reads the CACHE (the files a backtest reads), not the raw store.  Every check writes its own CSV under ``<report-dir>/verify/``
(default ``<out>/_meta/verify``), the step prints PASS / FAIL / INFO per check and exits 1 on any FAIL.  ``--panel-symbols`` is a JSON
list of the screener panel's symbols (the population the thresholds judge; without it every symbol of the run is "panel").
``--symbols`` restricts the run to a subset.  The "other" cache of the comparisons is the FMP cache for ``--source alpaca`` and the
Alpaca cache for ``--source fmp``.  Years: ``--verify-years`` (default 2019-2025).

CHECKS (constants below; the coordinator and the owner set the final thresholds)
  1 structural     FAIL on any violation (``structural.csv`` lists them): columns != the FMP layout; duplicate timestamps; NaN; prices <= 0;
                   volume < 0; Low > min(Open, Close) or High < max(Open, Close); 5-minute bar starts outside 09:30..15:55 New York; a 5-minute
                   or daily date that is not an NYSE regular session (the daily Date must also be midnight); a bar before the symbol's last
                   series break (``split_table.json`` ``breaks`` in ``<out>/_meta``; alpaca only).
  2 coverage       ``coverage_symbol_year.csv`` / ``coverage_by_year.csv``: share of the daily sessions with >= 1 regular 5-minute bar, over symbols
                   that have a 5-minute file.  FAIL when the PANEL share < MIN_COVERAGE (target: 96.5 % measured 2026-10-20 on the raw store).
  3 selfcons       last regular 5-minute close vs the same day's daily close (``selfcons.csv`` per symbol, worst first; the official closing print and the last trade before 16:00 legitimately differ on thin names, so
                   the result is also reported BY LIQUIDITY BAND, ``selfcons_by_band.csv``: terciles of the panel's median daily dollar volume).  FAIL
                   applies to the TOP TWO terciles only (the bottom tercile is informational).  FAIL when more than
                   MAX_SELFCONS_BAD (1 %) of the panel symbol-days are off by more than 2 %.
  4 residual       the MOST IMPORTANT: every overnight ratio open(D) / close(D-1) beyond x1.8 or x0.55 of the built daily series (``residual_jumps.csv``)
                   with the other cache's view: r = other close / our close on D-1 and D; class ``flat`` (|ln(r_D / r_D-1)| <= ln 1.02: the other
                   series shows the same move, so it is a real market move), ``step`` (the vendors disagree: one has a split error), ``unverifiable``
                   (the other cache has no bar on D-1 or D: listed separately, neither a pass nor a fail).  A PANEL jump since 2019-01-01 that is a ``step`` is
                   settled by YAHOO as the tie-breaker (``resolution`` column): Yahoo's close-to-close move over D-1 -> D equal to ours within 2 % =
                   ``vendor_error_fmp`` (FMP invented or missed a split; non-blocking); equal to the other vendor's =
                   ``ours_suspect`` (blocking); Yahoo has neither (or is unavailable) = ``unresolved`` (blocking).  FAIL on any blocking jump.
  5 vendor_ratio   INFORMATIONAL: r(t) = other close / our close on common days; steps of r beyond 2 % per symbol (``vendor_ratio_steps.csv``,
                   ``vendor_ratio_symbols.csv``, ``vendor_ratio_by_year.csv``); the top 50 panel symbols by step count are printed.
  6 yahoo          independent daily Close check against Yahoo (``yfinance``, ``auto_adjust=False``: Yahoo's ``Close`` is split-adjusted and NOT
                   dividend-adjusted -- probe it on AAPL around 2020-08-31 -- while ``Adj Close`` is dividend-adjusted and is not used).  Sample: the
                   given ``--symbols`` or, without them, YAHOO_SAMPLE panel symbols drawn with a seeded RNG plus YAHOO_EXTRA.  Cached under
                   ``verify/yahoo_daily/`` (offline reruns; ``--refresh-yahoo`` fetches again); a ticker Yahoo does not know is retried under its
                   renamed successors.  PASS when >= YAHOO_MATCH (99 %) of the sample symbol-days match within 0.5 % AND no symbol has a run of
                   more than YAHOO_RUN_DAYS (20) consecutive days off by more than 2 %.  ``yahoo_symbols.csv`` / ``yahoo_mismatch_runs.csv`` (runs of
                   consecutive days off by > 0.5 %, with the median ratio ours / Yahoo: a constant factor is a split error, a small step is a
                   spin-off convention difference).  Yahoo's Close is not spin-off adjusted, so a run that ends within YAHOO_EVENT_DAYS before one of
                   OUR applied spin-offs with a median ratio within 2 % of 1 / (the product of our spin-off factors after the run, or the factor of the one that ends it) is
                   ``spinoff_convention``; a run that touches one of our series breaks (within YAHOO_EVENT_DAYS) is ``series_break``; both are
                   expected and do not count; a run before one of our spin-offs whose ratio ours / Yahoo is within YAHOO_CONVENTION_TOL (10 %) of 1 -- our
                   factor within 10 % of Yahoo's -- or whose FMP / ours ratio is, is ``convention`` (the vendors compute the factor differently;
                   non-blocking); a run whose ratio ours / Yahoo equals, within 0.5 %, the cumulative factor of the events of OUR table
                   at the run's end (within YAHOO_EVENT_DAYS after it) that Yahoo's own split list lacks (a Yahoo split within +-5 days and 1 % of the factor counts as listed) is
                   ``yahoo_missing_event``; a run on a Yahoo series answered under a successor ticker (the old folder is a pre-rename duplicate)
                   whose ratio equals the product of the successor's table events dated after our last bar is ``successor_later_event``; both are
                   non-blocking and list their event ids (``events`` column); the remaining runs (``unexplained``) decide PASS / FAIL.  All three groups are reported.
  7 readiness      INFORMATIONAL (ED-11): per year the panel symbols with no built daily file / daily bars, or no 5-minute file / 5-minute bars
                   (``readiness.csv``, one row per symbol-year that lacks something).
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

MIN_COVERAGE = 0.95
COVERAGE_TARGET = 0.965
MAX_SELFCONS_BAD = 0.01
SELFCONS_TOL = 0.02              # last 5-min close vs daily close: more than 2 % off is bad
JUMP_HI, JUMP_LO = 1.8, 0.55     # residual overnight jump bounds
JUMP_SINCE = "2019-01-01"        # residual jumps since this date FAIL when the vendors disagree
STEP_TOL = 0.02                  # the other cache's ratio r moves by more than 2 % across a jump / between common days
YAHOO_SAMPLE = 200
YAHOO_SEED = 20261020
YAHOO_EXTRA = ("AAPL", "NVDA", "TSLA", "AMC", "GOOGL", "AMZN", "CRWD", "HON", "NVS", "SPY", "QQQ", "BRK-B", "GPOR", "AIV", "CTVA",
               "SM", "RLMD", "SCVL", "TPX", "OP")
YAHOO_MATCH = 0.99
YAHOO_MATCH_TOL = 0.005
YAHOO_OFF_TOL = 0.02
YAHOO_RUN_DAYS = 20
YAHOO_EVENT_DAYS = 5              # a mismatch run ending this many calendar days before one of our spin-off dates / breaks is explained by it
YAHOO_FACTOR_TOL = 0.02
YAHOO_EVENT_RATIO_TOL = 0.005    # yahoo_missing_event / successor_later_event: the ratio equals the events' factor within 0.5 %
YAHOO_LISTED_DAYS = 5
YAHOO_CONVENTION_TOL = 0.10      # spin-off factors of ours and of Yahoo / FMP within 10 %: a convention difference
DEFAULT_YEARS = tuple(range(2019, 2026))
RTH_FIRST_MIN = 9 * 60 + 30
RTH_LAST_MIN = 15 * 60 + 55
COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume", "effective_date"]
PRICES = ["Open", "High", "Low", "Close"]
STATUS_PASS, STATUS_FAIL, STATUS_INFO = "PASS", "FAIL", "INFO"

_SESSIONS: Optional[np.ndarray] = None


def nyse_sessions() -> np.ndarray:
    """Sorted ``datetime64[D]`` array of the NYSE regular sessions 2000..2026 (memoised per process)."""
    global _SESSIONS
    if _SESSIONS is None:
        from ba2_common.core.market_calendar import regular_session_dates
        _SESSIONS = np.array(regular_session_dates(date(2000, 1, 1), date(2026, 12, 31)), dtype="datetime64[D]")
    return _SESSIONS


def _rth_mask(d: pd.Series) -> pd.Series:
    minute = d.dt.hour * 60 + d.dt.minute
    return (minute >= RTH_FIRST_MIN) & (minute <= RTH_LAST_MIN)


# ---------------------------------------------------------------------------
# check 1: structural (pure, per frame)
# ---------------------------------------------------------------------------
def structural_issues(df: pd.DataFrame, interval: str, break_date: Optional[str] = None,
                      sessions: Optional[np.ndarray] = None) -> List[Tuple[str, int, str]]:
    """``[(issue, count, first example)]`` of everything wrong with one built frame (empty = clean)."""
    out: List[Tuple[str, int, str]] = []

    def add(name: str, mask: Any) -> None:
        m = np.asarray(mask, dtype=bool)
        if m.any():
            out.append((name, int(m.sum()), str(df["Date"].iloc[int(np.argmax(m))])))

    if list(df.columns) != COLUMNS:
        return [("columns", 1, f"{list(df.columns)}")]
    add("duplicate_timestamp", df["Date"].duplicated())
    add("nan", df[COLUMNS[1:6]].isna().any(axis=1))
    add("price_not_positive", (df[PRICES] <= 0).any(axis=1))
    add("volume_negative", df["Volume"] < 0)
    lo = np.minimum(df["Open"], df["Close"])
    hi = np.maximum(df["Open"], df["Close"])
    add("low_above_open_close", df["Low"] > lo * (1 + 1e-12))
    add("high_below_open_close", df["High"] < hi * (1 - 1e-12))
    if interval == "5min":
        add("bar_outside_0930_1555", ~_rth_mask(df["Date"]))
    else:
        add("daily_not_midnight", (df["Date"].dt.hour != 0) | (df["Date"].dt.minute != 0))
    sess = nyse_sessions() if sessions is None else sessions
    days = df["Date"].dt.normalize().values.astype("datetime64[D]")
    add("not_an_nyse_session", ~np.isin(days, sess))
    if break_date is not None:
        add("bar_before_series_break", df["Date"].dt.normalize() < pd.Timestamp(break_date))
    return out


# ---------------------------------------------------------------------------
# per-symbol scan (one worker task: loads the files once, runs checks 1-5 and 7)
# ---------------------------------------------------------------------------
def _daily_series(df: pd.DataFrame, col: str) -> pd.Series:
    return df.assign(day=df["Date"].dt.normalize()).drop_duplicates("day").set_index("day")[col]


def _read(path: str, columns: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
    return pd.read_parquet(path, columns=columns) if os.path.isfile(path) else None


def scan_symbol(args: Tuple[str, str, Optional[str], Tuple[int, ...], Optional[str]]) -> Dict[str, Any]:
    symbol, cache, other, years, break_date = args
    res: Dict[str, Any] = {"symbol": symbol, "median_dollar_volume": math.nan, "structural": [], "year_rows": [], "jumps": [], "ratio_steps": [], "readiness": [],
                           "error": None}
    try:
        d1 = _read(os.path.join(cache, f"{symbol}_1d.parquet"))
        d5 = _read(os.path.join(cache, f"{symbol}_5min.parquet"))
        for df, iv in ((d1, "1d"), (d5, "5min")):
            if df is not None:
                for name, n, first in structural_issues(df, "5min" if iv == "5min" else "1d", break_date):
                    res["structural"].append({"symbol": symbol, "file": iv, "issue": name, "count": n, "first": first})
        day1 = _daily_series(d1, "Close") if d1 is not None and len(d1) else None
        if day1 is not None:
            dv = (d1["Close"] * d1["Volume"])[d1["Date"].dt.year.isin(list(years))]
            res["median_dollar_volume"] = float(dv.median()) if len(dv) else math.nan
        if d5 is not None and len(d5):
            r5 = d5[_rth_mask(d5["Date"])]
            r5 = r5.assign(day=r5["Date"].dt.normalize())
            last = r5.sort_values("Date").groupby("day")["Close"].last()
        else:
            r5, last = None, None
        for y in years:
            has_d = day1 is not None and bool((day1.index.year == y).any())
            has_5 = r5 is not None and bool((r5["Date"].dt.year == y).any())
            row = {"symbol": symbol, "year": y, "has_1d_file": d1 is not None, "has_5min_file": d5 is not None,
                   "daily_bars_year": has_d, "bars_5min_year": has_5}
            res["readiness"].append(row)
            if d5 is None or not has_d:
                continue                                   # coverage population: symbols with a 5-min file and daily sessions
            days = day1.index[day1.index.year == y]
            by = r5[r5["Date"].dt.year == y]
            yr = {"symbol": symbol, "year": y, "days": len(days), "bars_5min": len(by),
                  "days_with_5min": len(set(days) & set(by["day"]))}
            both = pd.concat([last[last.index.year == y], day1[days]], axis=1, join="inner", keys=["l", "d"]).dropna()
            yr["selfcons_days"] = len(both)
            yr["selfcons_bad_days"] = int(((both["l"] / both["d"] - 1).abs() > SELFCONS_TOL).sum()) if len(both) else 0
            res["year_rows"].append(yr)
        if day1 is not None and len(d1) > 1:
            o = _daily_series(d1, "Open")
            prev = day1.shift(1)
            ratio = (o / prev).dropna()
            big = ratio[(ratio > JUMP_HI) | (ratio < JUMP_LO)]
            oth = None
            if other is not None:
                po = os.path.join(other, f"{symbol}_1d.parquet")
                if os.path.isfile(po):
                    odf = pd.read_parquet(po, columns=["Date", "Close"])
                    oth = _daily_series(odf, "Close")
            for day, rt in big.items():
                pday = day1.index[day1.index.get_loc(day) - 1]
                rec = {"symbol": symbol, "date": str(day.date()), "prev_date": str(pday.date()), "overnight_ratio": float(rt),
                       "our_close_prev": float(day1[pday]), "our_close": float(day1[day]), "r_prev": math.nan, "r_now": math.nan,
                       "step": math.nan, "class": "unverifiable"}
                if oth is not None and pday in oth.index and day in oth.index:
                    rp, rn = float(oth[pday] / day1[pday]), float(oth[day] / day1[day])
                    rec.update(r_prev=rp, r_now=rn, step=rn / rp, **{"class": "flat" if abs(math.log(rn / rp)) <= math.log(1 + STEP_TOL) else "step"})
                res["jumps"].append(rec)
            if oth is not None:
                j = pd.concat([oth, day1], axis=1, join="inner", keys=["o", "u"]).dropna()
                j = j[(j["o"] > 0) & (j["u"] > 0)]
                if len(j) > 1:
                    r = j["o"] / j["u"]
                    st = r / r.shift(1) - 1
                    for day, v in st[st.abs() > STEP_TOL].items():
                        res["ratio_steps"].append({"symbol": symbol, "date": str(day.date()), "step": float(v), "year": day.year})
    except Exception as e:                                  # reported as an error, never hidden: the gate fails on it
        res["error"] = f"{type(e).__name__}: {e}"
    return res


# ---------------------------------------------------------------------------
# check 6: Yahoo
# ---------------------------------------------------------------------------
def yfinance_daily(ticker: str) -> Optional[pd.DataFrame]:
    """Yahoo's daily history of ``ticker`` as a frame ``day, Close, Splits`` (``auto_adjust=False``: split-adjusted, not dividend-adjusted),
    or None when Yahoo has nothing for it."""
    import yfinance as yf
    h = yf.Ticker(ticker).history(start="2015-12-01", auto_adjust=False)
    if h is None or h.empty:
        return None
    idx = h.index.tz_localize(None) if h.index.tz is None else h.index.tz_convert("America/New_York").tz_localize(None)
    return pd.DataFrame({"day": idx.normalize(), "Close": h["Close"].values, "Splits": h["Stock Splits"].values})


class YahooDaily:
    """Per-symbol Yahoo daily Close with an on-disk cache (``<cache_dir>/<SYM>.parquet`` + ``<SYM>.json`` naming the answering
    ticker); a rerun is offline unless ``refresh``.  A ticker Yahoo does not know is retried under ``successors(symbol)``."""

    def __init__(self, cache_dir: str, *, refresh: bool = False, fetch: Callable[[str], Optional[pd.DataFrame]] = yfinance_daily,
                 successors: Callable[[str], List[str]] = lambda s: [], sleep: Callable[[float], None] = time.sleep,
                 min_interval: float = 0.5):
        self.cache_dir, self.refresh, self.fetch, self.successors = cache_dir, refresh, fetch, successors
        self.sleep, self.min_interval = sleep, min_interval
        self.records: Dict[str, Dict[str, Any]] = {}      # symbol -> row of yahoo_inputs.csv (every symbol asked for)

    def get(self, symbol: str) -> Tuple[Optional[pd.DataFrame], str]:
        df, note = self._get(symbol)
        rec: Dict[str, Any] = {"symbol": symbol, "ticker": "", "status": "not_found", "rows": 0, "first_day": "", "last_day": "",
                               "fetched_at": "", "sha256": "", "note": note}
        if df is not None:
            import hashlib
            pq = os.path.join(self.cache_dir, f"{symbol}.parquet")
            with open(pq, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            with open(os.path.join(self.cache_dir, f"{symbol}.json"), encoding="utf-8") as f:
                fetched = json.load(f).get("fetched_at", "")
            rec.update(ticker=note, status="ok", rows=len(df), first_day=str(df["day"].min().date()), last_day=str(df["day"].max().date()),
                       fetched_at=fetched, sha256=sha, note="")
        self.records[symbol] = rec
        return df, note

    def inputs(self) -> pd.DataFrame:
        cols = ["symbol", "ticker", "status", "rows", "first_day", "last_day", "fetched_at", "sha256", "note"]
        return pd.DataFrame([self.records[k] for k in sorted(self.records)], columns=cols)

    def _get(self, symbol: str) -> Tuple[Optional[pd.DataFrame], str]:
        """(frame or None, answering ticker or a note)."""
        from ba2_providers.ohlcv.alpaca_history.split_build import atomic_write
        pq, meta = os.path.join(self.cache_dir, f"{symbol}.parquet"), os.path.join(self.cache_dir, f"{symbol}.json")
        if not self.refresh and os.path.isfile(pq) and os.path.isfile(meta):
            with open(meta, encoding="utf-8") as f:
                return pd.read_parquet(pq), json.load(f)["ticker"]
        notes = []
        for t in [symbol] + list(self.successors(symbol)):
            self.sleep(self.min_interval)
            try:
                df = self.fetch(t)
            except Exception as e:                           # a transient failure is not cached
                notes.append(f"{t}: {type(e).__name__}: {e}")
                continue
            if df is not None and len(df):
                buf = df.to_parquet(index=False)
                atomic_write(pq, buf)
                atomic_write(meta, json.dumps({"ticker": t, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}).encode())
                return df, t
            notes.append(f"{t}: no data")
        return None, "; ".join(notes)


def _cum_spin(spinoffs: Sequence[Tuple[str, float]], after: pd.Timestamp) -> float:
    f = 1.0
    for d, v in spinoffs:
        if pd.Timestamp(d) > after:
            f *= v
    return f


def classify_run(date_from: str, date_to: str, median_ratio: float, spinoffs: Sequence[Tuple[str, float]], break_date: Optional[str],
                 fmp_median_ratio: Optional[float] = None) -> str:
    """``spinoff_convention`` / ``series_break`` / ``convention`` / ``unexplained`` for one mismatch run (ours / Yahoo; FMP / ours)."""
    end, start = pd.Timestamp(date_to), pd.Timestamp(date_from)
    near = pd.Timedelta(days=YAHOO_EVENT_DAYS)
    if any(end < pd.Timestamp(d) <= end + near for d, _ in spinoffs):
        nxt = [v for d, v in spinoffs if end < pd.Timestamp(d) <= end + near]
        for expect in [1.0 / _cum_spin(spinoffs, end)] + [1.0 / v for v in nxt]:        # all spin-offs after the run, or the one ending it
            if abs(median_ratio / expect - 1) <= YAHOO_FACTOR_TOL:
                return "spinoff_convention"
    if break_date is not None and (start <= pd.Timestamp(break_date) <= end + near):
        return "series_break"
    if any(pd.Timestamp(d) > end for d, _ in spinoffs):          # our series is spin-off adjusted over the run
        if abs(median_ratio - 1) <= YAHOO_CONVENTION_TOL or (fmp_median_ratio is not None and abs(fmp_median_ratio - 1) <= YAHOO_CONVENTION_TOL):
            return "convention"
    return "unexplained"


def classify_event_run(end_date: str, median_ratio: float, events: Sequence[Tuple[str, float]],
                       yahoo_splits: Optional[Sequence[Tuple[str, float]]], successor_events: Optional[Sequence[Tuple[str, float]]],
                       last_bar: pd.Timestamp) -> Tuple[str, str]:
    """(``yahoo_missing_event`` | ``successor_later_event`` | ``unexplained``, event ids) for a run no other class explains."""
    end = pd.Timestamp(end_date)
    if successor_events is not None:                                 # Yahoo answered under a successor ticker
        later = [(d, f) for d, f in successor_events if pd.Timestamp(d) > last_bar]
        if later:
            prod = float(np.prod([f for _, f in later]))
            if abs(median_ratio / prod - 1) <= YAHOO_EVENT_RATIO_TOL:
                return "successor_later_event", "; ".join(f"{d} x{f:g}" for d, f in later)
    if yahoo_splits is not None:
        missing = []
        for d, f in events:
            if end < pd.Timestamp(d) <= end + pd.Timedelta(days=YAHOO_EVENT_DAYS) and not any(abs((pd.Timestamp(yd) - pd.Timestamp(d)).days) <= YAHOO_LISTED_DAYS and abs(yf / f - 1) <= 0.01
                                                 for yd, yf in yahoo_splits):
                missing.append((d, f))
        if missing:
            prod = float(np.prod([f for _, f in missing]))
            if abs(median_ratio * prod - 1) <= YAHOO_EVENT_RATIO_TOL:      # ours divided by the factor, Yahoo not
                return "yahoo_missing_event", "; ".join(f"{d} x{f:g}" for d, f in missing)
    return "unexplained", ""


def compare_yahoo(ours: pd.DataFrame, yahoo: pd.DataFrame, spinoffs: Sequence[Tuple[str, float]] = (),
                  break_date: Optional[str] = None, fmp: Optional[pd.Series] = None,
                  events: Sequence[Tuple[str, float]] = (), yahoo_splits: Optional[Sequence[Tuple[str, float]]] = None,
                  successor_events: Optional[Sequence[Tuple[str, float]]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(per-symbol stats, mismatch runs) of our daily Close vs Yahoo's on common days.  A run is a maximal stretch of consecutive common
    days off by > 0.5 % (cut at each of our spin-off dates), classified by ``classify_run``; the stats count only the unexplained days as
    mismatches (``match_days`` = common days minus unexplained mismatch days)."""
    a = _daily_series(ours, "Close")
    y = yahoo.drop_duplicates("day").set_index("day")["Close"]
    j = pd.concat([a, y], axis=1, join="inner", keys=["u", "y"]).dropna()
    j = j[(j["u"] > 0) & (j["y"] > 0)]
    stats = {"common_days": len(j), "match_days": 0, "longest_off_run": 0, "spinoff_convention_days": 0, "series_break_days": 0,
             "convention_days": 0, "yahoo_missing_event_days": 0, "successor_later_event_days": 0,
             "unexplained_days": 0}
    runs: List[Dict[str, Any]] = []
    if not len(j):
        return stats, runs
    rt = j["u"] / j["y"]
    off = ((rt - 1).abs() > YAHOO_MATCH_TOL).values
    big = ((rt - 1).abs() > YAHOO_OFF_TOL).values
    idx = list(j.index)
    seg_id = np.array([sum(pd.Timestamp(d) <= t for d, _ in spinoffs) for t in idx])
    start = None
    explained = np.zeros(len(idx), dtype=bool)
    for i in range(len(idx) + 1):
        cont = i < len(idx) and off[i] and (start is None or seg_id[i] == seg_id[start])
        if cont and start is None:
            start = i
        elif not cont and start is not None:
            seg = rt.iloc[start:i]
            r = {"date_from": str(idx[start].date()), "date_to": str(idx[i - 1].date()), "days": i - start,
                 "median_ratio": float(seg.median()), "min_ratio": float(seg.min()), "max_ratio": float(seg.max()),
                 "days_off_over_2pct": int(big[start:i].sum())}
            fr = None
            if fmp is not None:
                fx = fmp.reindex(a.index[(a.index >= idx[start]) & (a.index <= idx[i - 1])])
                fr = float((fx / a.reindex(fx.index)).median()) if fx.notna().any() else None
            r["classification"] = classify_run(r["date_from"], r["date_to"], r["median_ratio"], spinoffs, break_date, fr)
            r["events"] = ""
            if r["classification"] == "unexplained":
                r["classification"], r["events"] = classify_event_run(r["date_to"], r["median_ratio"], events, yahoo_splits, successor_events,
                                                                      a.index.max())
            if r["classification"] != "unexplained":
                explained[start:i] = True
                stats[r["classification"] + "_days"] += i - start
            runs.append(r)
            start = i if (i < len(idx) and off[i]) else None
    unexplained = off & ~explained
    stats["unexplained_days"] = int(unexplained.sum())
    stats["match_days"] = len(idx) - stats["unexplained_days"]
    longest = cur = 0
    for b in (big & ~explained):
        cur = cur + 1 if b else 0
        longest = max(longest, cur)
    stats["longest_off_run"] = longest
    return stats, runs


def choose_yahoo_sample(available: Sequence[str], panel: Optional[Set[str]], symbols: Optional[Sequence[str]],
                        size: int = YAHOO_SAMPLE, seed: int = YAHOO_SEED) -> List[str]:
    """``symbols`` when given; else ``size`` seeded-random panel symbols (of those built) plus YAHOO_EXTRA that are built."""
    have = set(available)
    if symbols is not None:
        return sorted(s for s in symbols if s in have)
    pool = sorted(s for s in have if panel is None or s in panel)
    rng = random.Random(seed)
    pick = set(rng.sample(pool, min(size, len(pool))))
    return sorted(pick | (set(YAHOO_EXTRA) & have))


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def _csv(df: pd.DataFrame, path: str) -> str:
    from ba2_providers.ohlcv.alpaca_history.split_build import atomic_write
    atomic_write(path, df.to_csv(index=False).encode("utf-8"))
    return path


def list_cache_symbols(cache: str) -> List[str]:
    return sorted({n.rsplit("_", 1)[0] for n in os.listdir(cache) if n.endswith("_1d.parquet") or n.endswith("_5min.parquet")})


def load_breaks(meta_dir: str) -> Dict[str, str]:
    """symbol -> last series break date of ``<meta>/split_table.json`` (empty when there is no table)."""
    p = os.path.join(meta_dir, "split_table.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {s: max(v) for s, v in json.load(f)["breaks"].items()}


def load_splits(meta_dir: str) -> Dict[str, List[Tuple[str, float]]]:
    p = os.path.join(meta_dir, "split_table.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {s: [(d, float(v)) for d, v in e] for s, e in json.load(f)["splits"].items()}


def yahoo_split_list(ydf: pd.DataFrame) -> Optional[List[Tuple[str, float]]]:
    """Yahoo's split events from the frame's ``Splits`` column; None for a cache written before that column existed."""
    if "Splits" not in ydf.columns:
        return None
    sp = ydf[(ydf["Splits"] > 0) & (ydf["Splits"] != 1)]
    return [(str(d.date()), float(v)) for d, v in zip(sp["day"], sp["Splits"])]


def load_spinoffs(meta_dir: str) -> Dict[str, List[Tuple[str, float]]]:
    p = os.path.join(meta_dir, "split_table.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {s: [(d, float(v)) for d, v in e] for s, e in json.load(f)["spinoffs"].items()}


def resolve_with_yahoo(jump: Any, yahoo: Optional["YahooDaily"]) -> Tuple[str, float]:
    """Yahoo as the tie-breaker for one jump the two vendors disagree on: (resolution, Yahoo's close D / close D-1).
    ``vendor_error_fmp``: Yahoo's move equals ours within 2 %; ``ours_suspect``: it equals the other vendor's; ``unresolved``: neither, or
    Yahoo has no bar on both days / is unavailable."""
    if yahoo is None:
        return "unresolved", math.nan
    ydf, _ = yahoo.get(jump["symbol"])
    if ydf is None:
        return "unresolved", math.nan
    y = ydf.drop_duplicates("day").set_index("day")["Close"]
    d, p = pd.Timestamp(jump["date"]), pd.Timestamp(jump["prev_date"])
    if d not in y.index or p not in y.index or not (y[p] > 0):
        return "unresolved", math.nan
    yr = float(y[d] / y[p])
    ours = float(jump["our_close"] / jump["our_close_prev"])
    other = ours * float(jump["step"])                    # the other vendor's close-to-close move = ours x r_now / r_prev
    tol = math.log(1 + STEP_TOL)
    if abs(math.log(yr / ours)) <= tol:
        return "vendor_error_fmp", yr
    if abs(math.log(yr / other)) <= tol:
        return "ours_suspect", yr
    return "unresolved", yr


def run_gate(cache: str, *, other: Optional[str] = None, out_dir: str, symbols: Optional[Sequence[str]] = None,
             panel: Optional[Iterable[str]] = None, years: Sequence[int] = DEFAULT_YEARS, breaks: Optional[Dict[str, str]] = None,
             workers: int = 4, spinoffs: Optional[Dict[str, List[Tuple[str, float]]]] = None,
             splits: Optional[Dict[str, List[Tuple[str, float]]]] = None,
             yahoo: Optional[YahooDaily] = None, yahoo_sample: int = YAHOO_SAMPLE, yahoo_seed: int = YAHOO_SEED,
             min_coverage: float = MIN_COVERAGE, max_selfcons_bad: float = MAX_SELFCONS_BAD,
             log: Callable[[str], None] = lambda m: None) -> Dict[str, Dict[str, Any]]:
    """Run all checks; returns ``{check: {"status", "summary", "csv"...}}``.  Writes the CSVs under ``out_dir``."""
    os.makedirs(out_dir, exist_ok=True)
    panel_set: Optional[Set[str]] = set(panel) if panel is not None else None
    in_panel = (lambda s: True) if panel_set is None else (lambda s: s in panel_set)
    built = list_cache_symbols(cache)
    if symbols is not None:
        todo = sorted({s.strip().upper() for s in symbols})
    else:
        todo = sorted(set(built) | (panel_set or set()))
    if not todo:
        raise ValueError(f"nothing to verify in {cache}")
    brk = breaks or {}
    jobs = [(s, cache, other, tuple(years), brk.get(s)) for s in todo]
    log(f"verify: {len(jobs)} symbols, {workers} workers")
    if workers <= 1:
        scans = [scan_symbol(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            scans = list(ex.map(scan_symbol, jobs, chunksize=8))
    res: Dict[str, Dict[str, Any]] = {}
    errors = [{"symbol": s["symbol"], "error": s["error"]} for s in scans if s["error"]]

    def flat(key: str) -> pd.DataFrame:
        rows = [r for s in scans for r in s[key]]
        return pd.DataFrame(rows)

    # 1 structural
    st = flat("structural")
    if not len(st):
        st = pd.DataFrame(columns=["symbol", "file", "issue", "count", "first"])
    fails = len(st) + len(errors)
    res["structural"] = {"status": STATUS_FAIL if fails else STATUS_PASS, "csv": _csv(pd.concat([st, pd.DataFrame(errors).rename(columns={"error": "issue"})], ignore_index=True) if errors else st, os.path.join(out_dir, "structural.csv")),
                         "summary": {"violations": len(st), "unreadable_symbols": len(errors),
                                     "by_issue": st.groupby("issue")["count"].sum().to_dict() if len(st) else {}}}
    # 2 coverage + 3 selfcons
    yr = flat("year_rows")
    if len(yr):
        yr["panel"] = yr["symbol"].map(in_panel)
        yr["coverage"] = yr["days_with_5min"] / yr["days"]
        yr["selfcons_bad"] = np.where(yr["selfcons_days"] > 0, yr["selfcons_bad_days"] / yr["selfcons_days"].clip(lower=1), np.nan)
    else:
        yr = pd.DataFrame(columns=["symbol", "year", "days", "bars_5min", "days_with_5min", "selfcons_days", "selfcons_bad_days",
                                   "panel", "coverage", "selfcons_bad"])
    pyr = yr[yr["panel"].astype(bool)]
    if len(pyr):
        by_year = pyr.groupby("year").agg(days=("days", "sum"), days_with_5min=("days_with_5min", "sum"), symbols=("symbol", "nunique")).reset_index()
    else:
        by_year = pd.DataFrame(columns=["year", "days", "days_with_5min", "symbols"])
    by_year["coverage"] = by_year["days_with_5min"] / by_year["days"]
    _csv(yr.sort_values(["symbol", "year"]), os.path.join(out_dir, "coverage_symbol_year.csv"))
    _csv(by_year, os.path.join(out_dir, "coverage_by_year.csv"))
    days = float(pyr["days"].sum())
    cov = float(pyr["days_with_5min"].sum()) / days if days else math.nan
    res["coverage"] = {"status": STATUS_PASS if cov >= min_coverage else STATUS_FAIL, "csv": os.path.join(out_dir, "coverage_symbol_year.csv"),
                       "summary": {"panel_coverage": cov, "min": min_coverage, "target": COVERAGE_TARGET, "panel_days": int(days),
                                   "by_year": {int(r.year): round(float(r.coverage), 4) for r in by_year.itertuples()}}}
    if len(yr):
        worst = yr.groupby("symbol").agg(selfcons_days=("selfcons_days", "sum"), bad_days=("selfcons_bad_days", "sum"), panel=("panel", "first")).reset_index()
    else:
        worst = pd.DataFrame(columns=["symbol", "selfcons_days", "bad_days", "panel"])
    worst["median_dollar_volume"] = worst["symbol"].map({x["symbol"]: x["median_dollar_volume"] for x in scans})
    pw = worst[worst["panel"].astype(bool)]
    rank = pw["median_dollar_volume"].rank(method="first", na_option="bottom") / max(len(pw), 1)
    band = pd.Series(np.where(rank <= 1 / 3, "low", np.where(rank <= 2 / 3, "mid", "high")), index=pw.index)
    worst["band"] = band.reindex(worst.index)
    worst["bad_share"] = worst["bad_days"] / worst["selfcons_days"].clip(lower=1)
    worst = worst.sort_values(["bad_days", "bad_share"], ascending=False)
    _csv(worst, os.path.join(out_dir, "selfcons.csv"))
    pw = worst[worst["panel"].astype(bool)]
    bands = {}
    for name in ("low", "mid", "high"):
        g = pw[pw["band"] == name]
        d_ = float(g["selfcons_days"].sum())
        bands[name] = {"symbols": len(g), "days": int(d_), "bad_share": (float(g["bad_days"].sum()) / d_) if d_ else math.nan,
                       "max_median_dollar_volume": float(g["median_dollar_volume"].max()) if len(g) else math.nan}
    _csv(pd.DataFrame([{"band": k, **v} for k, v in bands.items()]), os.path.join(out_dir, "selfcons_by_band.csv"))
    gated = pw[pw["band"].isin(["mid", "high"])]
    sc_days = float(gated["selfcons_days"].sum())
    sc_bad = float(gated["bad_days"].sum()) / sc_days if sc_days else math.nan
    all_days = float(pw["selfcons_days"].sum())
    res["selfcons"] = {"status": STATUS_PASS if (sc_days and sc_bad <= max_selfcons_bad) else STATUS_FAIL,
                       "csv": os.path.join(out_dir, "selfcons.csv"),
                       "summary": {"gated_bad_share_top_two_terciles": sc_bad, "panel_bad_share_all": (float(pw["bad_days"].sum()) / all_days) if all_days else math.nan,
                                   "max": max_selfcons_bad, "gated_days": int(sc_days), "by_band": bands,
                                   "worst": worst.head(10)[["symbol", "bad_days", "bad_share", "band"]].to_dict("records")}}
    # 4 residual jumps
    jp = flat("jumps")
    if not len(jp):
        jp = pd.DataFrame(columns=["symbol", "date", "prev_date", "overnight_ratio", "our_close_prev", "our_close", "r_prev", "r_now", "step", "class"])
    jp["panel"] = jp["symbol"].map(in_panel)
    jp["since_2019"] = jp["date"] >= JUMP_SINCE
    _csv(jp.sort_values(["symbol", "date"]), os.path.join(out_dir, "residual_jumps.csv"))
    jp["yahoo_ratio"] = math.nan
    jp["resolution"] = ""
    cand = jp["panel"].astype(bool) & jp["since_2019"].astype(bool) & (jp["class"] == "step")
    for i in jp.index[cand]:
        jp.loc[i, ["resolution", "yahoo_ratio"]] = resolve_with_yahoo(jp.loc[i], yahoo)
    _csv(jp.sort_values(["symbol", "date"]), os.path.join(out_dir, "residual_jumps.csv"))
    bad = jp[cand & jp["resolution"].isin(["ours_suspect", "unresolved"])]
    res["residual_jumps"] = {"status": STATUS_FAIL if len(bad) else STATUS_PASS, "csv": os.path.join(out_dir, "residual_jumps.csv"),
                             "summary": {"jumps": len(jp), "by_class": jp["class"].value_counts().to_dict(),
                                         "resolutions": jp[cand]["resolution"].value_counts().to_dict(),
                                         "vendor_error_fmp": jp[cand & (jp["resolution"] == "vendor_error_fmp")][["symbol", "date", "overnight_ratio", "step"]].head(50).to_dict("records"),
                                         "unverifiable_panel_since_2019": jp[jp["panel"].astype(bool) & jp["since_2019"].astype(bool) & (jp["class"] == "unverifiable")][["symbol", "date", "overnight_ratio"]].head(50).to_dict("records"),
                                         "blocking": len(bad),
                                         "disagreements": bad[["symbol", "date", "overnight_ratio", "step", "resolution", "yahoo_ratio"]].head(50).to_dict("records")}}
    # 5 vendor ratio
    rs = flat("ratio_steps")
    if not len(rs):
        rs = pd.DataFrame(columns=["symbol", "date", "step", "year"])
    rs["panel"] = rs["symbol"].map(in_panel)
    _csv(rs.sort_values(["symbol", "date"]), os.path.join(out_dir, "vendor_ratio_steps.csv"))
    if len(rs):
        per = rs.groupby("symbol").agg(steps=("step", "size"), dates=("date", lambda x: " ".join(list(x)[:20]))).reset_index()
        per["panel"] = per["symbol"].map(in_panel)
        per = per.sort_values("steps", ascending=False)
        by_y = rs.groupby(["year", "panel"]).size().reset_index(name="steps")
    else:
        per = pd.DataFrame(columns=["symbol", "steps", "dates", "panel"])
        by_y = pd.DataFrame(columns=["year", "panel", "steps"])
    _csv(per, os.path.join(out_dir, "vendor_ratio_symbols.csv"))
    _csv(by_y, os.path.join(out_dir, "vendor_ratio_by_year.csv"))
    res["vendor_ratio"] = {"status": STATUS_INFO, "csv": os.path.join(out_dir, "vendor_ratio_symbols.csv"),
                           "summary": {"steps": len(rs), "symbols_with_steps": len(per),
                                       "by_year_panel": {f"{int(r.year)}/{'panel' if r.panel else 'other'}": int(r.steps) for r in by_y.itertuples()},
                                       "top50_panel": per[per["panel"].astype(bool)].head(50)[["symbol", "steps"]].to_dict("records")}}
    # 7 readiness
    rd = flat("readiness")
    rd["panel"] = rd["symbol"].map(in_panel)
    rd["missing_daily"] = ~(rd["has_1d_file"] & rd["daily_bars_year"])
    rd["missing_5min"] = ~(rd["has_5min_file"] & rd["bars_5min_year"])
    lack = rd[rd["panel"].astype(bool) & (rd["missing_daily"] | rd["missing_5min"])]
    _csv(lack, os.path.join(out_dir, "readiness.csv"))
    rby = rd[rd["panel"].astype(bool)].groupby("year").agg(missing_daily=("missing_daily", "sum"), missing_5min=("missing_5min", "sum")).reset_index()
    res["readiness"] = {"status": STATUS_INFO, "csv": os.path.join(out_dir, "readiness.csv"),
                        "summary": {int(r.year): {"missing_daily": int(r.missing_daily), "missing_5min": int(r.missing_5min)} for r in rby.itertuples()}}
    # 6 yahoo
    if yahoo is not None:
        sample = choose_yahoo_sample(built, panel_set, symbols, yahoo_sample, yahoo_seed)
        rows, runs, unknown = [], [], []
        tot = match = 0
        for s in sample:
            p1 = os.path.join(cache, f"{s}_1d.parquet")
            if not os.path.isfile(p1):
                continue
            ydf, note = yahoo.get(s)
            if ydf is None:
                unknown.append({"symbol": s, "note": note})
                continue
            fser = None
            if other is not None and os.path.isfile(os.path.join(other, f"{s}_1d.parquet")):
                fser = _daily_series(pd.read_parquet(os.path.join(other, f"{s}_1d.parquet"), columns=["Date", "Close"]), "Close")
            stats, rr = compare_yahoo(pd.read_parquet(p1, columns=["Date", "Close"]), ydf, (spinoffs or {}).get(s, ()), brk.get(s), fser,
                                    events=(splits or {}).get(s, ()), yahoo_splits=yahoo_split_list(ydf),
                                    successor_events=(splits or {}).get(note, ()) if note != s and splits is not None else None)
            rows.append({"symbol": s, "yahoo_ticker": note, **stats})
            runs += [{"symbol": s, **r} for r in rr]
            tot += stats["common_days"]
            match += stats["match_days"]
        ys = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["symbol", "yahoo_ticker", "common_days", "match_days", "longest_off_run",
                                                                  "spinoff_convention_days", "series_break_days", "unexplained_days"])
        _csv(ys, os.path.join(out_dir, "yahoo_symbols.csv"))
        _csv(pd.DataFrame(runs, columns=["symbol", "date_from", "date_to", "days", "median_ratio", "min_ratio", "max_ratio", "days_off_over_2pct", "classification", "events"]),
             os.path.join(out_dir, "yahoo_mismatch_runs.csv"))
        if unknown:
            _csv(pd.DataFrame(unknown), os.path.join(out_dir, "yahoo_unavailable.csv"))
        share = match / tot if tot else math.nan
        sustained = ys[ys["longest_off_run"] > YAHOO_RUN_DAYS] if len(ys) else ys
        ok = bool(tot) and share >= YAHOO_MATCH and not len(sustained)
        res["yahoo"] = {"status": STATUS_PASS if ok else STATUS_FAIL, "csv": os.path.join(out_dir, "yahoo_mismatch_runs.csv"),
                        "summary": {"sample": len(sample), "compared": len(ys), "yahoo_unavailable": len(unknown), "common_days": tot,
                                    "match_share": share, "min": YAHOO_MATCH, "sustained_mismatch_symbols": sustained["symbol"].tolist(),
                                    "runs_by_class": {c: sum(r["classification"] == c for r in runs) for c in ("spinoff_convention", "series_break", "convention", "yahoo_missing_event", "successor_later_event", "unexplained")},
                                    "listed": {c: [{k: r[k] for k in ("symbol", "date_from", "date_to", "days", "median_ratio", "events")} for r in runs if r["classification"] == c][:40]
                                               for c in ("spinoff_convention", "series_break", "convention", "yahoo_missing_event", "successor_later_event", "unexplained")},
                                    "symbols_with_unexplained_runs": sorted({r["symbol"] for r in runs if r["classification"] == "unexplained"})}}
    else:
        res["yahoo"] = {"status": STATUS_INFO, "summary": {"skipped": "no Yahoo source given"}, "csv": None}
    if yahoo is not None:
        _csv(yahoo.inputs(), os.path.join(out_dir, "yahoo_inputs.csv"))          # provenance: what Yahoo said, with the cache files' sha256
    return res


def gate_failed(results: Dict[str, Dict[str, Any]]) -> List[str]:
    return [k for k, v in results.items() if v["status"] == STATUS_FAIL]
