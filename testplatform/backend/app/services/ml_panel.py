"""Point-in-time intraday panel for the custom ML models (docs/plans/2026-10-27-custom-ml-models-design.md, section 1).

Source: the cached Alpaca 5-minute parquet ``<cache>/AlpacaHistoryOHLCVProvider/<SYM>_5min.parquet`` only (no
network). Bars are aggregated to H1 or M15 over the REGULAR session (NYSE calendar: a half day ends at its early close;
the 5-minute cache also holds prints after an early close, which are dropped).

Every bar is stamped at its END. A row at bar end ``t`` uses only data that ended at or before ``t``:
  * technical features: the symbol's own bars up to and including the row's bar;
  * daily context: the bar's close against COMPLETED prior sessions (moving averages, ATR, past returns);
  * market features: SPY / QQQ bars with the same end time;
  * fundamentals: values whose filing / announcement was public before the row's session opened
    (``fundamentals_visible_from``). The "current fundamentals" broadcast is never used here.

Feature columns carry their group as a prefix: ``t_`` technical, ``d_`` daily context, ``m_`` market,
``f_`` point-in-time fundamentals. Target ingredients (``fwd_ret_<N>``, high/low paths) are kept apart from the
features; ``feature_columns`` never returns them.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

MAG7: Tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA")
CONTEXT_SYMBOLS: Tuple[str, ...] = ("SPY", "QQQ")
TIMEFRAMES: Dict[str, int] = {"1h": 60, "15m": 15}
FEATURE_GROUPS: Tuple[str, ...] = ("technical", "daily", "market", "fundamentals")
GROUP_PREFIX: Dict[str, str] = {"technical": "t_", "daily": "d_", "market": "m_", "fundamentals": "f_"}
#: forward-return horizons (sessions) stored as target ingredients
FWD_HORIZONS: Tuple[int, ...] = (1, 3, 5, 10)
#: A symbol may miss at most this share of the reference calendar's bars; each missing bar is filled flat at the
#: previous close with zero volume and LISTED in the panel meta. Above it the build refuses.
MAX_MISSING_BAR_SHARE = 0.001
_ET = "America/New_York"


class PanelDataError(RuntimeError):
    """Cached data missing or inconsistent: the panel refuses rather than guess."""


def bars_per_session(timeframe: str) -> int:
    """Bars of a full 390-minute session (the last H1 bar is 30 minutes long)."""
    return math.ceil(390 / TIMEFRAMES[timeframe])


def _cache_file(cache_root: Path, symbol: str) -> Path:
    p = Path(cache_root) / "AlpacaHistoryOHLCVProvider" / f"{symbol}_5min.parquet"
    if not p.exists():
        raise PanelDataError(f"no cached 5-minute bars for {symbol}: {p}")
    return p


def regular_sessions(first_day: date, last_day: date) -> pd.DataFrame:
    """NYSE regular sessions as ET-naive open / close timestamps (one row per session)."""
    from ba2_common.core.market_calendar import nyse_regular_sessions
    rows = []
    for o, c in nyse_regular_sessions(first_day, last_day):
        o_et = pd.Timestamp(o).tz_convert(_ET).tz_localize(None)
        c_et = pd.Timestamp(c).tz_convert(_ET).tz_localize(None)
        rows.append((o_et.date(), o_et, c_et))
    if not rows:
        raise PanelDataError(f"no NYSE session between {first_day} and {last_day}")
    return pd.DataFrame(rows, columns=["session", "open_ts", "close_ts"])


def load_bars(symbol: str, timeframe: str, sessions: pd.DataFrame, cache_root: Path) -> pd.DataFrame:
    """``timeframe`` bars of one symbol over ``sessions``: columns session, bar, end, open, high, low, close, volume.

    ``bar`` is the bar's index within its session; ``end`` is min(bin end, session close). A bar exists when at
    least one 5-minute bar of the cache falls in it."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {sorted(TIMEFRAMES)}, got {timeframe!r}")
    minutes = TIMEFRAMES[timeframe]
    df = pd.read_parquet(_cache_file(cache_root, symbol), columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    first, last = sessions["open_ts"].iloc[0], sessions["close_ts"].iloc[-1]
    df = df[(df["Date"] >= first) & (df["Date"] < last)]
    day = df["Date"].dt.date
    s = sessions.set_index("session")
    known = day.isin(s.index)
    df, day = df[known], day[known]
    open_ts = day.map(s["open_ts"])
    close_ts = day.map(s["close_ts"])
    # a 5-minute bar belongs to the regular session when it STARTS at/after the open and ENDS at/before the close
    rth = (df["Date"] >= open_ts) & (df["Date"] + pd.Timedelta(minutes=5) <= close_ts)
    df, day, open_ts, close_ts = df[rth], day[rth], open_ts[rth], close_ts[rth]
    if df.empty:
        raise PanelDataError(f"{symbol}: no regular-session 5-minute bars in the requested range")
    b = ((df["Date"] - open_ts) // pd.Timedelta(minutes=minutes)).astype(int)
    g = df.assign(session=day.values, bar=b.values, open_ts=open_ts.values, close_ts=close_ts.values)
    out = g.groupby(["session", "bar"], sort=True).agg(
        open=("Open", "first"), high=("High", "max"), low=("Low", "min"), close=("Close", "last"),
        volume=("Volume", "sum"), open_ts=("open_ts", "first"), close_ts=("close_ts", "first")).reset_index()
    bin_end = out["open_ts"] + pd.to_timedelta((out["bar"] + 1) * minutes, unit="m")
    out["end"] = bin_end.where(bin_end <= out["close_ts"], out["close_ts"])
    return out.drop(columns=["open_ts", "close_ts"])


def align_to_reference(bars: pd.DataFrame, ref_keys: pd.DataFrame, symbol: str) -> Tuple[pd.DataFrame, List[str]]:
    """Put ``bars`` on the reference (session, bar) grid. Missing bars are filled flat at the previous close with zero
    volume when they are at most MAX_MISSING_BAR_SHARE of the grid; the filled bar ends are returned. Bars the
    reference does not have are refused (the reference is SPY, the most liquid series)."""
    keys = ref_keys[["session", "bar", "end"]]
    extra = bars.merge(keys[["session", "bar"]], how="left", on=["session", "bar"], indicator=True)
    extra = extra[extra["_merge"] == "left_only"]
    if len(extra):
        raise PanelDataError(f"{symbol}: {len(extra)} bars not on the reference calendar, first {extra.iloc[0]['session']}")
    m = keys.merge(bars.drop(columns=["end"]), how="left", on=["session", "bar"])
    miss = m["close"].isna()
    if miss.any():
        share = miss.mean()
        if share > MAX_MISSING_BAR_SHARE:
            raise PanelDataError(f"{symbol}: {int(miss.sum())} of {len(m)} bars missing ({share:.3%} > "
                                 f"{MAX_MISSING_BAR_SHARE:.3%})")
        if miss.iloc[0]:
            raise PanelDataError(f"{symbol}: the first bar of the range is missing; extend the range")
        prev = m["close"].ffill()
        for c in ("open", "high", "low", "close"):
            m.loc[miss, c] = prev[miss]
        m.loc[miss, "volume"] = 0.0
    filled = [str(t) for t in m.loc[miss, "end"]]
    return m, filled


# ------------------------------------------------------------------ indicator helpers (causal)
def ema(x: pd.Series, span: int) -> pd.Series:
    return x.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)


# the helpers are shared with app.services.ml_features: public names, the old private names stay as aliases
_ema, _wilder, _true_range = ema, wilder, true_range


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    return 100.0 - 100.0 / (1.0 + up / dn.replace(0.0, np.nan))


def daily_from_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Session OHLC from the regular-session bars (one row per session, ascending)."""
    return bars.groupby("session", sort=True).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                                   close=("close", "last"))


def technical_features(b: pd.DataFrame, bars_per_session: int) -> pd.DataFrame:
    c, h, l, v = b["close"], b["high"], b["low"], b["volume"]
    lr = np.log(c)
    bps = bars_per_session
    atr = _wilder(_true_range(h, l, c), 14)
    out = pd.DataFrame(index=b.index)
    out["t_ret_1"] = lr.diff(1)
    out["t_ret_3"] = lr.diff(3)
    out["t_ret_1d"] = lr.diff(bps)
    out["t_ret_5d"] = lr.diff(5 * bps)
    r1 = lr.diff(1)
    out["t_vol_1d"] = r1.rolling(bps, min_periods=bps).std()
    out["t_vol_5d"] = r1.rolling(5 * bps, min_periods=5 * bps).std()
    out["t_vol_ratio"] = out["t_vol_1d"] / out["t_vol_5d"]
    out["t_atr_pct"] = atr / c
    out["t_rsi"] = _rsi(c) / 100.0 - 0.5
    macd = _ema(c, 12) - _ema(c, 26)
    out["t_macd_hist"] = (macd - _ema(macd, 9)) / atr
    mid, sd = c.rolling(20, min_periods=20).mean(), c.rolling(20, min_periods=20).std()
    out["t_bb_pctb"] = (c - (mid - 2 * sd)) / (4 * sd) - 0.5
    out["t_bb_width"] = 4 * sd / mid
    out["t_dist_ema20"] = (c - _ema(c, 20)) / atr
    out["t_dist_ema50"] = (c - _ema(c, 50)) / atr
    hh, ll = h.rolling(14, min_periods=14).max(), l.rolling(14, min_periods=14).min()
    out["t_stoch"] = (c - ll) / (hh - ll).replace(0.0, np.nan) - 0.5
    lv = np.log1p(v)
    out["t_logvol_z"] = (lv - lv.rolling(5 * bps, min_periods=5 * bps).mean()) / lv.rolling(5 * bps, min_periods=5 * bps).std()
    out["t_bar_of_day"] = b["bar"] / max(bps - 1, 1)
    return out


def daily_context(b: pd.DataFrame, daily: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    """Bar close against COMPLETED prior sessions. Returns the features and the prior-session daily ATR14 in price
    units (the unit of the triple-barrier target)."""
    d = daily.copy()
    d["sma50"] = d["close"].rolling(50, min_periods=50).mean()
    d["sma200"] = d["close"].rolling(200, min_periods=200).mean()
    d["atr14"] = _wilder(_true_range(d["high"], d["low"], d["close"]), 14)
    for n in (1, 5, 20):
        d[f"close_lag{n}"] = d["close"].shift(n - 1)
    # every daily statistic is shifted one session: the row of session S sees statistics of sessions < S only
    prior = d[["sma50", "sma200", "atr14", "close_lag1", "close_lag5", "close_lag20"]].shift(1)
    p = prior.reindex(b["session"].values)
    p.index = b.index
    c = b["close"]
    out = pd.DataFrame(index=b.index)
    out["d_dist_sma50"] = c / p["sma50"] - 1.0
    out["d_dist_sma200"] = c / p["sma200"] - 1.0
    out["d_ret_1d"] = np.log(c / p["close_lag1"])
    out["d_ret_5d"] = np.log(c / p["close_lag5"])
    out["d_ret_20d"] = np.log(c / p["close_lag20"])
    out["d_atr_pct"] = p["atr14"] / p["close_lag1"]
    return out, p["atr14"]


def market_features(b: pd.DataFrame, own_tech: pd.DataFrame, own_daily: pd.DataFrame,
                    ctx: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]],
                    context: Sequence[str] = CONTEXT_SYMBOLS) -> pd.DataFrame:
    """Returns of every context symbol at the same bar end and relative strength against the benchmark (QQQ when it
    is a context symbol, else the first one). ``ctx[sym]`` = (bars, technical, daily context) of the context symbol,
    all on the same reference grid as ``b``."""
    out = pd.DataFrame(index=b.index)
    for sym in context:
        cb, ct, cd = ctx[sym]
        if not (cb["end"].values == b["end"].values).all():
            raise PanelDataError(f"{sym} bars are not aligned with the panel grid")
        k = sym.lower()
        out[f"m_{k}_ret_1"] = ct["t_ret_1"].values
        out[f"m_{k}_ret_1d"] = ct["t_ret_1d"].values
        out[f"m_{k}_ret_5d"] = cd["d_ret_5d"].values
        out[f"m_{k}_dist_sma50"] = cd["d_dist_sma50"].values
    bench = relative_strength_benchmark(context)
    q_t, q_d = ctx[bench][1], ctx[bench][2]
    k = bench.lower()
    out[f"m_rs_{k}_1"] = own_tech["t_ret_1"].values - q_t["t_ret_1"].values
    out[f"m_rs_{k}_1d"] = own_tech["t_ret_1d"].values - q_t["t_ret_1d"].values
    out[f"m_rs_{k}_5d"] = own_daily["d_ret_5d"].values - q_d["d_ret_5d"].values
    out[f"m_rs_{k}_20d"] = own_daily["d_ret_20d"].values - q_d["d_ret_20d"].values
    return out


def relative_strength_benchmark(context: Sequence[str]) -> str:
    """The context symbol the ``m_rs_*`` columns are measured against: QQQ when present (the default context), else
    the first context symbol."""
    return "QQQ" if "QQQ" in context else context[0]


# ------------------------------------------------------------------ point-in-time fundamentals
def _load_json(cache_root: Path, kind: str, symbol: str) -> list:
    p = Path(cache_root) / "fmp_history" / f"{kind}__{symbol}.json"
    if not p.exists():
        raise PanelDataError(f"no cached FMP {kind} for {symbol}: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise PanelDataError(f"{p}: expected a list of records")
    return data


def fundamentals_visible_from(kind: str, rec: dict, sessions: Sequence[date]) -> Optional[date]:
    """First session whose OPEN comes after the value was public.

    * quarterly earnings: ``bmo`` -> that session; ``amc``, unknown or blank time -> the next session;
    * annual statements: the session after the SEC acceptance date (``acceptedDate``), so a filing accepted during a
      session is never seen by that session's bars.
    Returns None when the record is not yet public within ``sessions``."""
    import bisect
    if kind == "earnings":
        d = date.fromisoformat(rec["date"][:10])
        same = (rec.get("time") or "").lower() == "bmo"
        i = bisect.bisect_left(sessions, d)
        if i < len(sessions) and sessions[i] == d and same:
            return d
        i = bisect.bisect_right(sessions, d)
    else:
        acc = rec.get("acceptedDate") or rec.get("fillingDate")
        if not acc:
            return None
        d = date.fromisoformat(str(acc)[:10])
        i = bisect.bisect_right(sessions, d)
    return sessions[i] if i < len(sessions) else None


def _clip(x: float, lo: float = -5.0, hi: float = 5.0) -> float:
    return float(min(hi, max(lo, x)))


def fundamentals_table(symbol: str, sessions: Sequence[date], cache_root: Path) -> pd.DataFrame:
    """One row per session (index) with the point-in-time fundamental features of ``symbol``: values carried forward
    from the session they became public, never backwards."""
    sessions = list(sessions)
    events: List[Tuple[date, str, dict]] = []
    for rec in _load_json(cache_root, "past_earnings_quarterly", symbol):
        if rec.get("eps") is None:
            continue                       # announced in the future at cache time, or not reported
        v = fundamentals_visible_from("earnings", rec, sessions)
        if v is not None:
            events.append((v, "earnings", rec))
    for rec in _load_json(cache_root, "income_statement_annual", symbol):
        v = fundamentals_visible_from("annual", rec, sessions)
        if v is not None:
            events.append((v, "annual", rec))
    events.sort(key=lambda e: (e[0], e[1], e[2].get("date", "")))
    earn_hist: List[dict] = []
    annual_hist: List[dict] = []
    rows = {}
    ei = 0
    last_earn_session: Optional[date] = None
    state: Dict[str, float] = {}
    for s in sessions:
        while ei < len(events) and events[ei][0] <= s:
            v, kind, rec = events[ei]
            if kind == "earnings":
                earn_hist.append(rec)
                earn_hist.sort(key=lambda r: r["date"])
                last_earn_session = v
                eps, est = float(rec["eps"]), rec.get("epsEstimated")
                state["f_eps_surprise"] = _clip((eps - float(est)) / max(abs(float(est)), 0.01)) if est is not None else np.nan
                rev, rest = rec.get("revenue"), rec.get("revenueEstimated")
                state["f_rev_surprise"] = (_clip((float(rev) - float(rest)) / float(rest)) if rev and rest else np.nan)
                if len(earn_hist) >= 5:
                    old = earn_hist[-5]
                    state["f_eps_yoy"] = _clip((eps - float(old["eps"])) / max(abs(float(old["eps"])), 0.01))
                    orev = old.get("revenue")
                    state["f_rev_yoy"] = _clip((float(rev) - float(orev)) / float(orev)) if rev and orev else np.nan
                ttm = [r["eps"] for r in earn_hist[-4:]]
                state["_eps_ttm"] = float(sum(ttm)) if len(ttm) == 4 else np.nan
            else:
                annual_hist.append(rec)
                annual_hist.sort(key=lambda r: r["date"])
                rev = float(rec["revenue"]) if rec.get("revenue") else np.nan
                state["f_gross_margin"] = float(rec["grossProfit"]) / rev if rev else np.nan
                state["f_op_margin"] = float(rec["operatingIncome"]) / rev if rev else np.nan
                if len(annual_hist) >= 2 and annual_hist[-2].get("revenue"):
                    state["f_annual_rev_growth"] = _clip(rev / float(annual_hist[-2]["revenue"]) - 1.0)
            ei += 1
        row = {k: v for k, v in state.items()}
        row["f_days_since_earn"] = ((s - last_earn_session).days / 91.0) if last_earn_session else np.nan
        rows[s] = row
    cols = ["f_eps_surprise", "f_rev_surprise", "f_eps_yoy", "f_rev_yoy", "_eps_ttm", "f_gross_margin",
            "f_op_margin", "f_annual_rev_growth", "f_days_since_earn"]
    return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=cols)


def fundamental_features(b: pd.DataFrame, table: pd.DataFrame) -> pd.DataFrame:
    t = table.reindex(b["session"].values)
    t.index = b.index
    out = t.drop(columns=["_eps_ttm"]).copy()
    out["f_earn_yield"] = (t["_eps_ttm"] / b["close"]).clip(-1, 1)    # trailing EPS / price; finite for EPS <= 0
    return out


# ------------------------------------------------------------------ target ingredients
def forward_returns(b: pd.DataFrame, horizons: Iterable[int]) -> pd.DataFrame:
    """``fwd_ret_<N>``: close of the same bar position N sessions later (that session's last bar when it is
    shorter) over this bar's close, minus 1. NaN past the data end. Also ``tgt_idx_<N>``: the row index of that bar."""
    sess = b["session"].values
    uniq, sidx = np.unique(sess, return_inverse=True)
    last_bar = b.groupby("session")["bar"].max().reindex(uniq).values
    pos = {(si, br): i for i, (si, br) in enumerate(zip(sidx, b["bar"].values))}
    out = pd.DataFrame(index=b.index)
    close = b["close"].values
    for n in horizons:
        idx = np.full(len(b), -1, dtype=np.int64)
        for i, (si, br) in enumerate(zip(sidx, b["bar"].values)):
            tj = si + n
            if tj < len(uniq):
                idx[i] = pos[(tj, min(br, last_bar[tj]))]
        fr = np.where(idx >= 0, close[np.maximum(idx, 0)] / close - 1.0, np.nan)
        out[f"fwd_ret_{n}"] = fr
        out[f"tgt_idx_{n}"] = idx
    return out


@dataclass(frozen=True)
class PanelSpec:
    timeframe: str
    first_day: date
    last_day: date
    symbols: Tuple[str, ...] = MAG7
    groups: Tuple[str, ...] = FEATURE_GROUPS
    #: market-context symbols of the ``market`` group (their same-bar returns become ``m_<sym>_*`` columns). SPY is
    #: always loaded as the calendar reference, whether or not it is a context symbol.
    context: Tuple[str, ...] = CONTEXT_SYMBOLS

    def __post_init__(self):
        object.__setattr__(self, "context", tuple(str(c).upper() for c in self.context))
        if len(set(self.context)) != len(self.context):
            raise ValueError(f"duplicate symbols in context {self.context}")
        if "market" in self.groups and not self.context:
            raise ValueError("the market group needs at least one context symbol")
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {sorted(TIMEFRAMES)}")
        bad = set(self.groups) - set(FEATURE_GROUPS)
        if bad:
            raise ValueError(f"unknown feature groups {sorted(bad)}; known {FEATURE_GROUPS}")
        if "technical" not in self.groups:
            raise ValueError("the technical group is required (the model input bars)")


def build_panel(spec: PanelSpec, cache_root: Path) -> Tuple[pd.DataFrame, Dict]:
    """The long panel (one row per symbol and bar) and its meta. Raises PanelDataError on missing cached data."""
    cache_root = Path(cache_root)
    sessions = regular_sessions(spec.first_day, spec.last_day)
    bps = bars_per_session(spec.timeframe)
    ref = load_bars("SPY", spec.timeframe, sessions, cache_root)
    context = tuple(spec.context) if "market" in spec.groups else ()      # only the market group reads them
    loaded = tuple(dict.fromkeys(("SPY",) + context + tuple(spec.symbols)))   # SPY: the calendar
    meta_fills: Dict[str, List[str]] = {}
    per: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]] = {}
    for sym in loaded:
        raw = ref if sym == "SPY" else load_bars(sym, spec.timeframe, sessions, cache_root)
        b, filled = align_to_reference(raw, ref, sym)
        if filled:
            meta_fills[sym] = filled
        tech = technical_features(b, bps)
        dctx, atr_d = daily_context(b, daily_from_bars(b))
        per[sym] = (b, tech, dctx, atr_d)
    ctx = {s: per[s][:3] for s in context}
    sess_list = [d for d in sessions["session"]]
    frames = []
    for sym in spec.symbols:
        b, tech, dctx, atr_d = per[sym]
        parts = [b[["session", "bar", "end", "open", "high", "low", "close", "volume"]], tech, dctx]
        if "market" in spec.groups:
            parts.append(market_features(b, tech, dctx, ctx, spec.context))
        if "fundamentals" in spec.groups:
            parts.append(fundamental_features(b, fundamentals_table(sym, sess_list, cache_root)))
        f = pd.concat(parts, axis=1)
        if "daily" not in spec.groups:
            f = f.drop(columns=[c for c in f.columns if c.startswith("d_")])
        f["atr_d"] = atr_d.values
        f = pd.concat([f, forward_returns(b, FWD_HORIZONS)], axis=1)
        f.insert(0, "symbol", sym)
        f["row_in_symbol"] = np.arange(len(f))
        last = f.groupby("session")["bar"].transform("max")
        f["is_eod"] = f["bar"] == last
        frames.append(f)
    panel = pd.concat(frames, ignore_index=True)
    feats = feature_columns(panel, spec.groups)
    meta = {"timeframe": spec.timeframe, "bars_per_session": bps, "first_day": str(spec.first_day),
            "last_day": str(spec.last_day), "symbols": list(spec.symbols), "context": list(context),
            "groups": list(spec.groups), "features": feats, "fwd_horizons": list(FWD_HORIZONS),
            "filled_bars": meta_fills, "rows": int(len(panel)),
            "source": {s: _fingerprint(_cache_file(cache_root, s)) for s in loaded}}
    return panel, meta


def _splitmix64(x: np.ndarray) -> np.ndarray:
    """One splitmix64 round on a uint64 array (wrapping arithmetic)."""
    with np.errstate(over="ignore"):
        x = x + np.uint64(0x9E3779B97F4A7C15)
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return x ^ (x >> np.uint64(31))


def sample_bars(panel: pd.DataFrame, per_session: int, seed: int) -> pd.DataFrame:
    """Thin an intraday panel for deep-model training: every symbol keeps its end-of-day bar plus ``per_session - 1``
    random intraday bars of each session (all of them when the session has fewer). ``per_session=1`` is the daily
    panel (end-of-day bars only), a large value keeps every row.

    The draw of a bar is a hash of ``(seed, symbol, session, bar)``, never of the panel's size or order, so it is
    deterministic and unchanged when the panel is extended or cut: the same bars are drawn for a session whatever
    else the panel holds. Rows keep their original index (the ``row_in_symbol`` / positional row of the full panel,
    which sequence windows are cut from) and order. Only training rows should be sampled: windows and evaluation
    still read every bar of the full panel."""
    if isinstance(per_session, bool) or not isinstance(per_session, (int, np.integer)) or per_session < 1:
        raise ValueError(f"per_session must be an integer >= 1, got {per_session!r}")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError(f"seed must be a non-negative integer, got {seed!r}")
    import zlib
    sym_code = {s: zlib.crc32(str(s).encode()) for s in panel["symbol"].unique()}
    day = pd.to_datetime(panel["session"]).to_numpy("datetime64[D]").astype(np.int64).astype(np.uint64)
    h = np.full(len(panel), (int(seed) * 0x2545F4914F6CDD1D) % (1 << 64), dtype=np.uint64)
    for col in (panel["symbol"].map(sym_code).to_numpy(np.uint64), day, panel["bar"].to_numpy(np.int64).astype(np.uint64)):
        h = _splitmix64(h ^ col)
    u = (h >> np.uint64(11)).astype(np.float64) / float(1 << 53)          # uniform [0, 1)
    key = np.where(panel["is_eod"].to_numpy(bool), -1.0, u)               # the end-of-day bar always ranks first
    rank = pd.Series(key, index=panel.index).groupby([panel["symbol"], panel["session"]], sort=False).rank(method="first")
    return panel[(rank <= per_session).to_numpy()]


#: calendar days loaded before ``first_day`` so the 200-session average and the indicators are warm on day one
WARMUP_CALENDAR_DAYS = 320


def ticker_frame(symbol: str, timeframe: str, first_day: date, last_day: date, groups: Iterable[str],
                 cache_root: Path) -> pd.DataFrame:
    """One symbol's bars and features for the dataset wizard: index ``Date`` = bar END (ET, naive), columns
    Open/High/Low/Close/Volume plus the feature columns of ``groups``. No target ingredient is included (labels are
    computed at training time by ``ml_targets.labels_for_frame``). Rows before ``first_day`` (warm-up) are dropped;
    a feature still NaN after the warm-up raises."""
    spec = PanelSpec(timeframe, first_day - timedelta(days=WARMUP_CALENDAR_DAYS), last_day, (symbol.upper(),),
                     tuple(groups))
    panel, meta = build_panel(spec, cache_root)
    feats = meta["features"]
    out = panel[pd.to_datetime(panel["session"]) >= pd.Timestamp(first_day)]
    bad = [c for c in feats if out[c].isna().any()]
    if bad:
        raise PanelDataError(f"{symbol}: features still NaN after the warm-up: {bad[:6]}")
    df = out[["end", "open", "high", "low", "close", "volume"] + feats].rename(
        columns={"end": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df.set_index("Date")


def feature_columns(panel: pd.DataFrame, groups: Iterable[str]) -> List[str]:
    prefixes = tuple(GROUP_PREFIX[g] for g in groups)
    return [c for c in panel.columns if c.startswith(prefixes)]


def _fingerprint(p: Path) -> Dict[str, object]:
    st = p.stat()
    return {"size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
