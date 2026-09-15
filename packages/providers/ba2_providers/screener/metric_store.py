"""Precomputed, exportable screener METRIC STORE for screener-settings optimization.

The FMP screener universe (current actively-trading US names — survivorship-biased by design)
is enumerated once, then each symbol's per-day screen metrics are computed VECTORISED from the
already-disk-cached OHLCV and written as date-partitioned parquet (exportable; extend by adding
partitions). At optimize time the store loads into pandas and each GA individual filters it
per day. No server.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ba2_common.core import shared_arrays as _sa
from ba2_providers.fmp_common import fmp_http_get
from ba2_common.logger import logger

_SCREENER_URL = "https://financialmodelingprep.com/api/v3/stock-screener"
# Point-in-time fundamentals (fetched ONCE at build time, baked into the store, disk-cached):
#  * historical-market-capitalization -> daily market cap (correct across buybacks/issuance/splits)
#  * v4 historical/shares_float        -> free float over time
_HIST_MCAP_URL = "https://financialmodelingprep.com/api/v3/historical-market-capitalization"
_HIST_FLOAT_URL = "https://financialmodelingprep.com/api/v4/historical/shares_float"

# historical-market-capitalization silently caps its response to the most recent ~1300 rows
# regardless of the from/to span or `limit` (confirmed by direct probe 2026-08-01: a 3-year/
# 756-row window came back complete; the full 2020-2026/~1650-trading-day span was truncated to
# the newest 1306). 1000 CALENDAR days (~2.7y, ~680 trading days) stays comfortably under the
# confirmed-safe point while keeping the per-symbol call count low.
_MCAP_CHUNK_DAYS = 1000


def _date_chunks(start: str, end: str, chunk_days: int) -> List[Tuple[str, str]]:
    """Split [start, end] (``YYYY-MM-DD``) into consecutive <=``chunk_days`` windows."""
    lo = datetime.strptime(start, "%Y-%m-%d")
    hi = datetime.strptime(end, "%Y-%m-%d")
    if lo > hi:
        return []
    chunks = []
    cur = lo
    step = timedelta(days=chunk_days)
    while cur <= hi:
        chunk_end = min(cur + step - timedelta(days=1), hi)
        chunks.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _fund_cache_path(kind: str, symbol: str) -> str:
    """Disk-cache path for a per-symbol historical fundamentals series (so a re-build never
    re-fetches). ``kind`` in {'market_cap','float'}. Under CACHE_FOLDER/screener_fundamentals."""
    import ba2_common.config as _cfg  # read at call time so tests rebinding CACHE_FOLDER win
    d = os.path.join(_cfg.CACHE_FOLDER, "screener_fundamentals", kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol.upper()}.parquet")


def _fund_meta_path(path: str) -> str:
    return path + ".meta.json"


def _read_fetched_from(meta_path: str) -> Optional[str]:
    """The ``start`` a symbol's fundamentals cache was actually fetched from, or None if unknown
    (no meta file yet — e.g. a cache written before this tracking existed). ``None`` is always
    treated as "does not cover any start" so an old cache self-heals on the next build that
    widens the window, instead of silently short-circuiting on a range it never fetched."""
    try:
        with open(meta_path) as f:
            return json.load(f).get("fetched_from")
    except Exception:  # noqa: BLE001 — missing/corrupt meta -> unknown, forces a re-fetch
        return None


def _write_fetched_from(meta_path: str, start: str) -> None:
    try:
        with open(meta_path, "w") as f:
            json.dump({"fetched_from": start}, f)
    except Exception:  # noqa: BLE001 — best-effort, same as the parquet cache write
        pass


def _write_parquet_atomic(df: "pd.DataFrame", path: str) -> None:
    # Process+thread-unique temp so two concurrent builders (separate processes) writing the same
    # symbol's cache never clobber each other's half-written .tmp; os.replace is atomic on POSIX.
    import threading
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _series_from_cache_df(df: "pd.DataFrame", col: str) -> "pd.Series":
    """A tz-naive, date-indexed, ascending Series from a cached [date, <col>] frame (empty-safe)."""
    if df is None or df.empty or "date" not in df.columns or col not in df.columns:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(df["date"], errors="coerce")
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_localize(None)
    s = pd.Series(pd.to_numeric(df[col], errors="coerce").values, index=idx).dropna()
    s = s[~s.index.isna()].sort_index()
    # Drop duplicate dates (keep the last) — a duplicated source index makes the downstream
    # reindex(method='ffill') raise ("cannot reindex on an axis with duplicate labels").
    return s[~s.index.duplicated(keep="last")]


def fetch_historical_market_cap(symbol: str, api_key: str, start: str, end: str) -> "pd.Series":
    """Daily historical market cap for ``symbol`` over [start,end], as a tz-naive date-indexed
    Series. DISK-CACHED (parquet) — a re-build reads the cache instead of re-hitting FMP. Each
    row's date is the market date, so an as-of (ffill <= scan date) read is point-in-time.

    The cache is keyed on symbol only, so a plain "does the file exist" check would silently
    serve a shorter range whenever a later build widens ``start`` (e.g. extending the store back
    to 2020 after it was first built from 2022) — found 2026-08-01 when the 2020 extension left
    market_cap ~99.7% null for 2020-2021 despite the OHLCV backfill succeeding. A sidecar
    ``.meta.json`` records the ``start`` actually fetched; a build asking for an earlier one
    triggers a re-fetch, merged onto the existing cache (never re-fetches a range already
    covered).

    SECOND bug found the same day, AFTER the above fix: the endpoint itself silently caps its
    response to the most recent ~1300 rows regardless of the ``from``/``to`` span or
    ``limit=100000`` — confirmed by direct probe: a 1-year request for NVDA returns the full,
    untruncated year, but the SAME symbol's full 2020-2026 span returns only the newest ~1306
    rows (floor ~2021-04), silently DISCARDING the older 2020-2021 rows it would happily return
    if asked narrowly. ``limit`` doesn't help — it's accepted but ignored. Fixed here by CHUNKING
    any request wider than ``_MCAP_CHUNK_DAYS`` into sub-ranges (confirmed safe: a 3-year/756-row
    chunk came back complete) and concatenating — the same shape as the codebase's other known
    FMP per-request caps (intraday OHLCV's 8-day chunking, the insider provider's pagination)."""
    path = _fund_cache_path("market_cap", symbol)
    meta_path = _fund_meta_path(path)
    prior_start = _read_fetched_from(meta_path)
    covers_start = prior_start is not None and prior_start <= start
    cached_df: "Optional[pd.DataFrame]" = None
    if os.path.exists(path):
        try:
            cached_df = pd.read_parquet(path)
            if covers_start:
                return _series_from_cache_df(cached_df, "market_cap")
        except Exception:  # noqa: BLE001 — corrupt cache -> re-fetch
            cached_df = None
    rows: list = []
    fetch_ok = True
    for chunk_start, chunk_end in _date_chunks(start, end, _MCAP_CHUNK_DAYS):
        try:
            r = fmp_http_get(f"{_HIST_MCAP_URL}/{symbol}",
                             params={"apikey": api_key, "from": chunk_start, "to": chunk_end,
                                     "limit": 100000},
                             endpoint="historical-market-cap", timeout=30)
            j = r.json()
            rows.extend(j if isinstance(j, list) else [])
        except Exception:  # noqa: BLE001 — per-symbol/per-chunk fetch failure
            fetch_ok = False  # don't record fetched_from below -- this range wasn't fully fetched
    df = pd.DataFrame(
        [{"date": x.get("date"), "market_cap": x.get("marketCap")}
         for x in rows if isinstance(x, dict) and x.get("date")]
    )
    if cached_df is not None and not cached_df.empty:
        df = pd.concat([cached_df, df], ignore_index=True).drop_duplicates(subset="date", keep="last")
    if not df.empty:
        try:
            _write_parquet_atomic(df, path)
        except Exception:  # noqa: BLE001 — cache write best-effort
            fetch_ok = False
    # Only record coverage when every chunk genuinely succeeded -- a partial/failed chunk (e.g.
    # rate-limited mid-symbol) must NOT be remembered as "fetched from `start`", or the next
    # build would trust the gap as real and never retry it (this was the actual failure mode
    # that produced the 2020-2021 nulls even after the range-check fix above).
    if fetch_ok:
        try:
            _write_fetched_from(meta_path, start)
        except Exception:  # noqa: BLE001
            pass
    return _series_from_cache_df(df, "market_cap")


def fetch_historical_float(symbol: str, api_key: str, start: str, end: str) -> "pd.Series":
    """Historical FREE FLOAT (share count) for ``symbol`` as a tz-naive Series, indexed by the
    float's EFFECTIVE (publication) date. DISK-CACHED.

    LOOKAHEAD-SAFE: FMP v4 ``historical/shares_float`` rows carry a fiscal/value ``date`` (and an
    SEC filing as ``source``) — that ``date`` is the period-end, NOT when the float became public.
    Filing it forward by the raw ``date`` would leak a not-yet-announced float to screens run
    between period-end and the filing. We therefore index each row on
    ``statement_effective_date`` (fillingDate/acceptedDate, else ``date`` + the standard ~75-day
    reporting lag) — the SAME gate ``FMPHistoricalScreenerProvider._shares_at`` uses for shares.
    An as-of (ffill) read then only exposes a float on/after its likely public date. The cache
    stores the already-effective-dated series. Empty Series when the plan/endpoint returns
    nothing (the float filter then degrades gracefully — unknown float passes the gate).

    Cache-coverage tracking matches ``fetch_historical_market_cap`` (see its docstring): a
    sidecar ``.meta.json`` records the ``start`` a build actually requested, so widening the
    window on a later build re-fetches (merged onto the existing cache) instead of silently
    serving a cache that never covered the new range. The FMP endpoint here isn't itself
    ``from``/``to``-bounded (it returns everything), so ``start`` only gates the cache check.

    UNLIKE market_cap, this one can't be chunked around its cap: confirmed by direct probe
    2026-08-01 the endpoint returns a hard-capped ~1800 most-recent rows (NVDA floor ~2021-05)
    regardless of a ``page`` param (tried 0/1/2 -- identical response each time) and takes no
    ``from``/``to`` at all. Pre-2021ish float is therefore NOT recoverable from this endpoint on
    the current plan. Accepted as-is: ``screen_universe_for_day``'s float gate is already
    NaN-tolerant (missing float passes rather than excludes), so this degrades gracefully rather
    than silently breaking a screen."""
    path = _fund_cache_path("float", symbol)
    meta_path = _fund_meta_path(path)
    prior_start = _read_fetched_from(meta_path)
    covers_start = prior_start is not None and prior_start <= start
    cached_df: "Optional[pd.DataFrame]" = None
    if os.path.exists(path):
        try:
            cached_df = pd.read_parquet(path)
            if covers_start:
                return _series_from_cache_df(cached_df, "float_shares")
        except Exception:  # noqa: BLE001
            cached_df = None
    rows: list = []
    try:
        r = fmp_http_get(_HIST_FLOAT_URL, params={"symbol": symbol, "apikey": api_key},
                         endpoint="historical-shares-float", timeout=30)
        j = r.json()
        rows = j if isinstance(j, list) else []
    except Exception:  # noqa: BLE001
        rows = []
    from ba2_common.core.provider_utils import statement_effective_date
    recs = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        fs = x.get("floatShares")
        if fs is None:
            continue
        eff = statement_effective_date(x)  # publication date (filing/accepted, else date + lag)
        if eff is None:
            continue
        recs.append({"date": eff.strftime("%Y-%m-%d"), "float_shares": fs})
    df = pd.DataFrame(recs)
    if cached_df is not None and not cached_df.empty:
        df = pd.concat([cached_df, df], ignore_index=True).drop_duplicates(subset="date", keep="last")
    if not df.empty:
        try:
            _write_parquet_atomic(df, path)
            _write_fetched_from(meta_path, start)
        except Exception:  # noqa: BLE001
            pass
    return _series_from_cache_df(df, "float_shares")


def _fetch_screener_rows(api_key: str) -> List[Dict[str, Any]]:
    """One call to the FMP screener for the current actively-trading US universe.

    ETFs/mutual funds are excluded server-side (``isEtf=false&isFund=false``, matching the live
    FMPScreenerProvider) — the grade/earnings/insider experts don't apply to them, and excluding
    them server-side lets the 10k row cap fill with real equities instead of funds.
    """
    resp = fmp_http_get(
        _SCREENER_URL,
        params={"limit": 10000, "exchange": "nasdaq,nyse,amex",
                "isActivelyTrading": "true", "isEtf": "false", "isFund": "false",
                "apikey": api_key},
        endpoint="stock-screener",
    )
    rows = resp.json()
    return rows if isinstance(rows, list) else []


def enumerate_universe(api_key: str, market_cap_min: float, price_min: float,
                       volume_min: float) -> List[Dict[str, Any]]:
    """Return screener rows passing the LOOSEST static bounds (the shortlist superset).

    Uses the screener's own current marketCap/price/volume fields (one call). These bounds are
    the loosest of every static gene's range, so no individual's looser threshold can admit a
    symbol we didn't include.

    ETFs/mutual funds (``isEtf``/``isFund``) are ALWAYS excluded — the grade/earnings/insider
    experts don't apply to them and trading them isn't the intent, matching the LIVE
    ``FMPScreenerProvider`` (``isEtf=false&isFund=false`` server-side).
    """
    out = []
    for r in _fetch_screener_rows(api_key):
        if r.get("isEtf") or r.get("isFund"):
            continue
        cap = r.get("marketCap") or 0
        px = r.get("price") or 0
        vol = r.get("volume") or 0
        if cap >= market_cap_min and px >= price_min and vol >= volume_min:
            out.append(r)
    return out


def weinstein_stage_series(close: "pd.Series", sma_period: int = 150,
                           slope_lookback: int = 20,
                           flat_threshold_pct: float = 0.5) -> "pd.Series":
    """Vectorised Weinstein stage (1-4, NaN=insufficient history) for a close series.

    1:1 port of ``ba2_common.core.weinstein.classify_weinstein_stage`` applied to EVERY bar via
    rolling ops (no per-day Python loop). For each bar D:

      sma_now   = mean of the trailing ``sma_period`` closes ending at D
                  -> ``close.rolling(sma_period).mean()`` (min_periods == sma_period so it is
                  NaN until there are exactly ``sma_period`` bars, like the classifier's
                  ``len < period -> None``).
      sma_prior = the SMA as it stood ``slope_lookback`` bars earlier
                  -> ``sma_now.shift(slope_lookback)``. This equals the classifier's
                  ``_sma(closes[:-slope_lookback], sma_period)`` exactly, because the SMA at bar
                  D-slope_lookback is the mean of the ``sma_period`` closes ending there.
      slope_pct = (sma_now - sma_prior) / sma_prior * 100
      above     = close > sma_now
      rising    = slope_pct >  flat_threshold_pct
      falling   = slope_pct < -flat_threshold_pct

    Stage mapping (identical to the classifier):
      2  above and rising            (advancing — the buy zone)
      4  not above and falling       (declining)
      3  above and not rising        (topping)
      1  otherwise                   (basing)

    A bar has a stage only once it has ``sma_period + slope_lookback`` bars of history (the
    classifier's guard) AND ``sma_prior > 0`` — otherwise NaN (the classifier's None). Returned
    as a float Series (so NaN is representable); callers compare ``== 2``.
    """
    close = close.astype(float)
    sma_now = close.rolling(sma_period, min_periods=sma_period).mean()
    sma_prior = sma_now.shift(slope_lookback)
    # Guard: classifier returns None unless sma_prior is computable AND > 0 (avoids /0 and the
    # "could not compute SMA" branch). shift already makes the first slope_lookback valid SMAs
    # NaN, which together with min_periods enforces the >= sma_period + slope_lookback history.
    valid = sma_prior > 0
    slope_pct = (sma_now - sma_prior) / sma_prior * 100.0
    above = close > sma_now
    rising = slope_pct > flat_threshold_pct
    falling = slope_pct < -flat_threshold_pct
    stage = pd.Series(1.0, index=close.index)          # default: basing
    stage = stage.where(~(above & rising), 2.0)
    stage = stage.where(~((~above) & falling), 4.0)
    stage = stage.where(~(above & ~rising), 3.0)
    # NB: the four branches are mutually exclusive (same if/elif order as the classifier:
    # 2 wins over 3 because rising excludes "not rising"; 4 is the only not-above branch chained
    # before the above-only 3), so .where overwrites are non-overlapping.
    return stage.where(valid, float("nan"))


def _drop_pct(close: "pd.Series", window: int) -> "pd.Series":
    """Pullback % from the trailing-``window`` peak (inclusive) to today's close:
    ``(rolling_max(window) - close) / rolling_max * 100`` (0 where peak<=0). Point-in-time —
    every value at D uses only closes <= D. Window 1 ⇒ peak==close ⇒ always 0 (the old bug)."""
    peak = close.rolling(max(1, int(window)), min_periods=1).max()
    return ((peak - close) / peak * 100.0).where(peak > 0, 0.0)


def _drop_pct_windows(close: "pd.Series", max_window: int) -> Dict[int, "pd.Series"]:
    """Pullback % from the trailing-W peak for EVERY window W=1..max_window, in ONE incremental
    pass: ``peak_W = max(peak_{W-1}, close shifted by W-1)`` (window W just adds the one older bar
    to window W-1). ~K cheap numpy ``fmax`` ops instead of K pandas ``rolling().max()`` calls
    (~6x faster for K=30). ``fmax`` ignores the leading NaNs from the shift, matching
    ``rolling(min_periods=1)``. Point-in-time; identical values to ``_drop_pct`` per window."""
    import numpy as _np
    arr = close.to_numpy(dtype=float)
    n = arr.shape[0]
    peak = arr.copy()                                    # W=1: peak == today's close
    out: Dict[int, "pd.Series"] = {}
    for w in range(1, max(1, int(max_window)) + 1):
        if w >= 2:
            k = w - 1
            shifted = _np.full(n, _np.nan)
            if k < n:                                    # else the bar W-1 back doesn't exist yet
                shifted[k:] = arr[:n - k]
            peak = _np.fmax(peak, shifted)
        with _np.errstate(invalid="ignore", divide="ignore"):
            dp = _np.where(peak > 0, (peak - arr) / peak * 100.0, 0.0)
        out[w] = pd.Series(dp, index=close.index)
    return out


def momentum_12_1_series(close: "pd.Series", lookback: int = 252, skip: int = 21) -> "pd.Series":
    """Per-bar 12-1 momentum, vectorised 1:1 with ``ba2_experts.FactorRanker.factors.momentum_12_1``.

    That factor, on a series ending at bar D, is ``P[-skip-1] / P[-lookback] - 1`` (skip the most
    recent ``skip`` days to dodge short-term reversal), 0.0 when fewer than ``lookback`` bars or the
    start price <= 0. ``iloc[-lookback]`` == ``shift(lookback-1)`` and ``iloc[-skip-1]`` ==
    ``shift(skip)``, so at every bar D this is ``close.shift(skip) / close.shift(lookback-1) - 1``.
    NaN until ``lookback`` bars exist (the factor's <lookback -> 0.0 case); the consumer maps NaN
    AND non-positive-start to 0.0 so the precomputed value is byte-identical to the runtime factor.
    Precomputing it lets FactorRanker read momentum point-in-time from the store instead of fetching
    ~400 days of daily closes per symbol per rebalance (the dominant FactorRanker memory + CPU cost)."""
    c = close.astype(float)
    p_start = c.shift(lookback - 1)
    p_end = c.shift(skip)
    mom = p_end / p_start - 1.0
    return mom.where(p_start > 0)  # NaN where insufficient history or non-positive start


# ATR periods precomputed into the store — matches the atr_period optimizer gene's range
# ({7,14,21,28} in the launcher), so every value the GA can request is available offline.
ATR_PERIODS = (7, 14, 21, 28)


def atr_series(high: "pd.Series", low: "pd.Series", close: "pd.Series",
               period: int = 14) -> "pd.Series":
    """Per-bar Average True Range (Wilder's smoothing), vectorised over full history.

    True range at bar D = max(high-low, |high-prev_close|, |low-prev_close|) (the first bar has
    no prev_close, so TR there is just high-low). ATR is Wilder's exponential smoothing of TR:
    ``atr[0] = mean(TR[:period])``, ``atr[i] = (atr[i-1]*(period-1) + TR[i]) / period`` — the SAME
    smoothing used by every conventional ATR (equivalent to ``ewm(alpha=1/period, adjust=False)``
    seeded by the first simple average). NaN until ``period`` bars exist. Precomputing this lets
    the backtest's risk-based (ATR) position sizing read ATR point-in-time from the store instead
    of fetching live indicator data mid-run (which the GA/optimize trial-worker path has no
    hermetic route to — see ``recompute_atr_columns``)."""
    h, l, c = high.astype(float), low.astype(float), close.astype(float)
    prev_close = c.shift(1)
    tr = pd.concat([
        h - l,
        (h - prev_close).abs(),
        (l - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr.iloc[0] = (h - l).iloc[0]  # no prev_close on the first bar
    # Wilder smoothing == an EWM with alpha=1/period seeded by the first `period` bars' simple
    # mean; pandas' ewm(adjust=False) recurrence matches this exactly once seeded, so compute the
    # seed then run the recurrence via ewm on the POST-seed tail and stitch them together.
    if len(tr) < period:
        return pd.Series(float("nan"), index=tr.index)
    seed = tr.iloc[:period].mean()
    out = pd.Series(float("nan"), index=tr.index)
    out.iloc[period - 1] = seed
    tail = tr.iloc[period:]
    if len(tail):
        smoothed = tail.ewm(alpha=1.0 / period, adjust=False).mean()
        # ewm(adjust=False) with NO explicit seed starts from tail.iloc[0]; blend the true seed in
        # by prepending it as bar 0 of the recurrence, then dropping that synthetic first output.
        seeded = pd.concat([pd.Series([seed]), tail]).ewm(alpha=1.0 / period, adjust=False).mean()
        out.iloc[period:] = seeded.iloc[1:].to_numpy()
    return out


def compute_daily_metrics(ohlcv: "pd.DataFrame",
                          market_cap_series: Optional["pd.Series"] = None,
                          float_series: Optional["pd.Series"] = None,
                          shares: Optional[float] = None,
                          rvol_window: int = 20, drop_days: int = 5,
                          vol_window: int = 20, max_lookback: int = 30) -> "pd.DataFrame":
    """Per-day screen metrics for ONE symbol, vectorised over its full history.

    ``ohlcv`` is indexed by date with columns Open/High/Low/Close/Volume (the shape the as-of
    OHLCV cache returns). Returns a DataFrame indexed by date with columns:
    close, market_cap, volume, relative_volume, price_drop_pct, weinstein_stage, float_shares.
    NaN rows (insufficient lookback) are kept — callers drop them.

    POINT-IN-TIME safe: every value at row D uses only data <= D.
      * volume        = trailing ``vol_window``-session AVERAGE daily volume ending at D (the
                        "typical daily volume" level the screener's volume_min/max gates — was
                        previously a single CURRENT static value copied to every date: the bug).
      * market_cap    = ``market_cap_series`` as-of D (ffill from the FMP historical-market-cap
                        series) — NOT close x CURRENT shares. Falls back to close x ``shares``
                        only if no series is supplied (legacy).
      * float_shares  = ``float_series`` as-of D (ffill from the FMP historical free-float series).
    """
    close = ohlcv["Close"].astype(float)
    vol = ohlcv["Volume"].astype(float)
    # RVOL: today's volume / trailing average of the PRIOR rvol_window days (EXCLUDES today via
    # shift(1) — point-in-time: today is the spike measured against its prior baseline).
    avg_vol_prior = vol.shift(1).rolling(rvol_window, min_periods=1).mean()
    rvol = (vol / avg_vol_prior).where(avg_vol_prior > 0, 0.0)
    # Typical daily volume LEVEL: trailing average INCLUDING today (point-in-time, ending at D).
    volume = vol.rolling(vol_window, min_periods=1).mean()
    # Price drop %: pullback from the trailing-window peak. The legacy single-window column
    # (``price_drop_pct`` == the ``drop_days`` window) is kept for back-compat with older stores /
    # screens; the per-window columns ``price_drop_pct_2..max_lookback`` let the optimizer search
    # the lookback Y from ONE store without rebuilding per value (screen_universe_for_day picks the
    # column from the ``price_drop_days`` setting). All point-in-time (rolling max over closes <= D).
    _dw = _drop_pct_windows(close, max(int(max_lookback), int(drop_days), 1))
    drop_pct = _dw[max(1, int(drop_days))]
    windowed = {f"price_drop_pct_{w}": _dw[w].round(4)
                for w in range(2, max(2, int(max_lookback)) + 1)}
    # Market cap: point-in-time from the historical series (as-of each bar via ffill). Falls back
    # to close x static shares only when no series is available.
    if market_cap_series is not None and len(market_cap_series):
        mcap = market_cap_series.reindex(close.index, method="ffill")
    elif shares:
        mcap = close * shares
    else:
        mcap = pd.Series(float("nan"), index=close.index)
    # Free float: point-in-time from the historical series (held as-of via ffill); NaN otherwise.
    if float_series is not None and len(float_series):
        flt = float_series.reindex(close.index, method="ffill")
    else:
        flt = pd.Series(float("nan"), index=close.index)
    # Weinstein stage (price vs RISING 150-session/30-week SMA) — vectorised 1:1 with
    # ba2_common.core.weinstein.classify_weinstein_stage. NaN until enough history.
    stage = weinstein_stage_series(close)
    # 12-1 momentum, precomputed so FactorRanker reads it point-in-time (no per-rebalance OHLCV fetch).
    momentum = momentum_12_1_series(close)
    # ATR (all ATR_PERIODS), precomputed so risk-based (ATR) position sizing reads it point-in-time
    # from the store — the GA/optimize trial-worker path has no hermetic route to live indicator
    # data mid-run, so this is the ONLY way ATR-based sizing/stops function there (see
    # position_sizing.synthesize_safeguard_stop + TradeRiskManagement._risk_atr_quantity). Needs
    # High/Low (real OHLCV always has them); a Close-only caller (e.g. a minimal test fixture)
    # gets NaN ATR columns rather than a crash.
    if "High" in ohlcv.columns and "Low" in ohlcv.columns:
        atr_cols = {f"atr_{p}": atr_series(ohlcv["High"], ohlcv["Low"], close, period=p).round(4)
                   for p in ATR_PERIODS}
    else:
        atr_cols = {f"atr_{p}": pd.Series(float("nan"), index=close.index) for p in ATR_PERIODS}
    out = pd.DataFrame({
        "close": close,
        "market_cap": mcap,
        "volume": volume.round(2),
        "relative_volume": rvol.round(4),
        "price_drop_pct": drop_pct.round(4),
        "weinstein_stage": stage,
        "momentum_12_1": momentum.round(6),
        **atr_cols,
        "float_shares": flt,
    })
    for _col, _ser in windowed.items():
        out[_col] = _ser
    return out


def existing_months(store_dir: str) -> set:
    """Year-months (``YYYY-MM``) already materialised in the store (for incremental skip)."""
    if not os.path.isdir(store_dir):
        return set()
    return {d[len("ym="):] for d in os.listdir(store_dir) if d.startswith("ym=")}


def write_partitions(store_dir: str, df: "pd.DataFrame", part_name: str = "part.parquet") -> None:
    """Write rows to ``<store>/ym=YYYY-MM/<part_name>`` (one file per month, atomic tmp+replace).

    ``part_name`` defaults to ``part.parquet`` (the classic single-file-per-month layout). PERIODIC
    builds pass a UNIQUE name per flush (e.g. ``part-00001.parquet``) so successive flushes ACCUMULATE
    within each month's dir instead of clobbering — ``load_store`` reads every ``*.parquet`` in the
    month dir. Never touches other months."""
    os.makedirs(store_dir, exist_ok=True)
    ym = df["date"].astype(str).str.slice(0, 7)
    for month, chunk in df.groupby(ym):
        d = os.path.join(store_dir, f"ym={month}")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, part_name + ".tmp")
        chunk.to_parquet(tmp, index=False)
        os.replace(tmp, os.path.join(d, part_name))


def scan_date_grid(start: str, end: str, cadence_days: int) -> "pd.DatetimeIndex":
    """The common scan-date grid: every ``cadence_days`` CALENDAR days from start..end.
    Default cadence 7 = one scan per week. Shared across symbols so scan dates are consistent.

    NOTE: distinct from the read-time ``scan_dates(store_df, store_key)`` below — this builds the
    grid at store-BUILD time; that one lists the dates already present in a built store."""
    return pd.date_range(start=start, end=end, freq=f"{int(cadence_days)}D")


def build_store(store_dir: str, api_key: str, start: str, end: str, *,
                market_cap_min: float, price_min: float, volume_min: float,
                ohlcv_get, mcap_get=None, float_get=None, shares_get=None,
                cadence_days: int = 7, rvol_window: int = 20, drop_days: int = 5,
                max_lookback: int = 30, max_workers: int = 8, symbol_retries: int = 2,
                flush_every: int = 250, fail_on_quality: bool = True) -> Dict[str, Any]:
    """Build/extend the metric store for [start,end] at ``cadence_days`` (default 7 = weekly).
    SKIPS months already present (incremental).

    Per-symbol POINT-IN-TIME inputs (each disk-cached so a re-build never re-fetches):
      * ``ohlcv_get(symbol, end_date)`` -> OHLCV up to end_date (as-of cache).
      * ``mcap_get(symbol)``  -> date-indexed historical market-cap Series (optional).
      * ``float_get(symbol)`` -> date-indexed historical free-float Series (optional).
      * ``shares_get(symbol)``-> latest-filing shares (legacy mcap fallback only).

    Each symbol's daily metrics (volume/market_cap/float_shares/RVOL/price-drop/Weinstein) are
    computed then sampled AS-OF each scan date (latest trading day <= scan date via ffill), so
    every row is point-in-time. These values are BAKED into the parquet store, so the optimizer's
    per-day screen stays a pure in-memory filter (no fetching at optimize time).

    The per-symbol fetch+compute runs in a thread pool (``max_workers``); the OHLCV reads are
    disk-IO and the mcap/float fetches go through ``fmp_http_get`` (global rate-limit gate), so
    threads overlap IO/network safely. Returns {symbols, months_written, months_skipped,
    cadence_days, quality_failures}.

    ``fail_on_quality`` (default True): raise ``MetricStoreQualityError`` if any written month has
    a column exceeding its ``_BUILD_MAX_NAN`` tolerance. Written partitions are kept either way --
    the raise reports that they are not TRUSTWORTHY, it does not discard the work.
    """
    grid = scan_date_grid(start, end, cadence_days)
    want_months = sorted({d.strftime("%Y-%m") for d in grid})
    have = existing_months(store_dir)
    todo_months = [m for m in want_months if m not in have]
    if not todo_months:
        return {"symbols": 0, "months_written": 0, "months_skipped": len(want_months), "cadence_days": cadence_days}
    grid_todo = grid[[d.strftime("%Y-%m") in set(todo_months) for d in grid]]
    universe = enumerate_universe(api_key, market_cap_min, price_min, volume_min)
    static_by_sym = {r["symbol"]: r for r in universe}

    def _build_one_once(sym: str, srow: Dict[str, Any]):
        df = ohlcv_get(sym, end)
        if df is None or df.empty:
            return None
        mcap_s = mcap_get(sym) if mcap_get is not None else None
        flt_s = float_get(sym) if float_get is not None else None
        shares = shares_get(sym) if shares_get is not None else None
        m = compute_daily_metrics(df, market_cap_series=mcap_s, float_series=flt_s,
                                  shares=shares, rvol_window=rvol_window, drop_days=drop_days,
                                  max_lookback=max_lookback)
        m = m.reindex(grid_todo, method="ffill")             # value AS-OF each scan date
        m = m.dropna(subset=["close"]).reset_index().rename(columns={"index": "date"})
        m["date"] = m["date"].astype(str).str.slice(0, 10)
        if m.empty:
            return None
        m["symbol"] = sym
        m["sector"] = srow.get("sector")
        m["price"] = m["close"]
        # volume / market_cap / float_shares / weinstein_stage all ride along from
        # compute_daily_metrics (the reindex carried them as-of each scan date) — point-in-time,
        # baked into the store so the per-day screen needs no OHLCV/network at read time.
        return m

    def _build_one(sym: str, srow: Dict[str, Any]):
        # RESILIENT + RETRY: a single symbol's fetch failure (e.g. an FMP response with no
        # 'historical' key for a thin SPAC/unit, or a transient network/5xx error) must NOT abort
        # the whole build — ``ex.map`` below would otherwise re-raise it and discard every frame
        # built so far (partitions are written only at the end). Retry a few times with backoff
        # (most such failures are transient), then skip the symbol if it still fails.
        last_err = None
        for attempt in range(symbol_retries + 1):
            try:
                return _build_one_once(sym, srow)
            except Exception as e:  # noqa: BLE001 — one symbol must never kill the build
                last_err = e
                if attempt < symbol_retries:
                    time.sleep(1.5 * (attempt + 1))  # 1.5s, 3.0s, ... brief backoff before retry
        logger.warning(f"metric-store: skipping {sym} after {symbol_retries + 1} attempts ({last_err})")
        return None

    # PERIODIC WRITE: flush accumulated frames to disk every ``flush_every`` symbols (each flush
    # is a uniquely-named part file per month, so flushes accumulate — see write_partitions). This
    # persists progress incrementally, so a crash/kill keeps everything built so far instead of
    # losing the whole run (partitions used to be written only at the very end). Set flush_every<=0
    # to restore the single write-at-end behaviour.
    from concurrent.futures import ThreadPoolExecutor
    items = list(static_by_sym.items())
    frames: List["pd.DataFrame"] = []
    written = 0          # symbols whose rows have been flushed
    flush_seq = 0
    fe = int(flush_every)

    def _flush() -> None:
        nonlocal frames, flush_seq, written
        if not frames:
            return
        flush_seq += 1
        write_partitions(store_dir, pd.concat(frames, ignore_index=True),
                         part_name=f"part-{flush_seq:05d}.parquet")
        written += len(frames)
        logger.info(f"metric-store: flushed {len(frames)} symbols "
                    f"(part-{flush_seq:05d}, {written} total) to {store_dir}")
        frames = []

    quality: Dict[str, Dict[str, float]] = {}

    def _record_quality(batch: "pd.DataFrame") -> None:
        """Accumulate per-month NaN findings for the rows just flushed."""
        for ym, grp in batch.groupby(batch["date"].astype(str).str.slice(0, 7)):
            bad = check_frame_quality(grp)
            if bad:
                quality[str(ym)] = bad

    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        for res in ex.map(lambda kv: _build_one(kv[0], kv[1]), items):
            if res is not None and not res.empty:
                frames.append(res)
                if fe > 0 and len(frames) >= fe:
                    _record_quality(pd.concat(frames, ignore_index=True))
                    _flush()
    if frames:
        _record_quality(pd.concat(frames, ignore_index=True))
    _flush()  # final remainder

    summary = {"symbols": len(static_by_sym), "months_written": len(todo_months),
               "months_skipped": len(set(have) & set(want_months)),
               "cadence_days": cadence_days, "quality_failures": quality}
    if quality and fail_on_quality:
        # FAIL LOUD. A partition that exists but whose columns are mostly NaN is worse than a
        # missing one: every downstream check ("month coverage continuous", "all partitions
        # present") passes while the screen it feeds is silently starved. This is the guard that
        # was missing on 2026-08-01, when a build wrote 24 months of ~99.7%-NaN market_cap and
        # ~50%-NaN weinstein_stage and reported success -- the breakage was only caught days later
        # by reading a health-check log. Partitions are LEFT ON DISK (progress is not thrown away);
        # the caller decides whether to repair the inputs and re-run, or lower the thresholds.
        worst = sorted(quality.items())[:8]
        raise MetricStoreQualityError(
            f"metric-store build wrote {len(quality)} month(s) failing column-quality thresholds "
            f"{_BUILD_MAX_NAN} -- partitions are on disk but NOT trustworthy. "
            f"First failures: {worst}. "
            f"A high NaN rate on warmup-dependent columns (weinstein_stage/atr_14/momentum_12_1) "
            f"usually means the underlying OHLCV cache lacks history BEFORE the build's start "
            f"date -- re-fetch 1d bars with an earlier --start, then rebuild. "
            f"Pass fail_on_quality=False to record findings without raising."
        )
    return summary


_STORE_MEMO: Dict[str, "pd.DataFrame"] = {}
_SCAN_DATES_MEMO: Dict[str, List[str]] = {}


def scan_dates(store_df: "pd.DataFrame", store_key: str = "") -> List[str]:
    """Sorted unique scan-date strings ('YYYY-MM-DD'), memoised by ``store_key`` (the store dir).

    Lets the per-bar as-of resolve be an O(log n) bisect over this list instead of an O(rows)
    object-array comparison (``store_df['date'] <= day``) on EVERY 5-min bar — the latter was the
    dominant CPU cost of a screener backtest (re-scanning the whole ~160k-row store per bar)."""
    if store_key and store_key in _SCAN_DATES_MEMO:
        return _SCAN_DATES_MEMO[store_key]
    ds = sorted({str(d) for d in store_df["date"].unique()})
    if store_key:
        _SCAN_DATES_MEMO[store_key] = ds
    return ds


# Columns a build MUST be able to compute, with the max NaN fraction tolerated before the build
# is treated as failed. Deliberately per-column, not one global threshold: they degrade for
# different reasons and a blanket number would either mask a dead market_cap or reject a
# legitimately sparse float.
#   market_cap  - hard screen filter; a NaN row is silently DROPPED by every cap-band screen, so
#                 a dead column empties the universe rather than loosening it. Near-zero tolerance.
#   close/price - if these are NaN the row is meaningless.
#   weinstein_stage / atr_14 / momentum_12_1 - WARMUP-dependent (30-week MA, ATR-14, 252-bar
#                 momentum). A high NaN rate here means the underlying OHLCV lacks the lead-in
#                 before the build's start date, which is exactly the 2026-08-01 incident: half
#                 the universe's 1d cache began ON the start date, so weinstein sat at ~50% NaN
#                 and nothing failed. Tolerance is loose (0.35) because thin/new listings
#                 legitimately lack history -- it catches a systemic gap, not individual symbols.
# float_shares is deliberately ABSENT: the FMP endpoint hard-caps ~1800 rows with no pagination
# (see fetch_historical_float), so pre-~2021 float is unobtainable and the screen's float gate is
# explicitly NaN-tolerant. Failing on it would block every legitimate deep-history build.
_BUILD_MAX_NAN = {
    "close": 0.01,
    "price": 0.01,
    "market_cap": 0.20,
    "weinstein_stage": 0.35,
    "atr_14": 0.35,
    "momentum_12_1": 0.35,
}


class MetricStoreQualityError(RuntimeError):
    """A build produced partitions whose columns are too NaN-heavy to be usable."""


def check_frame_quality(df: "pd.DataFrame", thresholds: Optional[Dict[str, float]] = None
                        ) -> Dict[str, float]:
    """NaN fraction per column that EXCEEDS its threshold (empty dict = healthy).

    Split out from the build so callers/tests can assess a frame directly.
    """
    limits = _BUILD_MAX_NAN if thresholds is None else thresholds
    if df is None or df.empty:
        return {"__empty__": 1.0}
    bad = {}
    for col, limit in limits.items():
        if col not in df.columns:
            continue  # a store predating the column is not a build failure
        rate = float(df[col].isna().mean())
        if rate > limit:
            bad[col] = round(rate, 4)
    return bad


#: THE READER'S HALF OF THE DERIVED-CACHE CONTRACT, carried in the cache KEY. Bump it whenever
#: the array set below changes shape or MEANING: the encodings (``__columns_utf8``/``__kinds``/
#: ``<col>__cats_utf8``/``<col>__ncats``), the code-dtype rule, which columns become categoricals,
#: or which of them are ordered. A set published by an older reader then lives under a different
#: key and can never be opened by a newer one (nor the reverse), whatever the sources look like.
#: ``shared_arrays.SCHEMA_VERSION`` covers the on-disk FILE layout, which is the store's business;
#: this covers what the bytes inside those files mean, which is ours. A column ADDED to the store
#: needs no bump — that rewrites partitions, and a rewritten partition is a new (path, size, mtime)
#: and therefore a new signature.
#:
#: A BUMP ORPHANS DISK, so it is a maintenance action and not just an edit: the version is part of
#: the KEY, so ``<derived_root>/u_store.v<old>`` becomes a key nothing asks for, and ``sweep()``
#: only keeps the newest signature WITHIN a key — it will never collect it however long it sits
#: there. Deleting the ``*.v<old>`` directory is part of the bump.
METRIC_STORE_ARRAYS_VERSION = 1

#: Object columns that become an ORDERED categorical. ``date`` must be, and it is load-bearing in
#: TWO places: the as-of resolve (``dates <= day``) and ``.min()``/``.max()`` over the column
#: (``tools/strategy_research/runtime.py:110``, the store-coverage guard) both raise "Unordered
#: Categoricals can only compare equality" without it. The order is the sorted-ISO category order,
#: which is exactly the string order it replaces.
_ORDERED_CAT_COLUMNS = ("date",)

_COLUMNS_ARRAY = "__columns_utf8"
_KINDS_ARRAY = "__kinds"
_KIND_NUMERIC = 0
_KIND_CATEGORICAL = 1
_CATS_SUFFIX = "__cats_utf8"
_NCATS_SUFFIX = "__ncats"
#: Characters that cannot appear in a column name: each array is published as ``<name>.npy``, and
#: the name also travels inside a newline-joined blob.
_UNSAFE_NAME_CHARS = '/\\:*?"<>|\n\r'

#: Stems NTFS refuses as a path segment whatever the extension, so ``AUX.npy`` cannot be created.
#: ``shared_arrays._safe_key`` guards the cache KEY with the same set, but array NAMES go through
#: no sanitiser at all -- so the check has to be made here. Taken from that module so there is one
#: definition, with the (tiny) set replicated only for the case where it is ever renamed there.
_RESERVED_NAMES = getattr(_sa, "_WINDOWS_RESERVED", None) or (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)})


def _utf8_join(parts: "List[str]", what: str) -> "np.ndarray":
    """``parts`` as a uint8 array of the newline-joined UTF-8 bytes.

    Strings inside a numeric-only array contract, exactly as the option reader ships
    ``c_occ_utf8``. A member containing a newline would shift every later element by one and
    mis-key silently from then on, so it is refused here rather than decoded wrongly there.
    """
    for s in parts:
        if "\n" in s or "\r" in s:
            raise ValueError(
                f"metric store: {what} contains a newline ({s!r}); the shared-array encoding is "
                "newline-joined and cannot represent it")
    return np.frombuffer("\n".join(parts).encode("utf-8"), dtype=np.uint8)


def _utf8_split(arr: "np.ndarray", n_expected: int, what: str) -> "List[str]":
    """Decode ``_utf8_join`` back to exactly ``n_expected`` strings, or raise.

    The count is carried alongside the bytes because ``"".split("\\n")`` is ambiguous (zero
    strings or one empty one), and because a mapped set paired with the wrong sidecar must fail
    loudly rather than silently re-key every row.
    """
    # An EMPTY blob is two different things -- zero strings, or ONE empty string -- and the byte
    # count cannot tell them apart. ``n_expected`` is what decides, so a category column that is
    # "" on every row round-trips instead of failing the decode that is meant to validate it.
    out = [] if (arr.size == 0 and n_expected == 0) else bytes(arr).decode("utf-8").split("\n")
    if len(out) != n_expected:
        raise ValueError(
            f"metric store: {what} decoded to {len(out)} entries, expected {n_expected} — the "
            "mapped arrays are inconsistent")
    return out


def _code_dtype(n_categories: int) -> "np.dtype":
    """The code dtype pandas ITSELF would choose for this cardinality.

    Load-bearing, not tidiness: ``Categorical.from_codes`` runs the incoming codes through
    ``coerce_indexer_dtype``, which returns the same buffer only when the dtype already matches —
    an int32 code file for 4 734 symbols is silently COPIED into int16 and the whole point of
    mapping it is lost (measured, spike §3.4). Mirrors pandas' thresholds exactly.
    """
    if n_categories < np.iinfo(np.int8).max:
        return np.dtype(np.int8)
    if n_categories < np.iinfo(np.int16).max:
        return np.dtype(np.int16)
    if n_categories < np.iinfo(np.int32).max:
        return np.dtype(np.int32)
    return np.dtype(np.int64)


def _store_arrays_from_frame(df: "pd.DataFrame") -> "Dict[str, np.ndarray]":
    """The concatenated store frame -> the shareable numeric half, as 1-D arrays.

    Numeric/bool columns travel AS THEY ARE (no dtype rewriting — a float64 column that became
    float32 here would change every screen's arithmetic). Object columns travel as integer
    category codes plus their sorted category strings; on the real store that is 223 MB of python
    strings per worker collapsed to 6.8 MB of shared codes, 80 MB of which is a ``sector`` column
    no consumer reads. The original column ORDER and the per-column kind ride along
    (``__columns_utf8`` / ``__kinds``) so the frame is rebuilt exactly as the parquet concat
    produced it.

    Anything that is neither a numpy numeric/bool dtype nor object is REFUSED rather than coerced:
    a pandas nullable ``Int64`` has a mask this encoding does not carry, and silently turning it
    into float64 would change what ``isna()`` means downstream. The real store is 42 float64 + 3
    object (spike §1.2), so this is a guard against a future build writing something new, and it
    should fail the build rather than the trial.
    """
    arrays: "Dict[str, np.ndarray]" = {}
    names: "List[str]" = []
    kinds: "List[int]" = []
    for raw_name in df.columns:
        name = str(raw_name)
        if any(c in name for c in _UNSAFE_NAME_CHARS) or name.startswith("__") \
                or name.endswith((_CATS_SUFFIX, _NCATS_SUFFIX)) \
                or name.split(".")[0].upper() in _RESERVED_NAMES:
            raise ValueError(
                f"metric store: column {name!r} cannot be a shared-array name (it is published as "
                f"'<name>.npy', so it must not collide with the '__' encodings and must not be a "
                f"Windows reserved device stem)")
        col = df[raw_name]
        dtype = col.dtype
        if dtype == object or isinstance(dtype, pd.CategoricalDtype):
            values = col.astype(object) if isinstance(dtype, pd.CategoricalDtype) else col
            cat = pd.Categorical(values, ordered=name in _ORDERED_CAT_COLUMNS)
            # Refused, not stringified. ``str()`` would happily turn an int or Timestamp column
            # into categories that compare and sort DIFFERENTLY from the values the store was
            # built with, and the frame would come back silently re-typed instead of failing here,
            # where the BUILD that produced the odd column can be fixed.
            bad = next((c for c in cat.categories if not isinstance(c, str)), None)
            if bad is not None:
                raise TypeError(
                    f"metric store: object column {name!r} holds a non-string value {bad!r} "
                    f"({type(bad).__name__}); the shared-array contract carries object columns as "
                    "STRING categories only. Write it as a numeric column, or as ISO strings.")
            cats = [str(c) for c in cat.categories]
            codes = np.ascontiguousarray(cat.codes, dtype=_code_dtype(len(cats)))
            arrays[name] = codes
            arrays[name + _CATS_SUFFIX] = _utf8_join(cats, f"{name} categories")
            arrays[name + _NCATS_SUFFIX] = np.array([len(cats)], dtype=np.int64)
            kinds.append(_KIND_CATEGORICAL)
        elif isinstance(dtype, np.dtype) and dtype.kind in "fiub":
            arrays[name] = np.ascontiguousarray(col.to_numpy())
            kinds.append(_KIND_NUMERIC)
        else:
            raise TypeError(
                f"metric store: column {name!r} has dtype {dtype!r}, which the shared-array "
                "contract cannot carry (numeric/bool or object strings only). Write it as float64 "
                "(NaN for missing) in the build, or bump METRIC_STORE_ARRAYS_VERSION and teach "
                "this encoder about it.")
        names.append(name)
    arrays[_COLUMNS_ARRAY] = _utf8_join(names, "column names")
    arrays[_KINDS_ARRAY] = np.array(kinds, dtype=np.int8)
    return arrays


def _frame_from_arrays(arrays: "Dict[str, np.ndarray]") -> "pd.DataFrame":
    """Rebuild the store frame over ``arrays`` WITHOUT copying a single column.

    ``pd.DataFrame(mapping, copy=False)`` is the one construction that keeps the mapping: the
    block manager holds one (1, N) block per column and never consolidates on its own —
    ``_from_arrays``, ``concat(axis=1)`` and per-column assignment all copy, verified on pandas
    2.3.3 (spike §3.1/§3.2). Do not "tidy" it into any of those.

    Applied on BOTH paths, mapped and ``BA2_SHARED_ARRAYS=0``, so the escape hatch differs only in
    where the bytes live — never in the dtypes a consumer sees.
    """
    kinds = arrays[_KINDS_ARRAY]
    names = _utf8_split(arrays[_COLUMNS_ARRAY], len(kinds), "column names")
    mapping: "Dict[str, Any]" = {}
    for name, kind in zip(names, kinds):
        if int(kind) == _KIND_NUMERIC:
            mapping[name] = arrays[name]
        elif int(kind) == _KIND_CATEGORICAL:
            n_cats = int(arrays[name + _NCATS_SUFFIX][0])
            cats = _utf8_split(arrays[name + _CATS_SUFFIX], n_cats, f"{name} categories")
            mapping[name] = pd.Categorical.from_codes(
                arrays[name], pd.Index(cats, dtype=object),
                ordered=name in _ORDERED_CAT_COLUMNS)
        else:
            raise ValueError(f"metric store: column {name!r} has unknown kind {int(kind)}")
    return pd.DataFrame(mapping, copy=False)


def load_store(store_dir: str) -> "pd.DataFrame":
    """All month partitions as ONE DataFrame, memoised by store path (per process — GA workers
    stay alive across trials, so the store loads ~once per worker).

    The parquet is parsed by the first process on the HOST that asks for this store at this
    signature; every later process memory-maps what that build published and rebuilds the frame
    over the maps. Measured on the real store (1.28M rows x 45 cols): ~1.0 GB of private RAM saved
    per worker (4 workers: 4 087 MB -> 242 MB) and 6.3 s -> 0.35 s per worker
    (reports/strategy_research/metric_store_sharing_spike_2026-09-14.md).

    ``symbol``/``date``/``sector`` come back as ``category`` dtype — in BOTH modes, so the
    ``BA2_SHARED_ARRAYS=0`` escape hatch stays a memory switch and never a behaviour switch. Every
    consumer path (mask chains, ``sort_values``, ``groupby().head()``, ``set_index('symbol')``,
    ``list(d['symbol'])``) is unchanged by that, with ONE exception that is handled here rather
    than at the call sites: an ordered categorical refuses ``<=`` against a scalar that is not one
    of its categories, and every as-of resolve compares against a BAR date that is usually between
    scans — see ``_latest_scan_date_le``.
    """
    import glob
    hit = _STORE_MEMO.get(store_dir)
    if hit is not None:
        return hit
    # Read EVERY parquet in each month dir — classic single `part.parquet` AND the periodic-build
    # `part-NNNNN.parquet` flush files (which accumulate within a month). Both layouts load the same.
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")

    def _build() -> "Dict[str, np.ndarray]":
        # `parts`, not another glob: the files that were SIGNED are the files that are read.
        return _store_arrays_from_frame(
            pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True))

    derived = _sa.DerivedArrayStore(_sa.derived_root_for(store_dir))
    # ONE key per store dir: the derived root already mirrors the store's own directory name, so
    # two stores never share a key. ``u_`` prefix per the store's contract for caller-built keys
    # (a bare name could collide with a Windows reserved device stem).
    key = f"u_store.v{METRIC_STORE_ARRAYS_VERSION}"
    df = _frame_from_arrays(derived.build_or_open(key, parts, _build))
    _STORE_MEMO[store_dir] = df
    return df


def clear_store_memo() -> None:
    _STORE_MEMO.clear()
    _SCAN_DATES_MEMO.clear()


def recompute_price_drop_columns(store_dir: str, ohlcv_get, *,
                                 max_lookback: int = 30, drop_days: int = 5) -> Dict[str, Any]:
    """CACHE-ONLY in-place rebuild of ONLY the price-drop columns of an existing store.

    For each symbol already in the store, reads its DAILY OHLCV via ``ohlcv_get(symbol)`` (the
    caller wires a cache-only getter — e.g. under ``frozen_ttl_cache()`` so a miss raises rather
    than hitting the network), recomputes the legacy ``price_drop_pct`` (window ``drop_days``) and
    the per-window ``price_drop_pct_2..max_lookback`` columns on the daily close, samples them
    AS-OF each existing store date (latest daily <= date via ffill), and writes them back —
    consolidating each ``ym=`` month into a single ``part.parquet`` (stale flush files removed).
    Every OTHER column (market_cap/volume/float/weinstein/close/...) is left untouched.

    Symbols whose daily OHLCV is not cached (getter raises / empty) are SKIPPED with a warning;
    their drop columns keep whatever they had (windowed columns added as NaN). Returns a summary.
    """
    import glob
    drop_cols = ["price_drop_pct"] + [f"price_drop_pct_{w}" for w in range(2, int(max_lookback) + 1)]
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")
    store = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    store["date"] = store["date"].astype(str).str.slice(0, 10)
    symbols = sorted(store["symbol"].unique())

    updates: List["pd.DataFrame"] = []
    skipped: List[str] = []
    for sym in symbols:
        sdates = sorted(store.loc[store["symbol"] == sym, "date"].unique())
        try:
            ohlcv = ohlcv_get(sym)
        except Exception as e:  # noqa: BLE001 — cache miss / thin symbol must not abort the pass
            skipped.append(sym)
            logger.warning(f"recompute-drops: skipping {sym} ({type(e).__name__}: {e})")
            continue
        if ohlcv is None or getattr(ohlcv, "empty", True) or "Close" not in ohlcv.columns:
            skipped.append(sym)
            continue
        close = ohlcv["Close"].astype(float)
        close.index = pd.to_datetime(close.index)
        close = close.sort_index()
        _dw = _drop_pct_windows(close, max(int(max_lookback), int(drop_days), 1))
        per = {f"price_drop_pct_{w}": _dw[w].round(4) for w in range(2, int(max_lookback) + 1)}
        per["price_drop_pct"] = _dw[max(1, int(drop_days))].round(4)
        daily = pd.DataFrame(per).sort_index()
        target = pd.to_datetime(sdates)
        asof = daily.reindex(target, method="ffill")          # value AS-OF each store date
        u = pd.DataFrame({"symbol": sym, "date": [d.strftime("%Y-%m-%d") for d in target]})
        for c in drop_cols:
            u[c] = asof[c].to_numpy()
        updates.append(u)

    store = store.set_index(["symbol", "date"])
    for c in drop_cols:                                       # ensure target columns exist
        if c not in store.columns:
            store[c] = float("nan")
    if updates:
        upd = pd.concat(updates, ignore_index=True).set_index(["symbol", "date"])
        store.update(upd)                                     # overwrites only matching, non-NaN cells
    store = store.reset_index()

    # Consolidate each month to a single part.parquet, then drop the stale flush files so
    # load_store (reads every *.parquet) doesn't double-count rows.
    write_partitions(store_dir, store, part_name="part.parquet")
    for m in sorted(store["date"].str.slice(0, 7).unique()):
        d = os.path.join(store_dir, f"ym={m}")
        for p in glob.glob(os.path.join(d, "*.parquet")):
            if os.path.basename(p) != "part.parquet":
                os.remove(p)
    clear_store_memo()
    logger.info(f"recompute-drops: {len(symbols) - len(skipped)}/{len(symbols)} symbols "
                f"recomputed ({len(skipped)} skipped) in {store_dir}")
    return {"symbols": len(symbols), "recomputed": len(symbols) - len(skipped),
            "skipped": len(skipped), "skipped_symbols": skipped,
            "max_lookback": int(max_lookback), "drop_days": int(drop_days)}


def recompute_momentum_column(store_dir: str, ohlcv_get, *,
                              lookback: int = 252, skip: int = 21) -> Dict[str, Any]:
    """CACHE-ONLY in-place add/rebuild of ONLY the ``momentum_12_1`` column of an existing store.

    Mirrors ``recompute_price_drop_columns`` but for the precomputed 12-1 momentum factor: for each
    symbol already in the store, reads its DAILY OHLCV via ``ohlcv_get(symbol)`` (a cache-only
    getter — e.g. a direct parquet read so a miss is offline, never a network fetch), computes the
    per-bar momentum (``momentum_12_1_series``), samples it AS-OF each existing store date (latest
    daily <= date via ffill), and writes it back — consolidating each ``ym=`` month into a single
    ``part.parquet`` (stale flush files removed). Every OTHER column is left untouched.

    Used to ADD ``momentum_12_1`` to a store built before the column existed, so FactorRanker reads
    momentum point-in-time from the store instead of fetching ~400 days of daily OHLCV per symbol per
    rebalance. Insufficient-history momentum stays NaN (byte-identical to the build path
    ``compute_daily_metrics``; the FactorRanker consumer maps NaN -> 0.0). Symbols whose daily OHLCV
    is not cached (getter raises / empty) are SKIPPED with a warning (their momentum stays NaN)."""
    import glob
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")
    store = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    store["date"] = store["date"].astype(str).str.slice(0, 10)
    symbols = sorted(store["symbol"].unique())

    updates: List["pd.DataFrame"] = []
    skipped: List[str] = []
    for sym in symbols:
        sdates = sorted(store.loc[store["symbol"] == sym, "date"].unique())
        try:
            ohlcv = ohlcv_get(sym)
        except Exception as e:  # noqa: BLE001 — cache miss / thin symbol must not abort the pass
            skipped.append(sym)
            logger.warning(f"recompute-momentum: skipping {sym} ({type(e).__name__}: {e})")
            continue
        if ohlcv is None or getattr(ohlcv, "empty", True) or "Close" not in ohlcv.columns:
            skipped.append(sym)
            continue
        close = ohlcv["Close"].astype(float)
        close.index = pd.to_datetime(close.index)
        close = close.sort_index()
        mom = momentum_12_1_series(close, lookback=lookback, skip=skip).round(6)
        daily = pd.DataFrame({"momentum_12_1": mom}).sort_index()
        target = pd.to_datetime(sdates)
        asof = daily.reindex(target, method="ffill")          # value AS-OF each store date
        u = pd.DataFrame({"symbol": sym, "date": [d.strftime("%Y-%m-%d") for d in target]})
        u["momentum_12_1"] = asof["momentum_12_1"].to_numpy()
        updates.append(u)

    store = store.set_index(["symbol", "date"])
    if "momentum_12_1" not in store.columns:                  # ensure the target column exists
        store["momentum_12_1"] = float("nan")
    if updates:
        upd = pd.concat(updates, ignore_index=True).set_index(["symbol", "date"])
        store.update(upd)                                     # overwrites only matching, non-NaN cells
    store = store.reset_index()

    # Consolidate each month to a single part.parquet, then drop the stale flush files so
    # load_store (reads every *.parquet) doesn't double-count rows.
    write_partitions(store_dir, store, part_name="part.parquet")
    for m in sorted(store["date"].str.slice(0, 7).unique()):
        d = os.path.join(store_dir, f"ym={m}")
        for p in glob.glob(os.path.join(d, "*.parquet")):
            if os.path.basename(p) != "part.parquet":
                os.remove(p)
    clear_store_memo()
    logger.info(f"recompute-momentum: {len(symbols) - len(skipped)}/{len(symbols)} symbols "
                f"recomputed ({len(skipped)} skipped) in {store_dir}")
    return {"symbols": len(symbols), "recomputed": len(symbols) - len(skipped),
            "skipped": len(skipped), "skipped_symbols": skipped,
            "lookback": int(lookback), "skip": int(skip)}


def recompute_atr_columns(store_dir: str, ohlcv_get, *,
                          periods: "tuple" = ATR_PERIODS) -> Dict[str, Any]:
    """CACHE-ONLY in-place add/rebuild of the ``atr_<period>`` columns of an existing store.

    Mirrors ``recompute_momentum_column`` but for precomputed ATR (one column per period in
    ``periods``, default ``ATR_PERIODS``): for each symbol already in the store, reads its DAILY
    OHLCV via ``ohlcv_get(symbol)`` (a cache-only getter — e.g. a direct parquet read so a miss is
    offline, never a network fetch), computes per-bar ATR (``atr_series``) for every period, samples
    each AS-OF each existing store date (latest daily <= date via ffill), and writes them back —
    consolidating each ``ym=`` month into a single ``part.parquet`` (stale flush files removed).
    Every OTHER column is left untouched.

    Used to ADD ATR to a store built before the columns existed, so risk-based (ATR) position
    sizing/stops read ATR point-in-time from the store instead of needing a live indicator fetch
    (the GA/optimize trial-worker path has no hermetic route to one — see
    ``TradeRiskManagement._risk_atr_quantity``). Insufficient-history ATR stays NaN (byte-identical
    to the build path ``compute_daily_metrics``). Symbols whose daily OHLCV lacks High/Low/Close (or
    isn't cached — getter raises/empty) are SKIPPED with a warning (their ATR columns stay NaN)."""
    import glob
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")
    store = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    store["date"] = store["date"].astype(str).str.slice(0, 10)
    symbols = sorted(store["symbol"].unique())
    cols = [f"atr_{p}" for p in periods]

    updates: List["pd.DataFrame"] = []
    skipped: List[str] = []
    for sym in symbols:
        sdates = sorted(store.loc[store["symbol"] == sym, "date"].unique())
        try:
            ohlcv = ohlcv_get(sym)
        except Exception as e:  # noqa: BLE001 — cache miss / thin symbol must not abort the pass
            skipped.append(sym)
            logger.warning(f"recompute-atr: skipping {sym} ({type(e).__name__}: {e})")
            continue
        if (ohlcv is None or getattr(ohlcv, "empty", True)
                or not {"High", "Low", "Close"}.issubset(ohlcv.columns)):
            skipped.append(sym)
            continue
        idx = pd.to_datetime(ohlcv.index)
        high = ohlcv["High"].astype(float); high.index = idx
        low = ohlcv["Low"].astype(float); low.index = idx
        close = ohlcv["Close"].astype(float); close.index = idx
        order = idx.argsort()
        high, low, close = high.iloc[order], low.iloc[order], close.iloc[order]
        daily = pd.DataFrame({f"atr_{p}": atr_series(high, low, close, period=p).round(4)
                              for p in periods}).sort_index()
        target = pd.to_datetime(sdates)
        asof = daily.reindex(target, method="ffill")           # value AS-OF each store date
        u = pd.DataFrame({"symbol": sym, "date": [d.strftime("%Y-%m-%d") for d in target]})
        for c in cols:
            u[c] = asof[c].to_numpy()
        updates.append(u)

    store = store.set_index(["symbol", "date"])
    for c in cols:                                              # ensure target columns exist
        if c not in store.columns:
            store[c] = float("nan")
    if updates:
        upd = pd.concat(updates, ignore_index=True).set_index(["symbol", "date"])
        store.update(upd)                                       # overwrites only matching, non-NaN cells
    store = store.reset_index()

    # Consolidate each month to a single part.parquet, then drop the stale flush files so
    # load_store (reads every *.parquet) doesn't double-count rows.
    write_partitions(store_dir, store, part_name="part.parquet")
    for m in sorted(store["date"].str.slice(0, 7).unique()):
        d = os.path.join(store_dir, f"ym={m}")
        for p in glob.glob(os.path.join(d, "*.parquet")):
            if os.path.basename(p) != "part.parquet":
                os.remove(p)
    clear_store_memo()
    logger.info(f"recompute-atr: {len(symbols) - len(skipped)}/{len(symbols)} symbols "
                f"recomputed ({len(skipped)} skipped) in {store_dir}")
    return {"symbols": len(symbols), "recomputed": len(symbols) - len(skipped),
            "skipped": len(skipped), "skipped_symbols": skipped, "periods": list(periods)}


# The UNPREFIXED keys ``screen_universe_for_day`` (below) actually reads. The UI / optimizer /
# saved ``screener_settings`` may carry a ``screener_`` prefix (base-interface naming) and extra
# keys the metric store doesn't use — ``normalize_screener_settings`` maps an arbitrary settings
# dict to exactly this recognized, unprefixed subset so the per-bar gate gets a clean dict. This is
# the SINGLE source of truth for the recognized key vocabulary (the gate and the normalizer live
# together so they can't drift). Callers: daily_backtest_handler (UI/standalone path) AND
# strategy_optimization_handler (optimizer path) — both must normalize before the gate, else the
# prefixed keys are silently ignored and only ``market_cap_max`` filters.
METRIC_STORE_KEYS = (
    "market_cap_min", "market_cap_max", "price_min", "price_max",
    "volume_min", "volume_max", "dollar_volume_min", "float_min", "float_max",
    "relative_volume_min", "price_drop_pct", "price_drop_days",
    "weinstein_stage2_only", "max_stocks", "sort_metric",
)

# Public name is the one to import: callers that build their own screener_* -> unprefixed
# mapping by hand drift out of sync as keys are added here (FactorRanker silently dropped
# price_drop_days and float_min/max that way). Derive from this tuple + the normalizer below
# instead of hand-listing. Private alias kept for existing importers.
_METRIC_STORE_KEYS = METRIC_STORE_KEYS


def normalize_screener_settings(screener_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Map an arbitrary ``screener_settings`` dict to the metric store's recognized, UNPREFIXED key
    subset: strip a leading ``screener_`` prefix, drop keys the store doesn't use and ``None``
    values. The result is what ``screen_universe_for_day`` / ``screen_universe_as_of`` expect."""
    out: Dict[str, Any] = {}
    for k, v in (screener_settings or {}).items():
        key = k[len("screener_"):] if k.startswith("screener_") else k
        if key in _METRIC_STORE_KEYS and v is not None:
            out[key] = v
    return out


# --- symbols excluded from every screened universe -------------------------------------------
# Deliberately a SHORT, EVIDENCE-BACKED list, not a dumping ground. A symbol earns a place here
# only when its STORE DATA is wrong in a way the numeric gates cannot catch — i.e. it would be
# selected on values that do not describe a tradeable instrument.
#
# OP (added 2026-08-05, goal2020 grid). A collapsed nano-cap whose split-adjusted history explodes
# backwards: median close $47,060 in 2021 on a median FOUR shares/day, decaying to $348 on 2,357
# shares/day by 2025 — roughly $130k of daily turnover throughout, i.e. untradeable at any size.
# Its stored market_cap is computed off those adjusted prices and reads $35 BILLION in Dec 2021
# (range 1.3e6 .. 3.5e10), which is what put it in the MID band ($2-10bn) and into FMPRating's
# universe. It was also never prewarmed, so it additionally crashed 77 trials across three
# goal2020 jobs before the hermetic miss became non-fatal.
#
# The durable fix for the whole CLASS is a dollar-volume floor (see ``dollar_volume_min``), which
# is off by default because switching it on changes the universe for every run and would make new
# results non-comparable with existing ones. This list handles the one case already known to be
# poisoning a live grid.
EXCLUDED_SYMBOLS = frozenset({"OP"})


def _drop_excluded(d: "pd.DataFrame") -> "pd.DataFrame":
    """Remove EXCLUDED_SYMBOLS. Applied to every store-driven universe selection so a known-bad
    symbol cannot enter through any gate combination."""
    if not EXCLUDED_SYMBOLS or "symbol" not in d.columns:
        return d
    return d[~d["symbol"].isin(EXCLUDED_SYMBOLS)]


def _latest_scan_date_le(store_df: "pd.DataFrame", day: str) -> Optional[str]:
    """The latest scan date in ``store_df`` on or before ``day``, or None if there is none.

    WHY THIS EXISTS. ``load_store`` returns ``date`` as an ORDERED categorical, and pandas refuses
    ``series <= "2023-03-05"`` when that string is not one of the categories — which is the normal
    case, since the as-of day is a BAR date and the scan grid is weekly. So the comparison is done
    on the CODES (an int8/int16 pass -- ~24x cheaper than the object comparison it replaces:
    129 ms -> 5.4 ms on the real store, spike §1.6) and only the winning code is decoded. A string-keyed frame (a test fixture, a hand-built frame)
    still takes the original path, so both dtypes give the same answer.

    The codes are checked for PRESENCE rather than trusted from the category list: a caller may
    hand in a row-filtered frame, which keeps the full category set but not the rows.
    """
    s = store_df["date"]
    if isinstance(s.dtype, pd.CategoricalDtype):
        cats = s.cat.categories
        i = int(cats.searchsorted(str(day), side="right")) - 1
        if i < 0:
            return None
        codes = s.cat.codes.to_numpy()
        present = codes[(codes >= 0) & (codes <= i)]
        return None if not present.size else str(cats[int(present.max())])
    prior = s[s <= day]
    return None if prior.empty else str(prior.max())


def screen_universe_for_day(store_df: "pd.DataFrame", day: str,
                            settings: Dict[str, Any]) -> List[str]:
    """The dynamic per-day universe for one individual's screener thresholds.

    ``day`` is 'YYYY-MM-DD'. ``settings`` keys (all optional; absent => not enforced):
    market_cap_min/max, price_min/max, volume_min/max, relative_volume_min, price_drop_pct
    (min drop to qualify a 'dip'), weinstein_stage2_only (truthy => keep only Weinstein Stage 2
    rows, matching the slow StockScreener Stage-2 filter), max_stocks, sort_metric ('market_cap'|
    'relative_volume'|'price_drop_pct'). Returns the selected symbols (<= max_stocks), sorted by
    sort_metric desc. Pure in-memory filter over the precomputed row values — microseconds."""
    d = _drop_excluded(store_df[store_df["date"] == day])
    if d.empty:
        return []
    def _ge(col, key):
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] >= float(v)]
    def _le(col, key):
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] <= float(v)]
    _ge("market_cap", "market_cap_min"); _le("market_cap", "market_cap_max")
    _ge("price", "price_min"); _le("price", "price_max")
    _ge("volume", "volume_min"); _le("volume", "volume_max")
    # Dollar-volume floor (price x volume). OFF unless set: turning it on changes the universe for
    # every run, so it must be an explicit choice, not a silent default. This is the general form
    # of what EXCLUDED_SYMBOLS handles case-by-case -- OP passed every existing gate on a $35bn
    # phantom market cap while turning over ~$130k/day.
    _dvmin = settings.get("dollar_volume_min")
    if _dvmin is not None and float(_dvmin) > 0:
        d = d[(d["price"] * d["volume"]) >= float(_dvmin)]
    # Free float (point-in-time, baked into the store). A row with UNKNOWN float (NaN) PASSES the
    # gate (graceful degradation): this matches the column-absent skip for a pure legacy store AND
    # avoids silently dropping legacy-month symbols in an incrementally-rebuilt MIXED-schema store
    # (old months have no float_shares -> NaN after concat). A full rebuild gives every symbol a
    # real float. Absent column entirely -> no-op (older stores behave exactly as before).
    if "float_shares" in d.columns:
        _fmin = settings.get("float_min")
        if _fmin is not None and float(_fmin) > 0:
            d = d[(d["float_shares"] >= float(_fmin)) | d["float_shares"].isna()]
        _fmax = settings.get("float_max")
        if _fmax is not None and float(_fmax) > 0:
            d = d[(d["float_shares"] <= float(_fmax)) | d["float_shares"].isna()]
    _ge("relative_volume", "relative_volume_min")
    # Price-drop gate over an OPTIMIZABLE lookback window. ``price_drop_days`` (Y) selects the
    # precomputed per-window column ``price_drop_pct_<Y>`` (a multi-window store holds Y=2..max);
    # the ``price_drop_pct`` setting is the threshold. Falls back to the legacy single-window
    # ``price_drop_pct`` column when Y is unset or the windowed column is absent (older store) — so
    # the per-bar cost is unchanged (one >= on one column) and old stores behave exactly as before.
    _drop_col = "price_drop_pct"
    _y = settings.get("price_drop_days")
    if _y is not None and int(float(_y)) >= 2 and f"price_drop_pct_{int(float(_y))}" in d.columns:
        _drop_col = f"price_drop_pct_{int(float(_y))}"
    _ge(_drop_col, "price_drop_pct")
    # Weinstein Stage 2 gate (price above a RISING 30-week/150-session SMA). Truthy => keep only
    # precomputed stage-2 rows, agreeing with StockScreener._filter_by_weinstein_stage2. Absent/0
    # => no-op (existing screener-opt runs behave identically). Tolerates a missing column (an
    # older store built before this metric) by skipping the gate.
    w = settings.get("weinstein_stage2_only")
    if w is not None and float(w) > 0 and "weinstein_stage" in d.columns:
        d = d[d["weinstein_stage"] == 2]
    if d.empty:
        return []
    sort_col = settings.get("sort_metric") or "market_cap"
    if sort_col not in d.columns:
        sort_col = "market_cap"
    d = d.sort_values(sort_col, ascending=False)
    n = int(settings.get("max_stocks") or 0)
    if n > 0:
        d = d.head(n)
    return list(d["symbol"])


def screen_universe_as_of(store_df: "pd.DataFrame", as_of_day: str,
                          settings: Dict[str, Any]) -> List[str]:
    """Same as ``screen_universe_for_day`` but resolves to the LATEST scan date <= as_of_day,
    so a bar between scan dates gets the held universe (the cadence is weekly by default). Empty
    if no scan date is on/before as_of_day."""
    day = _latest_scan_date_le(store_df, as_of_day)
    if day is None:
        return []
    return screen_universe_for_day(store_df, day, settings)


def metrics_as_of(store_df: "pd.DataFrame", as_of_day: str,
                  columns: "List[str]") -> Dict[str, Dict[str, Any]]:
    """Per-symbol precomputed metric VALUES as-of ``as_of_day`` (latest scan date <= the day).

    Returns ``{symbol: {col: value, ...}}`` for the requested ``columns`` (only those present in
    the store). Mirrors ``screen_universe_as_of``'s held-as-of semantics (the scan grid is shared
    across symbols, so the latest scan <= the day is one shared date). Lets a consumer read a
    precomputed factor (e.g. ``momentum_12_1``) or the point-in-time ``close`` point-in-time
    instead of re-fetching/re-deriving it from OHLCV. Empty if no scan date is on/before the day."""
    day = _latest_scan_date_le(store_df, as_of_day)
    if day is None:
        return {}
    d = store_df[store_df["date"] == day]
    cols = [c for c in columns if c in d.columns]
    if not cols:
        return {}
    return d.set_index("symbol")[cols].to_dict("index")


def screened_symbol_union(store_df: "pd.DataFrame", start_day: str, end_day: str,
                          settings: Dict[str, Any]) -> List[str]:
    """Union of symbols ``settings`` can EVER select over a backtest window — the complete set of
    symbols the per-bar ``screen_universe_as_of`` gate can return for any bar in [start, end].

    Used to BOUND the OHLCV preload to the symbols a screener run actually touches (vs the whole
    store, which is the loosest-bound superset of every gene — e.g. 868 symbols when only ~26 are
    ever selected). Semantically equal to unioning ``screen_universe_for_day`` over every store
    scan date in ``[latest scan <= start_day, end_day]`` (bars before the first in-range scan
    resolve to that prior scan, so it must be included) — but implemented as ONE vectorized pass
    over the windowed slice instead of one full-store re-filter per date. The per-row threshold
    filters (market_cap/price/volume/float/relative_volume/price_drop/weinstein) don't depend on
    grouping, so they can be applied once across the whole window; only the max_stocks cap needs a
    per-date top-N, done via ``groupby(...).head(n)`` on the sort-order. This was previously a
    Python-level loop calling a full ``store_df[store_df["date"] == day]`` scan per date (~750
    dates for a 3-year window) — ~7.5s/call on a 946k-row store, paid once per GA individual, i.e.
    once per trial config build in the master process before dispatch (~17.5 min/generation at
    population=140). Threading was tried and made it WORSE (measured: 8 calls threaded took 84s vs
    60s sequential — this workload's Python-loop overhead dominates, so GIL contention from more
    threads only adds cost); this vectorized rewrite instead cuts the per-call cost directly, see
    ``testplatform/backend/tests/backtest`` for the equivalence test against the old logic.
    Returns sorted symbols (empty if the store has none).
    """
    dates = sorted({str(d) for d in store_df["date"].unique()})
    if not dates:
        return []
    prior = [d for d in dates if d <= start_day]
    lo = prior[-1] if prior else dates[0]
    # The UPPER bound is resolved to a real scan date too. Nothing changes for a string-keyed
    # frame (no scan date lies strictly between the last one <= end_day and end_day), but an
    # ordered categorical refuses `<=` against a day that is not one of its categories, and
    # end_day is an arbitrary backtest end.
    hi = _latest_scan_date_le(store_df, end_day)
    if hi is None:
        return []
    d = _drop_excluded(store_df[(store_df["date"] >= lo) & (store_df["date"] <= hi)])
    if d.empty:
        return []

    def _ge(col: str, key: str) -> None:
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] >= float(v)]

    def _le(col: str, key: str) -> None:
        nonlocal d
        v = settings.get(key)
        if v is not None and float(v) > 0:
            d = d[d[col] <= float(v)]

    _ge("market_cap", "market_cap_min"); _le("market_cap", "market_cap_max")
    _ge("price", "price_min"); _le("price", "price_max")
    _ge("volume", "volume_min"); _le("volume", "volume_max")
    # Dollar-volume floor (price x volume). OFF unless set: turning it on changes the universe for
    # every run, so it must be an explicit choice, not a silent default. This is the general form
    # of what EXCLUDED_SYMBOLS handles case-by-case -- OP passed every existing gate on a $35bn
    # phantom market cap while turning over ~$130k/day.
    _dvmin = settings.get("dollar_volume_min")
    if _dvmin is not None and float(_dvmin) > 0:
        d = d[(d["price"] * d["volume"]) >= float(_dvmin)]
    if "float_shares" in d.columns:
        _fmin = settings.get("float_min")
        if _fmin is not None and float(_fmin) > 0:
            d = d[(d["float_shares"] >= float(_fmin)) | d["float_shares"].isna()]
        _fmax = settings.get("float_max")
        if _fmax is not None and float(_fmax) > 0:
            d = d[(d["float_shares"] <= float(_fmax)) | d["float_shares"].isna()]
    _ge("relative_volume", "relative_volume_min")
    _drop_col = "price_drop_pct"
    _y = settings.get("price_drop_days")
    if _y is not None and int(float(_y)) >= 2 and f"price_drop_pct_{int(float(_y))}" in d.columns:
        _drop_col = f"price_drop_pct_{int(float(_y))}"
    _ge(_drop_col, "price_drop_pct")
    w = settings.get("weinstein_stage2_only")
    if w is not None and float(w) > 0 and "weinstein_stage" in d.columns:
        d = d[d["weinstein_stage"] == 2]
    if d.empty:
        return []

    sort_col = settings.get("sort_metric") or "market_cap"
    if sort_col not in d.columns:
        sort_col = "market_cap"
    d = d.sort_values(sort_col, ascending=False)
    n = int(settings.get("max_stocks") or 0)
    if n > 0:
        # observed=True: a categorical `date` (what load_store returns) otherwise enumerates
        # EVERY category as a group, including the ones this windowed slice does not contain.
        # head() itself is row-selecting so the RESULT is the same either way -- this pins the
        # intent and drops the pandas deprecation warning that comes with the default.
        d = d.groupby("date", sort=False, observed=True).head(n)
    return sorted(set(d["symbol"]))
