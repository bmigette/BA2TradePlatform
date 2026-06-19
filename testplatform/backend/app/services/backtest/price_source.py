"""As-of OHLCV price source for the backtest — the "time machine" backing store.

This is the ONLY place that knows "the current backtest bar". The engine advances
the virtual clock via ``set_clock(as_of)`` once per simulated day; ``BacktestAccount.
_get_instrument_current_price_impl`` and the fill engine delegate every price lookup
here. Because all prices come from a pre-loaded, date-keyed bar store, a run is
hermetic and reproducible (no per-call network, no wall-clock dependence).

Backing store: each symbol's bounded history is pulled ONCE via the injected ba2_providers
OHLCV provider (``get_ohlcv_data`` -> pandas DataFrame with columns ``Date, Open, High, Low,
Close, Volume``) and kept COLUMNAR — a per-symbol ascending Python key list (``date`` for
daily/coarser, tz-naive UTC ``datetime`` for intraday) plus parallel float64 OHLCV arrays
(``self._keys/_o/_h/_l/_c/_v[symbol]``). Clock lookups advance a monotonic per-symbol cursor
(O(1) amortised; the clock only moves forward); arbitrary ``as_of`` lookups bisect the key list.
A ``{"open",...}`` bar dict is materialised lazily, only for the bars actually accessed. This
replaced a dict-of-dicts (``{key: {"open",...}}``) that cost ~400 bytes/bar (~9 GB for a screened
union × 3yr × 5min) — almost all of it the ~9M tiny inner dicts; dropping them is ~98 bytes/bar
(~4× less) and loads faster (no per-bar dict allocation), with equal-or-lower per-lookup CPU.
Daily/coarser bars are keyed at midnight (the time component is dropped, mirroring ``_norm``);
intraday keys carry the full tz-naive UTC bar timestamp.

Verified against the installed ba2_providers OHLCV provider:
  * public method = ``get_ohlcv_data(symbol, start_date=, end_date=, interval=, ...)``
    -> pandas.DataFrame with columns ``Date, Open, High, Low, Close, Volume``.
"""
from __future__ import annotations

import bisect
import logging
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class BacktestCacheMiss(Exception):
    """A required symbol has no on-disk OHLCV cache anywhere a backtest reads.

    A backtest is HERMETIC: it never fetches over the network mid-run (that storms the
    provider rate limit, hangs the run, and risks lookahead). When a symbol's bars are
    absent from BOTH cache locations the backtest reads (see ``MemoizedOHLCVProvider.
    _read_cached_df``), this is raised instead of silently skipping the symbol or
    fetching it live. ``AsOfPriceSource.preload`` collects these across all symbols and
    re-raises ONE aggregated, actionable error naming what to cache.
    """

# WORKER-PERSISTENT parsed-bar cache: (symbol, interval, fetch_start_iso, end_iso) -> (bars, keys).
# Each backtest builds a fresh AsOfPriceSource, but a GA worker runs MANY individuals over the
# SAME [start-warmup, end] window — re-parsing each symbol's OHLCV DataFrame into the dict-of-dicts
# bar index per individual was a large optimizer cost (~12s/individual for a 300-symbol 5min run).
# Keyed by (symbol, interval, window) — NOT the per-run provider instance — so every individual in
# the worker reuses the parsed index built by the first one. Bytewise-identical bars (same parquet,
# same parse). Cleared via clear_worker_bar_cache() (between unrelated optimizations / in tests).
_WORKER_BAR_CACHE: Dict[Any, Any] = {}


def clear_worker_bar_cache() -> None:
    """Drop the process-wide parsed-bar cache (call between unrelated runs / in tests)."""
    _WORKER_BAR_CACHE.clear()


@lru_cache(maxsize=16)
def _is_intraday(interval: str) -> bool:
    """True for sub-daily bar intervals (1m/5m/15m/30m/1h/...). Daily and coarser
    (1d/1wk/1mo) are False — those keep calendar-date bar keys. Cached: the interval is
    constant for a run but this was called ~200k×/backtest (per price lookup)."""
    iv = (interval or "1d").lower()
    return iv.endswith("m") or iv.endswith("h") or iv.endswith("min")


@lru_cache(maxsize=4096)
def _to_datetime_cached(d: Any) -> datetime:
    return _to_datetime_impl(d)


def _to_datetime(d: Any) -> datetime:
    """Parse a datetime/date/Timestamp/ISO-string to a tz-naive UTC ``datetime``. Hot path
    (~200k calls/backtest, mostly the SAME clock value re-converted once per symbol per bar) —
    route hashable inputs through an LRU cache; fall back to the impl for unhashable ones."""
    try:
        return _to_datetime_cached(d)
    except TypeError:
        return _to_datetime_impl(d)


def _to_datetime_impl(d: Any) -> datetime:
    """Parse a datetime/date/Timestamp/ISO-string to a tz-naive UTC ``datetime``."""
    if isinstance(d, datetime):
        dt = d
    elif isinstance(d, date):
        dt = datetime(d.year, d.month, d.day)
    elif hasattr(d, "to_pydatetime"):           # pandas.Timestamp
        dt = d.to_pydatetime()
    elif isinstance(d, str):
        dt = datetime.fromisoformat(d)
    else:
        raise TypeError(f"Cannot normalise {d!r} ({type(d)}) to a datetime")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _norm(d: Any, interval: str = "1d") -> Any:
    """Normalise a datetime/date/Timestamp/ISO-string to the bar-store key.

    The key type depends on the execution interval (so daily backtests keep their
    historical behaviour and the same code serves intraday):
      * daily / coarser (1d, 1wk, 1mo) -> calendar ``date`` (time/tz dropped), as before.
      * intraday (1m..1h)              -> tz-naive UTC ``datetime`` (the full bar
                                          timestamp), so multiple bars per day are distinct.
    All keys within one run share a type because the source carries one interval.
    Raises loudly on an unparseable value rather than silently returning a wrong key.
    """
    if _is_intraday(interval):
        return _to_datetime(d)
    # Daily path — unchanged from the original date-keyed behaviour.
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if hasattr(d, "date") and callable(getattr(d, "date")):
        return d.date()
    if isinstance(d, str):
        return datetime.fromisoformat(d).date()
    raise TypeError(f"Cannot normalise {d!r} ({type(d)}) to a date key")


def _bar_from_row(row: Dict[str, Any]) -> Dict[str, float]:
    """Map one OHLCV DataFrame row (dict) to a lowercase {open,high,low,close,volume}.

    Accepts either the provider's canonical capitalised columns (Open/High/Low/Close/
    Volume) or already-lowercased keys (so a hand-built fixture works too). Fails
    loudly if a required field is missing.
    """
    def pick(*names: str) -> Any:
        for n in names:
            if n in row and row[n] is not None:
                return row[n]
        raise KeyError(f"OHLCV row missing any of {names}: keys={list(row)}")

    return {
        "open": float(pick("Open", "open")),
        "high": float(pick("High", "high")),
        "low": float(pick("Low", "low")),
        "close": float(pick("Close", "close")),
        "volume": float(pick("Volume", "volume")) if ("Volume" in row or "volume" in row) else 0.0,
    }


class AsOfPriceSource:
    """A virtual-clock, date-indexed OHLCV store driving every backtest price lookup."""

    def __init__(self, ohlcv_provider: Any, interval: str = "1d"):
        self._ohlcv = ohlcv_provider          # ba2_providers OHLCV provider (or None for pre-seeded fixtures)
        self._interval = interval
        self._intraday = _is_intraday(interval)  # cached: interval is constant for a run
        self._clock: Optional[datetime] = None
        # COLUMNAR bar store. The old store was a per-symbol dict-of-dicts ({key: {"open",...}}) at
        # ~400 bytes/bar — for a screened union × 3yr × 5min that was ~7-9 GB/worker, almost all of
        # it the ~9M tiny inner dicts. Here each symbol keeps a sorted Python key list (date for
        # daily/coarser, tz-naive UTC datetime for intraday — same objects the old store used, so
        # lookups stay native-Python-comparison fast, ~no CPU regression) PLUS parallel float64
        # OHLCV arrays. Dropping the inner dicts is the win: ~98 bytes/bar (~4× less). A bar dict is
        # materialised lazily only for the bars actually accessed, never for the whole store.
        # (datetime64 keys would be ~8× smaller but make every per-bar lookup box a numpy scalar,
        # which measured ~3× slower than the dict.get/bisect they replace — rejected.)
        self._keys: Dict[str, List[Any]] = {}    # symbol -> ascending Python date/datetime keys
        self._o: Dict[str, np.ndarray] = {}      # symbol -> float64 open  (aligned to _keys)
        self._h: Dict[str, np.ndarray] = {}      # high
        self._l: Dict[str, np.ndarray] = {}      # low
        self._c: Dict[str, np.ndarray] = {}      # close
        self._v: Dict[str, np.ndarray] = {}      # volume
        self._clock_key: Any = None      # normalised Python key of the current clock bar (set_clock)
        # Per-symbol monotonic cursor: index of the last key <= the current clock. The engine's
        # clock only ever moves forward, so a clock-based lookup advances this cursor (O(1)
        # amortised) instead of bisecting the whole list every call — so the hot path is at least
        # as fast as the old dict.get. Reset per instance (a fresh AsOfPriceSource per backtest);
        # bisect serves the rarer arbitrary-``as_of`` / next-bar lookups.
        self._cursor: Dict[str, int] = {}

    @property
    def interval(self) -> str:
        return self._interval

    @property
    def is_intraday(self) -> bool:
        return _is_intraday(self._interval)

    # ---- virtual clock -----------------------------------------------------
    def set_clock(self, as_of: datetime) -> None:
        """Advance the virtual clock to ``as_of`` (engine calls this once per bar)."""
        self._clock = as_of
        # Precompute the current bar's normalised key ONCE per bar. bar_at/close_at are called
        # millions of times per backtest, almost always for the current clock — caching the key
        # here avoids re-running _norm/_to_datetime on every lookup (a top per-bar cost).
        self._clock_key = _norm(as_of, self._interval)

    def now(self) -> datetime:
        if self._clock is None:
            raise RuntimeError(
                "AsOfPriceSource clock not set; the engine must call set_clock() per bar"
            )
        return self._clock

    def current(self) -> Optional[datetime]:
        """The current as_of clock, or None if not set yet (None-safe; never raises).

        Used by ``AsOfClampedOHLCVProvider`` to cap indicator/ATR fetches at the bar.
        """
        return self._clock

    # ---- loading -----------------------------------------------------------
    def preload(
        self,
        symbols: List[str],
        start: datetime,
        end: datetime,
        warmup_days: int,
    ) -> None:
        """Pull each symbol's bounded history once and index it by date.

        Native-cache contract: ONE fetch per symbol (a [start - warmup, end] window),
        then served sliced from memory. ``warmup_days`` extends the start backwards so
        indicators (e.g. ATR-14) have enough lookback before the first trading day.
        """
        if self._ohlcv is None:
            raise RuntimeError(
                "AsOfPriceSource.preload called with no OHLCV provider; either inject a "
                "provider or pre-seed bars via load_bars() (fixtures/tests)."
            )
        fetch_start = start - timedelta(days=warmup_days)
        win = (self._interval, fetch_start.isoformat(), end.isoformat())
        missing: List[str] = []  # symbols with NO cached series anywhere (hermetic mode)
        for sym in symbols:
            # Worker-persistent reuse: a prior individual in this worker already parsed this
            # symbol's bar index for the same window -> adopt it (no re-fetch, no re-parse).
            cached = _WORKER_BAR_CACHE.get((sym, *win))
            if cached is not None:
                (self._keys[sym], self._o[sym], self._h[sym],
                 self._l[sym], self._c[sym], self._v[sym]) = cached
                continue
            # A symbol whose cache EXISTS but has no rows in the window (e.g. a recent IPO before
            # its first bar, or a gap) loads as empty and continues — that is a legitimate data
            # gap, not an error. A symbol whose cache is ABSENT everywhere raises BacktestCacheMiss
            # (hermetic mode); collect those and fail once, loudly, after the loop (the user asked
            # for a hard error naming what to cache — never a silent skip).
            try:
                df = self._ohlcv.get_ohlcv_data(
                    sym,
                    start_date=fetch_start,
                    end_date=end,
                    interval=self._interval,
                )
            except BacktestCacheMiss:
                missing.append(sym)
                continue
            self.load_bars_df(sym, df)  # vectorized columnar build (no per-bar dict)
            _WORKER_BAR_CACHE[(sym, *win)] = (
                self._keys[sym], self._o[sym], self._h[sym],
                self._l[sym], self._c[sym], self._v[sym],
            )

        if missing:
            interval = self._interval
            sample = ", ".join(missing[:20]) + ("…" if len(missing) > 20 else "")
            raise BacktestCacheMiss(
                f"OHLCV cache miss: {len(missing)} of {len(symbols)} symbol(s) have no cached "
                f"{interval} bars on disk (e.g. {sample}). A backtest is hermetic — it never "
                f"fetches during a run. Populate the cache first, e.g.:\n"
                f"    ba2-test fetch-cache --provider fmp --timeframes {interval} "
                f"--symbols {' '.join(missing[:5])}{' …' if len(missing) > 5 else ''}\n"
                f"Expected at CACHE_FOLDER/FMPOHLCVProvider/<SYM>_{interval}.parquet "
                f"(native OHLCV cache)."
            )

    def _set_empty(self, symbol: str) -> None:
        self._keys[symbol] = []
        self._o[symbol] = np.array([], dtype=float)
        self._h[symbol] = np.array([], dtype=float)
        self._l[symbol] = np.array([], dtype=float)
        self._c[symbol] = np.array([], dtype=float)
        self._v[symbol] = np.array([], dtype=float)

    def _store(self, symbol: str, keys64: np.ndarray,
               o: np.ndarray, h: np.ndarray, l: np.ndarray,
               c: np.ndarray, v: np.ndarray) -> None:
        """Sort by key (ascending) + dedup keeping the LAST of each duplicate (byte-identical to the
        old dict-of-dicts, where a later row overwrote an earlier one with the same key), all via
        fast numpy on the datetime64 keys; then materialise the sorted Python key list (date for
        daily/coarser, tz-naive datetime for intraday) used by the lookups, and keep OHLCV columnar."""
        if not len(keys64):
            self._set_empty(symbol)
            return
        order = np.argsort(keys64, kind="stable")  # stable -> equal keys keep original order
        keys64, o, h, l, c, v = keys64[order], o[order], h[order], l[order], c[order], v[order]
        keep = np.ones(len(keys64), dtype=bool)
        keep[:-1] = keys64[1:] != keys64[:-1]  # keep only the LAST of each run of equal keys
        if not keep.all():
            keys64, o, h, l, c, v = keys64[keep], o[keep], h[keep], l[keep], c[keep], v[keep]
        objs = keys64.astype("datetime64[us]").astype(object)  # ndarray of datetime.datetime
        self._keys[symbol] = list(objs) if self._intraday else [d.date() for d in objs]
        self._o[symbol], self._h[symbol], self._l[symbol] = o, h, l
        self._c[symbol], self._v[symbol] = c, v

    def load_bars(self, symbol: str, rows: List[Dict[str, Any]]) -> None:
        """Index a list of OHLCV row dicts for ``symbol`` into the columnar store.

        Used directly by fixtures/tests (hand-built bar series). Each row must carry a date
        (``Date``/``date``) and OHLC(V) fields.
        """
        if not rows:
            self._set_empty(symbol)
            return
        n = len(rows)
        keys64 = np.empty(n, dtype="datetime64[ns]")
        o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n); v = np.empty(n)
        for i, row in enumerate(rows):
            d = _norm(row.get("Date", row.get("date")), self._interval)
            bar = _bar_from_row(row)
            keys64[i] = np.datetime64(d, "ns")
            o[i], h[i], l[i] = bar["open"], bar["high"], bar["low"]
            c[i], v[i] = bar["close"], bar["volume"]
        self._store(symbol, keys64, o, h, l, c, v)

    def load_bars_df(self, symbol: str, df: Any) -> None:
        """VECTORIZED columnar build straight from a pandas OHLCV DataFrame (the hot preload path).

        Builds the per-symbol datetime64[ns] key array + float64 OHLCV arrays in bulk — no per-bar
        Python dict (the old dict-of-dicts allocated ~9M small dicts for a 158-symbol 5min run,
        which dominated both the warmup time and the ~9 GB worker footprint)."""
        if df is None or len(df) == 0:
            self._set_empty(symbol)
            return
        import pandas as pd
        dcol = "Date" if "Date" in df.columns else "date"
        dates = pd.to_datetime(df[dcol])
        if self._intraday:
            # tz-naive UTC datetime keys (identical to _norm's intraday path).
            if getattr(dates.dt, "tz", None) is not None:
                dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
            keys64 = dates.to_numpy(dtype="datetime64[ns]")
        else:
            # daily/coarser: drop the time component (midnight) — mirrors _norm's date key.
            keys64 = dates.dt.normalize().to_numpy(dtype="datetime64[ns]")
        o = df["Open"].to_numpy(dtype=float)
        h = df["High"].to_numpy(dtype=float)
        l = df["Low"].to_numpy(dtype=float)
        c = df["Close"].to_numpy(dtype=float)
        v = (df["Volume"].to_numpy(dtype=float) if "Volume" in df.columns
             else np.zeros(len(df), dtype=float))
        self._store(symbol, keys64, o, h, l, c, v)

    # ---- queries -----------------------------------------------------------
    def _exact_index(self, symbol: str, key: Any) -> int:
        """Index of the EXACT bar at the (Python) ``key`` for ``symbol``, or -1 — mirrors the old
        ``dict.get(key)`` via bisect + equality on the ascending Python key list."""
        k = self._keys.get(symbol)
        if not k:
            return -1
        i = bisect.bisect_left(k, key)
        return i if i < len(k) and k[i] == key else -1

    def _cursor_at_clock(self, symbol: str) -> int:
        """Index of the last key <= the current clock for ``symbol``, advancing a per-symbol
        monotonic cursor. O(1) amortised — the engine's clock only moves forward, so across a whole
        run a cursor advances at most ``len(keys)`` times total — using fast native date/datetime
        comparisons. Returns -1 if no bar <= clock yet."""
        k = self._keys.get(symbol)
        if not k:
            return -1
        n = len(k)
        cur = self._cursor.get(symbol, -1)
        ck = self._clock_key
        # Advance while the NEXT key is still <= the clock (handles multi-bar jumps for a symbol not
        # queried every bar). Never moves backward (clock is monotonic).
        while cur + 1 < n and k[cur + 1] <= ck:
            cur += 1
        self._cursor[symbol] = cur
        return cur

    def has_symbol(self, symbol: str) -> bool:
        return bool(self._keys.get(symbol))

    def bar_at(self, symbol: str, as_of: Optional[datetime] = None) -> Optional[Dict[str, float]]:
        """The bar for ``symbol`` on the as-of bar (or current clock bar), or None."""
        if as_of is None:
            if self._clock is None:
                raise RuntimeError(
                    "AsOfPriceSource clock not set; the engine must call set_clock() per bar"
                )
            cur = self._cursor_at_clock(symbol)  # O(1) amortised clock cursor (hot path)
            if cur < 0 or self._keys[symbol][cur] != self._clock_key:
                return None  # no EXACT bar at the clock
            i = cur
        else:
            i = self._exact_index(symbol, _norm(as_of, self._interval))
            if i < 0:
                return None
        return {"open": float(self._o[symbol][i]), "high": float(self._h[symbol][i]),
                "low": float(self._l[symbol][i]), "close": float(self._c[symbol][i]),
                "volume": float(self._v[symbol][i])}

    def close_at(self, symbol: str, as_of: Optional[datetime] = None) -> Optional[float]:
        """Close price for ``symbol`` on the as-of day (or current clock day), or None."""
        if as_of is None:
            if self._clock is None:
                raise RuntimeError(
                    "AsOfPriceSource clock not set; the engine must call set_clock() per bar"
                )
            cur = self._cursor_at_clock(symbol)
            if cur < 0 or self._keys[symbol][cur] != self._clock_key:
                return None
            return float(self._c[symbol][cur])
        i = self._exact_index(symbol, _norm(as_of, self._interval))
        return float(self._c[symbol][i]) if i >= 0 else None

    def close_asof(self, symbol: str, as_of: Optional[datetime] = None) -> Optional[float]:
        """Last-known close AT OR BEFORE the clock (forward-fill), or None if never priced.

        For VALUING a held position on a bar where the symbol has no EXACT bar — the trading
        clock is the union of every symbol's timestamps, so a held symbol routinely lacks a
        bar on ticks driven by other symbols (and on data gaps / half-days / split days).
        ``close_at`` returns None there, which previously made the position vanish from the
        equity MTM ($0) and produced spurious 90%+ drawdowns. Uses the monotonic clock cursor
        (most recent bar <= clock). Valuation-only: TP/SL fill checks still use ``bar_at``/
        ``next_bar`` against EXACT bars (never a forward-filled one)."""
        if as_of is None:
            if self._clock is None:
                return None
            cur = self._cursor_at_clock(symbol)
            return float(self._c[symbol][cur]) if cur >= 0 else None
        k = self._keys.get(symbol)
        if not k:
            return None
        i = bisect.bisect_right(k, _norm(as_of, self._interval)) - 1
        return float(self._c[symbol][i]) if i >= 0 else None

    def next_bar(self, symbol: str, after: datetime) -> Optional[Dict[str, float]]:
        """The NEXT trading bar strictly after ``after`` (for next-bar fills)."""
        k = self._keys.get(symbol)
        if not k:
            return None
        i = bisect.bisect_right(k, _norm(after, self._interval))
        if i >= len(k):
            return None
        return {"open": float(self._o[symbol][i]), "high": float(self._h[symbol][i]),
                "low": float(self._l[symbol][i]), "close": float(self._c[symbol][i]),
                "volume": float(self._v[symbol][i])}

    def next_bar_date(self, symbol: str, after: datetime) -> Optional[Any]:
        """The key of the next trading bar strictly after ``after`` (date or datetime), or None.

        Binary-searches the symbol's ascending Python key list (``bisect_right`` -> first key
        strictly greater than the cutoff) — O(log n)."""
        k = self._keys.get(symbol)
        if not k:
            return None
        i = bisect.bisect_right(k, _norm(after, self._interval))
        return k[i] if i < len(k) else None

    def all_dates(self) -> List[Any]:
        """Sorted union of all bar keys across every loaded symbol (the trading clock).

        Keys are ``date`` for daily/coarser intervals and ``datetime`` for intraday.
        """
        seen: set = set()
        for k in self._keys.values():
            seen.update(k)
        return sorted(seen)


def _to_utc(d: Any) -> datetime:
    """Normalise a datetime/date/Timestamp/ISO-string to a tz-aware UTC datetime."""
    if isinstance(d, datetime):
        return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if hasattr(d, "to_pydatetime"):  # pandas.Timestamp
        dd = d.to_pydatetime()
        return dd if dd.tzinfo is not None else dd.replace(tzinfo=timezone.utc)
    if isinstance(d, str):
        dd = datetime.fromisoformat(d)
        return dd if dd.tzinfo is not None else dd.replace(tzinfo=timezone.utc)
    raise TypeError(f"Cannot normalise {d!r} ({type(d)}) to a datetime")


class AsOfClampedOHLCVProvider:
    """Backtest-only OHLCV wrapper that caps every ``get_ohlcv_data(end_date=...)`` at the
    price source's current as_of clock.

    The indicator calc (``PandasIndicatorCalc``) and ATR sizing (``position_sizing.
    get_latest_atr``) fetch with ``end_date=datetime.now()`` (wall clock). In a backtest that
    would pull bars from AFTER the simulated bar, leaking future data into the (causal)
    indicator/ATR used for rule conditions and position sizing. Wrapping the OHLCV provider
    here clamps any future end_date down to the current bar, so the indicator path is
    as_of-correct regardless of what end_date the caller requests. The LIVE path uses the
    unwrapped provider (its clock is wall-time, which is correct), so this is backtest-scoped.

    Everything other than ``get_ohlcv_data`` is delegated to the inner provider.
    """

    def __init__(self, inner: Any, price_source: AsOfPriceSource):
        self._inner = inner
        self._ps = price_source

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kwargs):
        asof = self._ps.current()
        if asof is not None and (end_date is None or _to_utc(end_date) > _to_utc(asof)):
            end_date = asof  # cap the fetch at the current backtest bar
        return self._inner.get_ohlcv_data(
            symbol, start_date=start_date, end_date=end_date, interval=interval, **kwargs
        )

    def __getattr__(self, name):  # delegate every other attribute/method to the inner provider
        return getattr(self._inner, name)


def _df_to_rows(df: Any) -> List[Dict[str, Any]]:
    """Convert a pandas OHLCV DataFrame (Date/Open/High/Low/Close/Volume) to row dicts.

    Kept here (not in load_bars) so load_bars stays pandas-free for fixtures/tests.
    """
    if df is None or len(df) == 0:
        return []
    # to_dict("records") yields one dict per row with the column names as keys.
    return df.to_dict("records")


# ---------------------------------------------------------------------------
# In-memory OHLCV memo (shared across every trial a worker process runs)
# ---------------------------------------------------------------------------
# Process-global cache of each symbol's FULL bounded OHLCV series. Key:
# (symbol, interval, bounds_start_iso, bounds_end_iso). Value: (DataFrame, dates_ndarray).
# A backtest asks for the same symbol's bars on EVERY bar (price_at_date) plus indicator/ATR
# lookbacks — all sub-ranges of one fixed window. Re-reading the disk cache + re-parsing dates
# per call dominated runtime (~370s of an 836s 6-month run). Caching the full series here, at
# MODULE level, means it is paid ~once per worker and reused across the whole GA population
# (the pool workers stay alive across trials), not once per call.
_FULL_SERIES_MEMO: Dict[tuple, Any] = {}


def clear_ohlcv_memo() -> None:
    """Drop the process-global OHLCV memo (tests / between distinct universes)."""
    _FULL_SERIES_MEMO.clear()


class MemoizedOHLCVProvider:
    """Wrap an OHLCV provider so each symbol's full [bounds] series is fetched ONCE per worker
    process and every ``get_ohlcv_data`` call is served by an in-memory date-range slice.

    ``bounds`` is the widest window the run needs (start - warmup .. end). The first request for
    a symbol fetches+parses that window once (module memo, shared across trials); every later
    request — same symbol, any sub-range, any later trial in the population — is an O(log n)
    ``searchsorted`` slice with no disk read and no date re-parsing. Non-OHLCV attributes are
    delegated to the inner provider unchanged.
    """

    def __init__(self, inner: Any, bounds_start: Any, bounds_end: Any, interval: str = "1d",
                 cached_only: bool = False):
        self._inner = inner
        self._bs = bounds_start
        self._be = bounds_end
        self._interval = interval
        # HERMETIC backtest mode: when True, the series is read ONLY from the on-disk OHLCV
        # caches (never a live network fetch). A screener optimization's candidate universe can
        # be hundreds of symbols; fetching missing ones mid-backtest storms the FMP rate limit
        # (429 backoff -> the run hangs for many minutes) and is lookahead-prone. A backtest is
        # meant to be hermetic: the data is pre-built (ba2-test fetch-cache / build-screener-
        # metrics). When a symbol is absent from EVERY cache location we read, raise
        # BacktestCacheMiss (the user asked for a hard error, NOT a silent skip) so preload can
        # report exactly what to cache. cached_only=False keeps the live passthrough (fetch).
        self._cached_only = cached_only

    def _read_cached_df(self, symbol: str, interval: str):
        """Read a symbol's full OHLCV series from the native on-disk cache. None on miss.

        Reads ``CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet`` — the single native
        parquet cache that ``MarketDataProviderInterface.get_ohlcv_data`` AND ``ba2-test
        fetch-cache`` (via ohlcv_cache_provider) both write. Returns a DataFrame (columns
        Date,Open,High,Low,Close,Volume[,effective_date]) or None when the file is absent.
        """
        import pandas as pd

        try:
            from ba2_common.core import native_cache
            # Resolve any interval-alias spelling on disk (canonical "5m" + legacy "5min", etc.).
            # MUST use find_timeseries_path, not timeseries_path: the latter returns only the
            # canonical write path, so a legacy-spelled cache file ("<SYM>_5min.parquet") would
            # be a false miss -> a spurious BacktestCacheMiss on data that is actually cached.
            p = native_cache.find_timeseries_path(type(self._inner).__name__, symbol, interval)
            if p is not None:
                return pd.read_parquet(p)
        except Exception:  # pragma: no cover
            pass
        return None

    def _full(self, symbol: str, interval: str):
        import numpy as np
        import pandas as pd

        key = (symbol, interval, _to_utc(self._bs).isoformat(), _to_utc(self._be).isoformat())
        hit = _FULL_SERIES_MEMO.get(key)
        if hit is None:
            if self._cached_only:
                # Hermetic: read from the on-disk caches only (both layouts). Absent from every
                # layout -> hard error (aggregated by preload), never a silent skip or live fetch.
                df = self._read_cached_df(symbol, interval)
                if df is None:
                    # Only a REAL, network-backed provider (FMPOHLCVProvider et al — they expose
                    # get_provider_name) must error on miss; an in-memory test/synthetic provider
                    # (no get_provider_name, no network) is served directly so fixtures still work.
                    if hasattr(self._inner, "get_provider_name"):
                        raise BacktestCacheMiss(symbol)
                    df = self._inner.get_ohlcv_data(
                        symbol, start_date=self._bs, end_date=self._be, interval=interval
                    )
                elif len(df) and "Date" in df.columns:
                    # _read_cached_df returns the WHOLE on-disk series; clamp to [bounds] so the
                    # memo matches the live path (get_ohlcv_data(start,end)) and memory stays bounded.
                    _d = pd.to_datetime(df["Date"], utc=True).dt.tz_localize(None)
                    _bs = _to_utc(self._bs).replace(tzinfo=None)
                    _be = _to_utc(self._be).replace(tzinfo=None)
                    df = df[(_d >= _bs) & (_d <= _be)]
            else:
                df = self._inner.get_ohlcv_data(
                    symbol, start_date=self._bs, end_date=self._be, interval=interval
                )
            if df is None or len(df) == 0:
                import pandas as _pd
                df = _pd.DataFrame() if df is None else df
                dates = np.array([], dtype="datetime64[ns]")
            else:
                df = df.reset_index(drop=True)
                dates = (
                    pd.to_datetime(df["Date"], utc=True).dt.tz_localize(None).values
                ).astype("datetime64[ns]")
                order = np.argsort(dates, kind="stable")
                df = df.iloc[order].reset_index(drop=True)
                dates = dates[order]
            hit = (df, dates)
            _FULL_SERIES_MEMO[key] = hit
        return hit

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kwargs):
        import numpy as np

        df, dates = self._full(symbol, interval)
        if len(df) == 0:
            return df
        lo = 0
        hi = len(df)
        if start_date is not None:
            s = np.datetime64(_to_utc(start_date).replace(tzinfo=None))
            lo = int(np.searchsorted(dates, s, side="left"))
        if end_date is not None:
            e = np.datetime64(_to_utc(end_date).replace(tzinfo=None))
            hi = int(np.searchsorted(dates, e, side="right"))
        return df.iloc[lo:hi].reset_index(drop=True)

    def __getattr__(self, name):
        return getattr(self._inner, name)
