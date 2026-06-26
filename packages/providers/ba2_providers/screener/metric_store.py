"""Precomputed, exportable screener METRIC STORE for screener-settings optimization.

The FMP screener universe (current actively-trading US names — survivorship-biased by design)
is enumerated once, then each symbol's per-day screen metrics are computed VECTORISED from the
already-disk-cached OHLCV and written as date-partitioned parquet (exportable; extend by adding
partitions). At optimize time the store loads into pandas and each GA individual filters it
per day. No server.
"""
from __future__ import annotations
import os
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from ba2_providers.fmp_common import fmp_http_get
from ba2_common.logger import logger

_SCREENER_URL = "https://financialmodelingprep.com/api/v3/stock-screener"
# Point-in-time fundamentals (fetched ONCE at build time, baked into the store, disk-cached):
#  * historical-market-capitalization -> daily market cap (correct across buybacks/issuance/splits)
#  * v4 historical/shares_float        -> free float over time
_HIST_MCAP_URL = "https://financialmodelingprep.com/api/v3/historical-market-capitalization"
_HIST_FLOAT_URL = "https://financialmodelingprep.com/api/v4/historical/shares_float"


def _fund_cache_path(kind: str, symbol: str) -> str:
    """Disk-cache path for a per-symbol historical fundamentals series (so a re-build never
    re-fetches). ``kind`` in {'market_cap','float'}. Under CACHE_FOLDER/screener_fundamentals."""
    import ba2_common.config as _cfg  # read at call time so tests rebinding CACHE_FOLDER win
    d = os.path.join(_cfg.CACHE_FOLDER, "screener_fundamentals", kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol.upper()}.parquet")


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
    row's date is the market date, so an as-of (ffill <= scan date) read is point-in-time."""
    path = _fund_cache_path("market_cap", symbol)
    if os.path.exists(path):
        try:
            return _series_from_cache_df(pd.read_parquet(path), "market_cap")
        except Exception:  # noqa: BLE001 — corrupt cache -> re-fetch
            pass
    rows: list = []
    try:
        r = fmp_http_get(f"{_HIST_MCAP_URL}/{symbol}",
                         params={"apikey": api_key, "from": start, "to": end, "limit": 100000},
                         endpoint="historical-market-cap", timeout=30)
        j = r.json()
        rows = j if isinstance(j, list) else []
    except Exception:  # noqa: BLE001 — per-symbol fetch failure -> empty (mcap NaN, dropped by filter)
        rows = []
    df = pd.DataFrame(
        [{"date": x.get("date"), "market_cap": x.get("marketCap")}
         for x in rows if isinstance(x, dict) and x.get("date")]
    )
    if not df.empty:
        try:
            _write_parquet_atomic(df, path)
        except Exception:  # noqa: BLE001 — cache write best-effort
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
    nothing (the float filter then degrades gracefully — unknown float passes the gate)."""
    path = _fund_cache_path("float", symbol)
    if os.path.exists(path):
        try:
            return _series_from_cache_df(pd.read_parquet(path), "float_shares")
        except Exception:  # noqa: BLE001
            pass
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
    if not df.empty:
        try:
            _write_parquet_atomic(df, path)
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
    out = pd.DataFrame({
        "close": close,
        "market_cap": mcap,
        "volume": volume.round(2),
        "relative_volume": rvol.round(4),
        "price_drop_pct": drop_pct.round(4),
        "weinstein_stage": stage,
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
                flush_every: int = 250) -> Dict[str, Any]:
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
    threads overlap IO/network safely. Returns {symbols, months_written, months_skipped, cadence_days}.
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

    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        for res in ex.map(lambda kv: _build_one(kv[0], kv[1]), items):
            if res is not None and not res.empty:
                frames.append(res)
                if fe > 0 and len(frames) >= fe:
                    _flush()
    _flush()  # final remainder
    return {"symbols": len(static_by_sym), "months_written": len(todo_months),
            "months_skipped": len(set(have) & set(want_months)), "cadence_days": cadence_days}


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


def load_store(store_dir: str) -> "pd.DataFrame":
    """Load all month partitions into one DataFrame, memoised by store path (per process —
    GA workers stay alive across trials, so the store loads ~once per worker)."""
    import glob
    hit = _STORE_MEMO.get(store_dir)
    if hit is not None:
        return hit
    # Read EVERY parquet in each month dir — classic single `part.parquet` AND the periodic-build
    # `part-NNNNN.parquet` flush files (which accumulate within a month). Both layouts load the same.
    parts = sorted(glob.glob(os.path.join(store_dir, "ym=*", "*.parquet")))
    if not parts:
        raise FileNotFoundError(f"empty screener metric store: {store_dir}")
    df = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
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


def screen_universe_for_day(store_df: "pd.DataFrame", day: str,
                            settings: Dict[str, Any]) -> List[str]:
    """The dynamic per-day universe for one individual's screener thresholds.

    ``day`` is 'YYYY-MM-DD'. ``settings`` keys (all optional; absent => not enforced):
    market_cap_min/max, price_min/max, volume_min/max, relative_volume_min, price_drop_pct
    (min drop to qualify a 'dip'), weinstein_stage2_only (truthy => keep only Weinstein Stage 2
    rows, matching the slow StockScreener Stage-2 filter), max_stocks, sort_metric ('market_cap'|
    'relative_volume'|'price_drop_pct'). Returns the selected symbols (<= max_stocks), sorted by
    sort_metric desc. Pure in-memory filter over the precomputed row values — microseconds."""
    d = store_df[store_df["date"] == day]
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
    dates = store_df["date"]
    prior = dates[dates <= as_of_day]
    if prior.empty:
        return []
    return screen_universe_for_day(store_df, prior.max(), settings)


def screened_symbol_union(store_df: "pd.DataFrame", start_day: str, end_day: str,
                          settings: Dict[str, Any]) -> List[str]:
    """Union of symbols ``settings`` can EVER select over a backtest window — the complete set of
    symbols the per-bar ``screen_universe_as_of`` gate can return for any bar in [start, end].

    Used to BOUND the OHLCV preload to the symbols a screener run actually touches (vs the whole
    store, which is the loosest-bound superset of every gene — e.g. 868 symbols when only ~26 are
    ever selected). Unions ``screen_universe_for_day`` over every store scan date in
    ``[latest scan <= start_day, end_day]`` (bars before the first in-range scan resolve to that
    prior scan, so it must be included). Returns sorted symbols (empty if the store has none).
    """
    dates = sorted({str(d) for d in store_df["date"].unique()})
    if not dates:
        return []
    prior = [d for d in dates if d <= start_day]
    lo = prior[-1] if prior else dates[0]
    relevant = [d for d in dates if lo <= d <= end_day]
    union: set = set()
    for d in relevant:
        union.update(screen_universe_for_day(store_df, d, settings))
    return sorted(union)
