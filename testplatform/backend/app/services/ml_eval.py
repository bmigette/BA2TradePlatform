"""Track metrics for the ML exploration v2 grid: how good are out-of-sample signal scores?

Input is a predictions frame with one row per (symbol, session): ``score`` (higher = more bullish) joined to the
realised outcome of the trade the score would have opened, all supplied by the caller (this module reads nothing):

* ``ret``     the trade return from the NEXT session's open to the close ``H`` sessions later (a fraction);
* ``rel_ret`` ``ret`` minus the session's universe mean (the cross-sectional excess);
* ``vol``     ``ATR% x sqrt(H)``, the volatility unit that makes names comparable.

``symbol`` / ``session`` may be columns or index levels. Rows with a NaN score or outcome (the tail of the data, a
feature not yet available) are dropped by every metric, never filled.

Cost convention (one number everywhere): ``cost_bps`` is the cost of ONE ROUND TRIP of one position (entry and exit),
in basis points of its notional. A leg of a cross-sectional spread pays it once per session, so a long-short spread
pays ``2 x cost``; a timing entry pays ``cost``. Costs enter in volatility units, ``cost / vol`` of the row.

Overlap: a trade lasts ``H`` sessions, so consecutive daily metrics overlap. ``newey_west_t`` is the t-statistic that
accounts for it (pass lag >= H - 1); the naive t of the same series is much too large.

Timing thresholds must be causal. ``expanding_cutoff`` gives each row the ``q``-quantile of the scores BEFORE it, a
cutoff a live trader could have used; a cutoff taken from the whole block's own scores looks ahead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from app.services import ml_walkforward as W

MIN_NAMES = 5                    # a session needs this many scored names for a cross-sectional statistic
PERIODS_PER_YEAR = 252
_KEYS = ("symbol", "session")


# ------------------------------------------------------------------ frame handling
def flat(df: pd.DataFrame, need: Sequence[str] = ()) -> pd.DataFrame:
    """``df`` with ``symbol`` and ``session`` as plain columns (moved out of the index when needed), the session as
    datetime64, sorted by (session, symbol) so every tie-break and permutation is deterministic. Refuses missing
    columns and duplicated (symbol, session) rows."""
    out = df
    idx_names = [n for n in out.index.names if n is not None]
    if idx_names:
        out = out.reset_index(level=idx_names)
    out = out.copy()
    missing = [c for c in (*_KEYS, *need) if c not in out.columns]
    if missing:
        raise ValueError(f"predictions frame lacks columns {missing}")
    out["session"] = pd.to_datetime(out["session"])
    if out.duplicated(list(_KEYS)).any():
        raise ValueError("duplicated (symbol, session) rows")
    return out.sort_values(["session", "symbol"], kind="mergesort").reset_index(drop=True)


def _valid(df: pd.DataFrame, cols: Sequence[str]) -> pd.DataFrame:
    d = df.dropna(subset=list(cols))
    if "vol" in cols and (d["vol"] <= 0).any():
        raise ValueError("vol must be > 0")
    return d


# ------------------------------------------------------------------ rank IC
def rank_ic(df: pd.DataFrame, min_names: int = MIN_NAMES) -> Tuple[float, pd.Series]:
    """Mean over sessions of the cross-sectional Spearman correlation of ``score`` with ``rel_ret``, and the daily
    series (indexed by session). Sessions with fewer than ``min_names`` names, or a constant score or outcome (no
    rank information), are skipped. ``(nan, empty series)`` when no session qualifies."""
    d = _valid(flat(df, ("score", "rel_ret")), ("score", "rel_ret"))
    g = d.groupby("session", sort=True)
    d = d[g["score"].transform("size") >= min_names]
    g = d.groupby("session", sort=True)
    a = g["score"].rank(method="average")
    b = g["rel_ret"].rank(method="average")
    a = a - a.groupby(d["session"]).transform("mean")
    b = b - b.groupby(d["session"]).transform("mean")
    num = (a * b).groupby(d["session"]).sum()
    den = np.sqrt((a * a).groupby(d["session"]).sum() * (b * b).groupby(d["session"]).sum())
    s = (num / den.where(den > 0)).dropna().rename("rank_ic")
    return (float(s.mean()) if len(s) else float("nan")), s


# ------------------------------------------------------------------ quintile spread
@dataclass(frozen=True)
class Spread:
    """``mean`` / ``series``: top quintile minus bottom quintile of rel_ret/vol, after ``2 x cost`` (a round trip per
    leg). ``long_mean`` / ``long_series``: the long-only variant, the top quintile (after one round trip) minus the
    session's all-names mean of rel_ret/vol. Both in volatility units per trade."""
    mean: float
    series: pd.Series
    long_mean: float
    long_series: pd.Series

    def __iter__(self):          # (mean, series) unpacking, as rank_ic
        return iter((self.mean, self.series))


def quintile_spread(df: pd.DataFrame, cost_bps: float = 0.0, min_names: int = MIN_NAMES) -> Spread:
    """Daily top-minus-bottom quintile spread by ``score``.

    Per session with at least ``min_names`` names, ``k = n // 5`` names form each end (rank ties broken by symbol, so
    the result is deterministic). The long leg earns ``(rel_ret - c) / vol`` per name and the short leg
    ``-(rel_ret + c) / vol`` with ``c = cost_bps / 1e4``: cost lowers the spread by ``c / vol`` per leg, i.e.
    exactly ``2c / vol`` for a uniform vol."""
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    c = cost_bps / 1e4
    d = _valid(flat(df, ("score", "rel_ret", "vol")), ("score", "rel_ret", "vol"))
    n = d.groupby("session")["score"].transform("size")
    d = d[n >= min_names].copy()
    n = d.groupby("session")["score"].transform("size")
    k = n // 5
    r = d.groupby("session")["score"].rank(method="first")
    d["x"] = d["rel_ret"] / d["vol"]
    d["cost"] = c / d["vol"]
    top, bot = r > (n - k), r <= k
    long_leg = (d["x"] - d["cost"])[top].groupby(d["session"][top]).mean()
    short_leg = (d["x"] + d["cost"])[bot].groupby(d["session"][bot]).mean()
    spread = (long_leg - short_leg).dropna().rename("quintile_spread")
    universe = d.groupby("session")["x"].mean()
    long_only = (long_leg - universe.reindex(long_leg.index)).dropna().rename("long_only")
    return Spread(float(spread.mean()) if len(spread) else float("nan"), spread,
                  float(long_only.mean()) if len(long_only) else float("nan"), long_only)


# ------------------------------------------------------------------ timing
def expanding_cutoff(score: pd.Series, q: float, min_obs: int = 20) -> pd.Series:
    """Causal per-row cutoff: the ``q``-quantile of ``score``'s rows strictly BEFORE each row (in the given order,
    which must be time order), NaN until ``min_obs`` earlier scores exist. Use per symbol (or per instrument) and
    reset per block; it never looks at the row itself or any later row."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    return score.shift(1).expanding(min_periods=min_obs).quantile(q)


def _with_cutoff(df: pd.DataFrame, threshold: Union[float, str, pd.Series, np.ndarray], need: Sequence[str]
                 ) -> pd.DataFrame:
    """``flat`` frame with the cutoff in column ``_cut``. A per-row array is attached BEFORE ``flat`` re-sorts the
    rows, so it pairs with the rows in the order the caller gave them."""
    d = df
    if isinstance(threshold, str):
        if threshold not in d.columns:
            raise ValueError(f"threshold column {threshold!r} not in the frame")
        d = d.assign(_cut=d[threshold].to_numpy(dtype=float))
    elif np.isscalar(threshold):
        d = d.assign(_cut=float(threshold))
    else:
        arr = np.asarray(threshold, dtype=float)
        if len(arr) != len(d):
            raise ValueError("a per-row threshold must have one value per row")
        d = d.assign(_cut=arr)
    return flat(d, (*need, "_cut"))


@dataclass(frozen=True)
class Timing:
    """``excess``: mean of ret/vol over the in rows, net of one round trip each, minus the mean of ret/vol over all
    rows. ``series``: per session, the mean over its rows of (in x net ret/vol - ret/vol), the in/out strategy's
    excess over always being in, for overlap-corrected inference."""
    excess: float
    mean_in: float
    mean_all: float
    n_in: int
    n: int
    in_share: float
    series: pd.Series


def timing_excess(df: pd.DataFrame, threshold, cost_bps: float = 0.0) -> Timing:
    """Does being in only when ``score >= threshold`` beat being always in?

    ``threshold``: a score level (float), a column name, or one cutoff per row (see ``expanding_cutoff``). Rows
    without a cutoff are excluded. Every in row opens its own trade (next open to ``H`` sessions later), so each pays
    one round trip ``cost_bps / 1e4`` (in vol units, ``/ vol``). A frame holding one instrument gives that
    instrument's timing value; for a panel use ``timing_by_symbol`` or pool deliberately."""
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    d = _valid(_with_cutoff(df, threshold, ("score", "ret", "vol")), ("score", "ret", "vol", "_cut"))
    if d.empty:
        return Timing(float("nan"), float("nan"), float("nan"), 0, 0, float("nan"), pd.Series(dtype=float))
    c = cost_bps / 1e4
    x = d["ret"] / d["vol"]
    inn = (d["score"] >= d["_cut"]).to_numpy()
    net = (d["ret"] - c) / d["vol"]
    mean_in = float(net[inn].mean()) if inn.any() else float("nan")
    mean_all = float(x.mean())
    per_row = np.where(inn, net, 0.0) - x
    series = per_row.groupby(d["session"]).mean().rename("timing_excess")
    return Timing(mean_in - mean_all if inn.any() else float("nan"), mean_in, mean_all, int(inn.sum()), len(d),
                  float(inn.mean()), series)


def timing_by_symbol(df: pd.DataFrame, threshold, cost_bps: float = 0.0) -> pd.DataFrame:
    """``timing_excess`` per symbol: a frame indexed by symbol with excess, mean_in, mean_all, n_in, n, in_share."""
    d = _with_cutoff(df, threshold, ("score", "ret", "vol"))
    rows = {}
    for sym, g in d.groupby("symbol", sort=True):
        t = timing_excess(g, "_cut", cost_bps)
        rows[sym] = {"excess": t.excess, "mean_in": t.mean_in, "mean_all": t.mean_all, "n_in": t.n_in, "n": t.n,
                     "in_share": t.in_share}
    return pd.DataFrame.from_dict(rows, orient="index")


def timing_sharpe(df: pd.DataFrame, threshold, cost_bps: float = 0.0, horizon: int = 1,
                  periods_per_year: int = PERIODS_PER_YEAR) -> float:
    """Annualised Sharpe of the in/out strategy: position 1 when ``score >= threshold`` else 0, each in row paying one
    round trip. The per-session return is the mean over the session's rows of ``in x (ret - cost)`` (an equal-weight
    book over the instruments in the frame; one instrument = its own strategy). Each return is an ``horizon``-session
    trade, so the per-trade Sharpe is annualised by ``sqrt(periods_per_year / horizon)``. NaN when the strategy never
    trades or has no variance."""
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    d = _with_cutoff(df, threshold, ("score", "ret")).dropna(subset=["score", "ret", "_cut"])
    if d.empty:
        return float("nan")
    inn = (d["score"] >= d["_cut"]).to_numpy()
    r = pd.Series(np.where(inn, d["ret"] - cost_bps / 1e4, 0.0), index=d.index).groupby(d["session"]).mean()
    sd = r.std(ddof=1)
    if not inn.any() or not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year / horizon))


# ------------------------------------------------------------------ inference
def newey_west_se(series, lag: int) -> float:
    """Standard error of the mean of ``series`` with a Bartlett-kernel long-run variance:
    ``LRV = g0 + 2 sum_{l=1..lag} (1 - l/(lag+1)) g_l``, ``se = sqrt(LRV / n)``, ``g_l`` the autocovariance at lag
    ``l`` (divisor n). NaNs are dropped. Refuses a series not longer than ``lag + 1``."""
    x = pd.Series(series, dtype=float).dropna().to_numpy()
    n = len(x)
    if lag < 0:
        raise ValueError("lag must be >= 0")
    if n <= lag + 1:
        raise ValueError(f"series of {n} values is too short for lag {lag}")
    dev = x - x.mean()
    lrv = float(dev @ dev) / n
    for l in range(1, lag + 1):
        lrv += 2.0 * (1.0 - l / (lag + 1.0)) * float(dev[l:] @ dev[:-l]) / n
    return math.sqrt(max(lrv, 0.0) / n)


def newey_west_t(series, lag: int) -> float:
    """Overlap-corrected t-statistic of the mean of ``series`` (Bartlett kernel). For H-session trades opened daily,
    use ``lag >= H - 1``. NaN when the series has no variance."""
    x = pd.Series(series, dtype=float).dropna()
    se = newey_west_se(x, lag)
    return float(x.mean() / se) if se > 0 else float("nan")


# ------------------------------------------------------------------ regimes
def by_regime(data: Union[pd.Series, pd.DataFrame], fn: Callable[[Any], float]) -> Dict[str, float]:
    """``fn`` over everything (key ``"all"``) and over each market regime present (``ml_walkforward.regime`` of the
    session). ``data`` is a Series indexed by session (a daily metric series) or a predictions frame with a
    ``session`` column or index level; ``fn`` receives the matching slice and returns a number. Regimes in time
    order, only those with rows."""
    if isinstance(data, pd.Series):
        sessions = pd.to_datetime(data.index.get_level_values("session") if "session" in data.index.names
                                  else data.index)
    else:
        if "session" in data.columns:
            sessions = pd.to_datetime(data["session"])
        elif "session" in data.index.names:
            sessions = pd.to_datetime(data.index.get_level_values("session"))
        else:
            raise ValueError("data has no session column or index level")
    names = W.regimes(np.asarray(sessions))
    out: Dict[str, float] = {"all": fn(data)}
    for r in W.REGIMES:
        m = names == r
        if m.any():
            out[r] = fn(data[m])
    return out


# ------------------------------------------------------------------ ready-made scalar metrics (for by_regime / controls)
def rank_ic_metric(min_names: int = MIN_NAMES) -> Callable[[pd.DataFrame], float]:
    return lambda d: rank_ic(d, min_names)[0]


def spread_metric(cost_bps: float = 0.0, long_only: bool = False, min_names: int = MIN_NAMES
                  ) -> Callable[[pd.DataFrame], float]:
    def fn(d: pd.DataFrame) -> float:
        s = quintile_spread(d, cost_bps, min_names)
        return s.long_mean if long_only else s.mean
    return fn


def timing_metric(threshold, cost_bps: float = 0.0) -> Callable[[pd.DataFrame], float]:
    """``threshold`` must be a level or a column name (it has to survive being sliced by regime)."""
    if not isinstance(threshold, str) and not np.isscalar(threshold):
        raise ValueError("timing_metric takes a score level or a column name")
    return lambda d: timing_excess(d, threshold, cost_bps).excess
