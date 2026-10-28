"""Track metrics for the ML exploration v2 grid: how good are out-of-sample signal scores?

Input is a predictions frame with one row per (symbol, session): ``score`` (higher = more bullish) joined to the
realised outcome of the trade the score would have opened, all supplied by the caller (this module reads nothing):

* ``ret``     the trade return from the NEXT session's open to the close ``H`` sessions later (a fraction);
* ``rel_ret`` ``ret`` minus the session's universe mean (the cross-sectional excess);
* ``vol``     ``ATR% x sqrt(H)``, the volatility unit that makes names comparable.

``symbol`` / ``session`` may be columns or index levels. Rows with a NaN score or outcome (the tail of the data, a
feature not yet available) are dropped by every metric, never filled. ``assume_flat=True`` skips the normalisation
(``flat``) for a caller that already holds a frame with ``symbol`` / ``session`` columns, ``session`` as datetime64,
rows sorted by (session, symbol) and no duplicate keys (``controls_report`` hands such frames to its metric).

Cost convention (one number everywhere): ``cost_bps`` is the cost of ONE ROUND TRIP of one position (entry and exit),
in basis points of its notional.
* Cross-sectional spread: each leg pays it once per session, so a long-short spread pays ``2 x cost`` and a
  long-only top quintile pays ``cost``. In volatility units: ``cost / vol`` of the row.
* Timing: every "in" row opens its own trade and pays ``cost``; ``excess`` compares those NET trades with the
  unconditional mean of ``ret / vol`` over all rows, which is deliberately gross (the average opportunity, before any
  trading) so that the comparison is the conservative "does conditioning beat always being available".
  ``timing_sharpe`` charges the same ``cost`` per in row, in return units.

Overlap: a trade lasts ``H`` sessions, so consecutive daily metrics overlap. ``newey_west_t`` is the t-statistic that
accounts for it. Its ``lag`` counts ROWS of the series passed (sessions for the daily series here, not calendar days),
so pass ``lag >= H - 1`` for H-session trades; a slice shorter than ``lag + 2`` rows gives NaN, not an error. The
naive t of the same series is much too large.

Timing cutoffs are causal. ``panel_expanding_cutoff`` gives each row, per symbol, the ``q``-quantile of that symbol's
scores strictly BEFORE it (optionally warmed up with earlier blocks' scores), a cutoff a live trader could have used;
a cutoff taken from the block's own scores would look ahead. ``timing_excess`` / ``timing_sharpe`` take ``q=`` (and
compute that cutoff from the frame's own score) or ``cutoff_col=`` (a column the caller prepared, e.g. with
``with_cutoff``); never a bare level. Order of operations when controls and regimes are involved:
shuffle the score -> compute the cutoff from the shuffled score -> evaluate -> ``by_regime``. A metric built with
``timing_metric(q=...)`` recomputes the cutoff on every frame it is given, so ``controls_report`` applies it after
each shuffle. ``by_regime`` slices first, so a ``q=`` metric restarts its cutoff inside each regime slice (first
``min_obs`` rows lost); to keep full-history cutoffs, add the column with ``with_cutoff`` on the whole frame and use
``timing_metric(cutoff_col=...)`` per regime (for a shuffled frame, call ``with_cutoff`` after the shuffle).
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
MIN_OBS = 20                     # scores a symbol needs before its causal cutoff exists
_KEYS = ("symbol", "session")


# ------------------------------------------------------------------ frame handling
def flat(df: pd.DataFrame, need: Sequence[str] = (), assume_flat: bool = False) -> pd.DataFrame:
    """``df`` with ``symbol`` and ``session`` as plain columns (moved out of the index when needed), the session as
    datetime64, sorted by (session, symbol) so every tie-break and permutation is deterministic. Refuses missing
    columns and duplicated (symbol, session) rows. With ``assume_flat`` the frame is returned as it is (columns are
    still checked): the caller guarantees the layout described in the module docstring."""
    if assume_flat:
        missing = [c for c in (*_KEYS, *need) if c not in df.columns]
        if missing:
            raise ValueError(f"predictions frame lacks columns {missing}")
        return df
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
def rank_ic(df: pd.DataFrame, min_names: int = MIN_NAMES, assume_flat: bool = False) -> Tuple[float, pd.Series]:
    """Mean over sessions of the cross-sectional Spearman correlation of ``score`` with ``rel_ret``, and the daily
    series (indexed by session). Sessions with fewer than ``min_names`` names, or a constant score or outcome (no
    rank information), are skipped. ``(nan, empty series)`` when no session qualifies."""
    d = _valid(flat(df, ("score", "rel_ret"), assume_flat), ("score", "rel_ret"))
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
@dataclass(frozen=True, eq=False)
class Spread:
    """``mean`` / ``series``: top quintile minus bottom quintile of rel_ret/vol, after ``2 x cost`` (a round trip per
    leg). ``long_mean`` / ``long_series``: the long-only variant, the top quintile (after one round trip) minus the
    session's all-names mean of rel_ret/vol. Both in volatility units per trade. Identity equality (it holds Series)."""
    mean: float
    series: pd.Series
    long_mean: float
    long_series: pd.Series

    def __iter__(self):          # (mean, series) unpacking, as rank_ic
        return iter((self.mean, self.series))


def quintile_spread(df: pd.DataFrame, cost_bps: float = 0.0, min_names: int = MIN_NAMES,
                    assume_flat: bool = False) -> Spread:
    """Daily top-minus-bottom quintile spread by ``score``.

    Per session with at least ``min_names`` names, ``k = n // 5`` names form each end (rank ties broken by symbol, so
    the result is deterministic). The long leg earns ``(rel_ret - c) / vol`` per name and the short leg
    ``-(rel_ret + c) / vol`` with ``c = cost_bps / 1e4``: cost lowers the spread by ``c / vol`` per leg, i.e.
    exactly ``2c / vol`` for a uniform vol."""
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    c = cost_bps / 1e4
    d = _valid(flat(df, ("score", "rel_ret", "vol"), assume_flat), ("score", "rel_ret", "vol"))
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


# ------------------------------------------------------------------ causal cutoffs
def _key_frame(df: pd.DataFrame, where: str) -> pd.DataFrame:
    """(symbol, session, score) of ``df`` by position (index untouched), session as datetime64; refuses duplicates."""
    idx_names = [n for n in df.index.names if n is not None]
    d = df.reset_index(level=idx_names) if idx_names else df
    missing = [c for c in (*_KEYS, "score") if c not in d.columns]
    if missing:
        raise ValueError(f"{where} lacks columns {missing}")
    out = pd.DataFrame({"symbol": d["symbol"].to_numpy(), "session": pd.to_datetime(d["session"]).to_numpy(),
                        "score": d["score"].to_numpy(dtype=float)})
    if out.duplicated(["symbol", "session"]).any():
        raise ValueError(f"{where}: duplicated (symbol, session) rows")
    return out


def panel_expanding_cutoff(df: pd.DataFrame, q: float, min_obs: int = MIN_OBS,
                           history: Optional[pd.DataFrame] = None) -> pd.Series:
    """Causal per-row timing cutoff: for each row, the ``q``-quantile of THAT SYMBOL's scores in strictly earlier
    sessions (never the row itself or a later one), NaN until ``min_obs`` earlier scores exist. Returned as a Series
    aligned to ``df.index`` (rows may come in any order).

    ``history``: a frame of earlier scores (``symbol``, ``session``, ``score``, e.g. the previous blocks' predictions)
    that warms the cutoff up so each block does not lose its first ``min_obs`` rows. It must end before ``df``
    starts; overlap is refused, because a later score would then leak into an earlier cutoff."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    if not _is_pos_int(min_obs):
        raise ValueError("min_obs must be an integer >= 1")
    work = _key_frame(df, "frame")
    work["_pos"] = np.arange(len(work))
    parts = [work]
    if history is not None and len(history):
        h = _key_frame(history, "history")
        h["_pos"] = -1
        if len(work) and h["session"].max() >= work["session"].min():
            raise ValueError("history must end before the frame starts (it would leak later scores into earlier cutoffs)")
        parts.insert(0, h)
    allr = pd.concat(parts, ignore_index=True).sort_values(["symbol", "session"], kind="mergesort")
    allr["cut"] = allr.groupby("symbol", sort=False)["score"].transform(
        lambda s: s.shift(1).expanding(min_periods=min_obs).quantile(q))
    mine = allr[allr["_pos"] >= 0].sort_values("_pos")
    return pd.Series(mine["cut"].to_numpy(), index=df.index, name="cutoff")


def with_cutoff(df: pd.DataFrame, q: float, min_obs: int = MIN_OBS, history: Optional[pd.DataFrame] = None,
                col: str = "cutoff", assume_flat: bool = False) -> pd.DataFrame:
    """The flat frame plus the causal cutoff column ``col`` (see ``panel_expanding_cutoff``), for
    ``timing_*(cutoff_col=col)``. Compute it AFTER any score shuffle and BEFORE slicing by regime."""
    d = flat(df, ("score",), assume_flat)
    out = d.assign(**{col: panel_expanding_cutoff(d, q, min_obs, history).to_numpy()})
    return out


def _is_pos_int(v: Any) -> bool:
    return isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_)) and v >= 1


def _cutoffs(d: pd.DataFrame, q: Optional[float], cutoff_col: Optional[str], min_obs: int,
             history: Optional[pd.DataFrame]) -> np.ndarray:
    if (q is None) == (cutoff_col is None):
        raise ValueError("pass exactly one of q= (causal cutoff computed here) and cutoff_col= (a prepared column)")
    if cutoff_col is not None:
        if cutoff_col not in d.columns:
            raise ValueError(f"cutoff column {cutoff_col!r} is not in the frame")
        return d[cutoff_col].to_numpy(dtype=float)
    return panel_expanding_cutoff(d, q, min_obs, history).to_numpy()


def _timing_frame(df, need, cost_bps, q, cutoff_col, min_obs, history, assume_flat) -> pd.DataFrame:
    """Valid rows with a cutoff, plus ``_in`` (score >= cutoff)."""
    if cost_bps < 0:
        raise ValueError("cost_bps must be >= 0")
    d = flat(df, need, assume_flat)
    d = d.assign(_cut=_cutoffs(d, q, cutoff_col, min_obs, history))
    d = _valid(d, (*need, "_cut"))
    return d.assign(_in=(d["score"] >= d["_cut"]).to_numpy())


# ------------------------------------------------------------------ timing
@dataclass(frozen=True, eq=False)
class Timing:
    """``excess``: mean of the net trade value ``(ret - cost) / vol`` over the in rows minus the mean of
    ``ret / vol`` over all rows. ``series``: a per-session series whose mean IS ``excess`` exactly, for overlap-
    corrected inference: each row contributes ``in x (net - mean_all) / p_in`` (``p_in`` = the overall in-share), the
    contributions are summed per session and scaled by ``sessions / rows`` (so unequal numbers of names per session
    cannot change the mean). Under a score unrelated to returns its expectation is 0 whatever the market drift.
    Identity equality (it holds a Series)."""
    excess: float
    mean_in: float
    mean_all: float
    n_in: int
    n: int
    in_share: float
    series: pd.Series


def timing_excess(df: pd.DataFrame, cost_bps: float = 0.0, *, q: Optional[float] = None,
                  cutoff_col: Optional[str] = None, min_obs: int = MIN_OBS,
                  history: Optional[pd.DataFrame] = None, assume_flat: bool = False) -> Timing:
    """Does being in only when ``score >= cutoff`` beat the unconditional average opportunity?

    The cutoff is causal: ``q=`` (the per-symbol expanding quantile of earlier scores, ``panel_expanding_cutoff``,
    rows without ``min_obs`` earlier scores are excluded) or ``cutoff_col=`` (a prepared column). Every in row opens
    its own trade (next open to ``H`` sessions later) and pays one round trip ``cost_bps / 1e4`` (see the module
    docstring for the cost convention). A frame holding one instrument gives that instrument's timing value; for a
    panel use ``timing_by_symbol`` or pool deliberately. ``excess`` is NaN when the strategy never trades."""
    d = _timing_frame(df, ("score", "ret", "vol"), cost_bps, q, cutoff_col, min_obs, history, assume_flat)
    if d.empty:
        return Timing(float("nan"), float("nan"), float("nan"), 0, 0, float("nan"), pd.Series(dtype=float))
    c = cost_bps / 1e4
    x = (d["ret"] / d["vol"]).to_numpy()
    net = ((d["ret"] - c) / d["vol"]).to_numpy()
    inn = d["_in"].to_numpy()
    n, n_in = len(d), int(inn.sum())
    mean_all = float(x.mean())
    if n_in == 0:
        return Timing(float("nan"), float("nan"), mean_all, 0, n, 0.0, pd.Series(dtype=float))
    p_in = n_in / n
    mean_in = float(net[inn].mean())
    z = pd.Series(np.where(inn, (net - mean_all) / p_in, 0.0), index=d.index)
    sess = d["session"]
    series = (z.groupby(sess).sum() * (sess.nunique() / n)).rename("timing_excess")
    return Timing(mean_in - mean_all, mean_in, mean_all, n_in, n, p_in, series)


def timing_by_symbol(df: pd.DataFrame, cost_bps: float = 0.0, *, q: Optional[float] = None,
                     cutoff_col: Optional[str] = None, min_obs: int = MIN_OBS,
                     history: Optional[pd.DataFrame] = None, assume_flat: bool = False) -> pd.DataFrame:
    """``timing_excess`` per symbol (same cutoff rules): a frame indexed by symbol with excess, mean_in, mean_all,
    n_in, n, in_share. One groupby, no per-symbol loop. Symbols with no valid row are absent."""
    d = _timing_frame(df, ("score", "ret", "vol"), cost_bps, q, cutoff_col, min_obs, history, assume_flat)
    c = cost_bps / 1e4
    g = d.assign(_x=d["ret"] / d["vol"], _net=((d["ret"] - c) / d["vol"]).where(d["_in"])).groupby("symbol", sort=True)
    out = pd.DataFrame({"n": g.size(), "n_in": g["_in"].sum().astype(int), "mean_all": g["_x"].mean(),
                        "mean_in": g["_net"].mean()})
    out["in_share"] = out["n_in"] / out["n"]
    out["excess"] = out["mean_in"] - out["mean_all"]
    return out[["excess", "mean_in", "mean_all", "n_in", "n", "in_share"]]


def timing_sharpe(df: pd.DataFrame, horizon: int, cost_bps: float = 0.0, *, q: Optional[float] = None,
                  cutoff_col: Optional[str] = None, min_obs: int = MIN_OBS,
                  history: Optional[pd.DataFrame] = None, periods_per_year: int = PERIODS_PER_YEAR,
                  assume_flat: bool = False) -> float:
    """Annualised Sharpe of the in/out strategy: position 1 when ``score >= cutoff`` (causal, as ``timing_excess``)
    else 0, each in row paying one round trip. The per-session return is the mean over the session's rows of
    ``in x (ret - cost)`` (an equal-weight book over the instruments in the frame; one instrument = its own
    strategy). Each return is an ``horizon``-session trade (``horizon`` is required: it sets the annualisation
    ``sqrt(periods_per_year / horizon)``). NaN when the strategy never trades or has no variance."""
    if not _is_pos_int(horizon):
        raise ValueError("horizon must be an integer >= 1")
    d = _timing_frame(df, ("score", "ret"), cost_bps, q, cutoff_col, min_obs, history, assume_flat)
    if d.empty:
        return float("nan")
    inn = d["_in"].to_numpy()
    r = pd.Series(np.where(inn, d["ret"].to_numpy() - cost_bps / 1e4, 0.0), index=d.index).groupby(d["session"]).mean()
    sd = r.std(ddof=1)
    if not inn.any() or not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year / horizon))


# ------------------------------------------------------------------ inference
def newey_west_se(series, lag: int) -> float:
    """Standard error of the mean of ``series`` with a Bartlett-kernel long-run variance:
    ``LRV = g0 + 2 sum_{l=1..lag} (1 - l/(lag+1)) g_l``, ``se = sqrt(LRV / n)``, ``g_l`` the autocovariance at lag
    ``l`` (divisor n). NaNs are dropped, then ``lag`` counts the remaining ROWS. NaN (not an error) when the series
    has ``n <= lag + 1`` values: a short block or regime slice has no estimate."""
    if lag < 0:
        raise ValueError("lag must be >= 0")
    x = pd.Series(series, dtype=float).dropna().to_numpy()
    n = len(x)
    if n <= lag + 1:
        return float("nan")
    dev = x - x.mean()
    lrv = float(dev @ dev) / n
    for l in range(1, lag + 1):
        lrv += 2.0 * (1.0 - l / (lag + 1.0)) * float(dev[l:] @ dev[:-l]) / n
    return math.sqrt(max(lrv, 0.0) / n)


def newey_west_t(series, lag: int) -> float:
    """Overlap-corrected t-statistic of the mean of ``series`` (Bartlett kernel; ``lag`` counts rows, use
    ``lag >= H - 1`` for H-session trades opened daily). NaN when the series is too short (``n <= lag + 1``) or has
    no variance."""
    x = pd.Series(series, dtype=float).dropna()
    se = newey_west_se(x, lag)
    return float(x.mean() / se) if np.isfinite(se) and se > 0 else float("nan")


# ------------------------------------------------------------------ regimes
def by_regime(data: Union[pd.Series, pd.DataFrame], fn: Callable[[Any], float], min_rows: int = 1
              ) -> Dict[str, float]:
    """``fn`` over everything (key ``"all"``) and over each market regime present (``ml_walkforward.regime`` of the
    session). ``data`` is a Series indexed by session (a daily metric series) or a predictions frame with a
    ``session`` column or index level; ``fn`` receives the matching slice and returns a number. A slice with fewer
    than ``min_rows`` rows gives NaN without calling ``fn`` (thin regimes produce noise, not estimates). Regimes in
    time order, only those with rows. The data is sliced BEFORE ``fn`` runs, so anything ``fn`` computes from the
    slice (a causal cutoff, a Newey-West lag over the slice's rows) restarts inside each regime."""
    if not _is_pos_int(min_rows):
        raise ValueError("min_rows must be an integer >= 1")
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
    out: Dict[str, float] = {"all": fn(data) if len(data) >= min_rows else float("nan")}
    for r in W.REGIMES:
        m = names == r
        if m.any():
            out[r] = fn(data[m]) if int(m.sum()) >= min_rows else float("nan")
    return out


# ------------------------------------------------------------------ ready-made scalar metrics (for by_regime / controls)
def rank_ic_metric(min_names: int = MIN_NAMES, assume_flat: bool = False) -> Callable[[pd.DataFrame], float]:
    return lambda d: rank_ic(d, min_names, assume_flat)[0]


def spread_metric(cost_bps: float = 0.0, long_only: bool = False, min_names: int = MIN_NAMES,
                  assume_flat: bool = False) -> Callable[[pd.DataFrame], float]:
    def fn(d: pd.DataFrame) -> float:
        s = quintile_spread(d, cost_bps, min_names, assume_flat)
        return s.long_mean if long_only else s.mean
    return fn


def timing_metric(q: Optional[float] = None, cost_bps: float = 0.0, cutoff_col: Optional[str] = None,
                  min_obs: int = MIN_OBS, assume_flat: bool = False) -> Callable[[pd.DataFrame], float]:
    """Metric ``frame -> timing excess``. With ``q=`` the causal cutoff is recomputed from each frame's OWN score, so
    after ``controls_report`` shuffles the score the cutoff follows the shuffle (the right control); with
    ``cutoff_col=`` the frame must carry a cutoff computed after any shuffle (see the module docstring)."""
    if (q is None) == (cutoff_col is None):
        raise ValueError("pass exactly one of q= and cutoff_col=")
    return lambda d: timing_excess(d, cost_bps, q=q, cutoff_col=cutoff_col, min_obs=min_obs,
                                   assume_flat=assume_flat).excess
