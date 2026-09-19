"""DeterministicScorer - data fetchers.

All fetches go through the ProviderBundle (backtest: parquet/SQLite as_of
cache, hermetic; live: provider TTL caches). Point-in-time is enforced by the
platform providers themselves (fundamentals interfaces take as_of_date /
end_date semantics), plus explicit OHLCV slicing to <= as_of here.

Fetcher layout follows the FactorRanker pattern: module-level functions, no
expert instance required, so the testplatform --prewarm hook can call them and
GA workers share one fetch per symbol.

ERROR POLICY: every broad handler calls ``absorb_if_benign`` first. Only OSError
(a genuine network/disk outage) is absorbed into a degraded result; everything
else -- above all ``FMPHermeticViolation`` and the cache-miss errors -- must
propagate. Swallowing those turns "this backtest silently reached the network /
ran on missing data" into a plausible-looking score, which is the exact failure
mode the hermetic guard exists to make loud. ``ReplayMiss`` is re-raised BEFORE
``absorb_if_benign`` for the same reason and independently of ``BA2_ERROR_MODE``:
an offline replay whose tape lacks a response must stop and say so, never degrade
to an empty section that then compares as a plausible bundle.
"""
from __future__ import annotations

import itertools
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ba2_common.core.failure_modes import absorb_if_benign
from ba2_common.core.replay import (
    ReplayMiss,
    ReplayStatus,
    record_observation,
    replay_now,
)
from ba2_common.logger import logger
from ba2_providers.fmp_common import TTLCache

# Process-wide caches keyed by symbol (NOT by as_of): the fetched payloads are
# time-invariant within the TTL window; as_of slicing happens below and in the
# pure calculators. Shared across instances + GA workers (FMPRating pattern).
_TTL_SECONDS = 900
_OHLCV_CACHE = TTLCache(_TTL_SECONDS)
# OHLCV needs range-aware caching (a payload is only reusable if it COVERS the
# requested window), which TTLCache's key/value shape cannot express -- so the
# frames live here as {key: (covered_from, df)} instead.
_OHLCV_COVERAGE: dict = {}
# One OhlcvView per cached frame (same keys as _OHLCV_COVERAGE): the parsed
# dates + column arrays + the fingerprint the technical series memo keys on.
_OHLCV_VIEWS: dict = {}
_OHLCV_EPOCH = itertools.count()
# (index_symbol, as_of, lookback) -> (frame fingerprint, close Series). Every
# symbol in a batch asks for the SAME index slice on the same bar; without this
# the SPY window was rebuilt once per (symbol, bar).
_INDEX_CLOSES_MEMO: "OrderedDict[tuple, tuple]" = OrderedDict()
_INDEX_CLOSES_MAX = 8
_STATEMENTS_CACHE = TTLCache(_TTL_SECONDS)
_GRADES_CACHE = TTLCache(_TTL_SECONDS)
_MACRO_CACHE = TTLCache(3600)

# History needed for a 252d momentum + 200d SMA + indicator buffers.
OHLCV_LOOKBACK_DAYS = 600

INDEX_SYMBOL = "SPY"

# The FRED series ``fetch_macro_series`` reads, in the order it reads them. Declared
# here rather than inline in the fetcher so the replay-dependency adapter
# (``ba2_experts.replay_dependencies``) names EXACTLY what the fetcher reads: a second
# hand-kept copy of this tuple is how a warm plan silently stops covering a series the
# expert still asks for. Every id must exist in ``fred_series.SERIES_SPEC``.
MACRO_SERIES_IDS = ("VIXCLS", "UNRATE", "BAA10Y", "T10Y3M")


def reset_caches() -> None:
    """Drop every process-wide cache (tests, and the live /api/reload path).

    TTLCache exposes get_or_call/invalidate, not a bulk clear, so the store is
    reset under its own lock.
    """
    _OHLCV_COVERAGE.clear()
    _OHLCV_VIEWS.clear()
    _INDEX_CLOSES_MEMO.clear()
    # The technical series memo is keyed on an OhlcvView fingerprint, so dropping
    # the frames without dropping it would leave orphans behind (never stale
    # hits -- the epoch in the fingerprint is unique per stored frame -- but
    # ~18MB of arrays nobody can reach again).
    from . import technical
    technical.reset_series_memo()
    for cache in (_OHLCV_CACHE, _STATEMENTS_CACHE, _GRADES_CACHE, _MACRO_CACHE):
        with cache._lock:               # type: ignore[attr-defined]
            cache._store.clear()        # type: ignore[attr-defined]


def _normalized_dates(df: pd.DataFrame) -> tuple:
    """(int64 ns values, tz_aware) for the frame's Date column.

    The comparison ``_slice_to_as_of`` makes, hoisted out of the per-decision
    path: ``pd.to_datetime`` over the whole column ran on EVERY bar of EVERY
    symbol against an immutable cached frame.
    """
    dates = pd.to_datetime(df["Date"])
    tz_aware = dates.dt.tz is not None
    if tz_aware:
        dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
    return dates.to_numpy(dtype="datetime64[ns]").astype("int64"), tz_aware


def _cutoff_ns(as_of: datetime, tz_aware: bool) -> int:
    """``as_of`` in the same units/awareness as ``_normalized_dates``.

    Mixing awarenesses raises TypeError, and a bare try/except around it would
    silently return the unsliced frame -- i.e. lookahead. So the coercion is
    explicit and identical to the one the boolean-mask version did.
    """
    cutoff = pd.Timestamp(as_of)
    if not tz_aware:
        return int((cutoff.tz_localize(None) if cutoff.tz is not None else cutoff).value)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tz is None else cutoff.tz_convert("UTC")
    return int(cutoff.value)


class OhlcvView:
    """The full cached OHLCV frame for one symbol, plus everything derived from
    it that used to be recomputed once per decision.

    FINGERPRINT / STALENESS. ``fingerprint`` carries a process-unique epoch
    stamped when the payload was stored, the row count, and the last bar's date.
    A frame that changed -- a live re-run after a new bar, a re-fetch over a
    wider window -- produces a NEW view with a NEW epoch, so nothing keyed on the
    old fingerprint can be served for it. ``reset_caches()`` drops views and the
    series memo together.
    """

    __slots__ = ("symbol", "frame", "nrows", "fingerprint", "date_ns", "tz_aware",
                 "ascending", "raw_dates", "closes", "close_s", "high_s", "low_s")

    def __init__(self, symbol: str, df: pd.DataFrame) -> None:
        self.symbol = symbol
        self.frame = df
        self.nrows = len(df)
        self.date_ns, self.tz_aware = _normalized_dates(df)
        # searchsorted is only a legal substitute for the boolean mask when the
        # dates ascend; a non-monotonic frame keeps the mask (and the fast
        # indicator path refuses it, because a slice is then not a prefix).
        self.ascending = bool(np.all(np.diff(self.date_ns) > 0)) if self.nrows > 1 else True
        self.raw_dates = df["Date"].to_numpy()
        self.closes = df["Close"].to_numpy(dtype=float, copy=False)
        # Clean RangeIndex Series for the indicator builders: the provider's own
        # index may be anything, and ``pd.concat(..., axis=1)`` inside the ADX/ATR
        # true-range aligns on it.
        self.close_s = pd.Series(self.closes)
        self.high_s = pd.Series(df["High"].to_numpy(dtype=float, copy=False))
        self.low_s = pd.Series(df["Low"].to_numpy(dtype=float, copy=False))
        self.fingerprint = (next(_OHLCV_EPOCH), self.nrows,
                            int(self.date_ns[-1]) if self.nrows else None)

    def rows_through(self, as_of: Optional[datetime]) -> int:
        """How many rows are dated <= as_of (the causal prefix length)."""
        if as_of is None:
            return self.nrows
        return int(np.searchsorted(self.date_ns, _cutoff_ns(as_of, self.tz_aware),
                                   side="right"))

    def position_of_prefix(self, df: pd.DataFrame) -> Optional[int]:
        """The bar index `df` ends on, IF `df` is a leading prefix of this frame.

        Returns None whenever that cannot be established -- a synthetic frame in
        a test, a replayed bundle whose cached frame is gone, a non-monotonic
        payload. The caller then recomputes from the slice, which is the same
        arithmetic on the same rows.
        """
        if df is None or not self.ascending or self.nrows == 0:
            return None
        n = len(df)
        if n == 0 or n > self.nrows:
            return None
        if "Close" not in df.columns or "Date" not in df.columns:
            return None
        pos = n - 1
        closes = df["Close"].to_numpy(dtype=float, copy=False)
        dates = df["Date"].to_numpy()
        if not (closes[0] == self.closes[0] and closes[pos] == self.closes[pos]):
            return None
        if not (dates[0] == self.raw_dates[0] and dates[pos] == self.raw_dates[pos]):
            return None
        return pos


def frame_view(symbol: str, lookback_days: Optional[int] = None) -> Optional[OhlcvView]:
    """The cached OhlcvView for `symbol`, or None if nothing is cached.

    A pure lookup -- never fetches. ``technical_score`` uses it to index memoised
    indicator series instead of rebuilding them from the slice; a miss simply
    costs the old per-slice path.
    """
    key = _ohlcv_key(symbol, OHLCV_LOOKBACK_DAYS if lookback_days is None else lookback_days)
    return _OHLCV_VIEWS.get(key)


def _ohlcv_key(symbol: str, lookback_days: int) -> str:
    return f"ohlcv|{symbol}|{lookback_days}"


def _slice_to_as_of(df: pd.DataFrame, as_of: Optional[datetime],
                    view: Optional[OhlcvView] = None) -> Optional[pd.DataFrame]:
    """Causal slice: keep rows dated <= as_of, matching the frame's tz-awareness.

    With a `view` for this exact frame the cut is a searchsorted on the dates
    parsed ONCE when the payload was cached; without one it falls back to parsing
    the column and masking, which is what this always did.
    """
    if df is None or df.empty or "Date" not in df.columns:
        return df
    if as_of is None:
        return df
    if view is not None and view.frame is df and view.ascending:
        # .copy() so the returned slice is independent of the cached payload,
        # exactly as the boolean mask below always was.
        return df.iloc[:view.rows_through(as_of)].copy()
    dates = pd.to_datetime(df["Date"])
    cutoff = pd.Timestamp(as_of)
    if dates.dt.tz is None:
        cutoff = cutoff.tz_localize(None) if cutoff.tz is not None else cutoff
    else:
        cutoff = cutoff.tz_localize("UTC") if cutoff.tz is None else cutoff.tz_convert("UTC")
        dates = dates.dt.tz_convert("UTC")
    return df[dates <= cutoff]


def fetch_ohlcv(providers, symbol: str, as_of: Optional[datetime],
                lookback_days: int = OHLCV_LOOKBACK_DAYS) -> Optional[pd.DataFrame]:
    """Daily OHLCV ascending by date, sliced to <= as_of (causal).

    ONE provider fetch per symbol per run (plan §4): the payload is cached on the
    symbol alone and each bar gets its own local slice. Keying the cache by as_of
    -- or bypassing it whenever as_of is set, as this used to -- re-pulled the
    full 600-day window for every symbol on every bar of every GA trial.
    """
    # The cached payload must COVER every bar that will ask for it. Bars advance
    # forward, so anchoring the window on the FIRST as_of seen (minus the
    # lookback) and ending at "now" covers the whole run in one fetch. Anchoring
    # it on `now` instead silently returns an empty causal slice for every
    # historical bar -- the backtest then trades nothing and looks merely idle.
    # replay_now(None), not _utcnow(): BOTH ends of this window reach the OHLCV
    # provider's REQUEST IDENTITY, and an identity key holding a raw wall-clock
    # value can never be matched again -- the recorded request would be
    # unreproducible by construction (see ba2_common.core.replay.observe). Passing
    # None (never as_of) keeps the semantics exactly as they were: the window ends
    # at "now" even in a backtest, because bars advance forward into it.
    now = replay_now(None)
    need_from = ((as_of or now) - timedelta(days=lookback_days)).replace(tzinfo=None)
    key = _ohlcv_key(symbol, lookback_days)
    covered_from, df = _OHLCV_COVERAGE.get(key, (None, None))
    if df is None or (covered_from is not None and need_from < covered_from):
        try:
            df = providers.ohlcv().get_ohlcv_data(
                symbol=symbol, start_date=need_from, end_date=now, interval="1d")
        except ReplayMiss:
            raise
        except ReplayMiss:
            raise
        except Exception as e:          # noqa: BLE001 - hermetic/defect errors re-raise
            absorb_if_benign(e)
            logger.warning("DeterministicScorer OHLCV fetch failed for %s: %s", symbol, e)
            return None
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            return None
        _OHLCV_COVERAGE[key] = (need_from, df)
        # A NEW payload means a NEW view and a new epoch in its fingerprint, so
        # nothing memoised against the previous frame can be served for it.
        _OHLCV_VIEWS[key] = _build_view(symbol, df)
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    out = _slice_to_as_of(df, as_of, _OHLCV_VIEWS.get(key))
    if out is None or out.empty:
        return None
    return out.reset_index(drop=True)


def _build_view(symbol: str, df: pd.DataFrame) -> Optional[OhlcvView]:
    """An OhlcvView for a freshly cached payload, or None if it cannot carry one.

    A frame without Date/High/Low (or with unparseable dates) simply gets no
    view: slicing falls back to the mask and the indicators to the per-slice
    path. Both produce the same numbers -- the view is a speed structure, never
    a source of truth -- so this absorbs ValueError/TypeError/KeyError from the
    parse and nothing else.
    """
    if df is None or getattr(df, "empty", True):
        return None
    if not {"Date", "Close", "High", "Low"}.issubset(df.columns):
        return None
    try:
        return OhlcvView(symbol, df)
    except (ValueError, TypeError, KeyError) as e:
        logger.warning("DeterministicScorer: no OHLCV view for %s (%s); the "
                       "per-slice indicator path will be used.", symbol, e)
        return None


def fetch_statements(providers, symbol: str, as_of: Optional[datetime],
                     lookback_periods: int = 6) -> Dict[str, Any]:
    """Annual income/balance/cashflow statements, latest-first, point-in-time.

    Passes the as_of down so the provider's filing-date (fillingDate) pre-pass
    drops statements not yet FILED at that date (no lookahead). Live (as_of
    None) skips the pre-pass and uses the latest available. Returns
    {'balance': [...], 'income': [...], 'cashflow': [...]} (provider dict
    format, snake_case fields).
    """
    # replay_now(as_of), not a raw wall clock: ``ref`` becomes the ``end_date`` of
    # three tapped statement requests, and an identity key holding an un-replayed
    # ``datetime.now()`` can never be matched again -- the recorded response would
    # be unreproducible by construction (see ba2_common.core.replay.observe).
    # as_of given => returned unchanged, so the historical path is untouched.
    ref = replay_now(as_of)
    det = providers.fundamentals_details()
    out: Dict[str, Any] = {}
    for key, fn in (("balance", det.get_balance_sheet),
                    ("income", det.get_income_statement),
                    ("cashflow", det.get_cashflow_statement)):
        kwargs = dict(symbol=symbol, frequency="annual", end_date=ref,
                      lookback_periods=lookback_periods, format_type="dict")
        if as_of is not None:
            kwargs["as_of"] = as_of  # activates the filing-date filter
        try:
            stmts = fn(**kwargs)
        except ReplayMiss:
            raise
        except Exception as e:          # noqa: BLE001 - hermetic/defect errors re-raise
            absorb_if_benign(e)
            logger.warning("DeterministicScorer %s fetch failed for %s: %s", key, symbol, e)
            out[key] = []
            continue
        out[key] = stmts.get("statements", []) if isinstance(stmts, dict) else []
    _warn_once_if_no_statements(symbol, out, as_of)
    return out


# One warning per process: a per-symbol-per-bar warning would emit tens of
# thousands of lines in a GA trial and be ignored, which is how a silently dead
# section survives a whole grid.
_NO_STATEMENTS_WARNED = False


def _warn_once_if_no_statements(symbol: str, out: Dict[str, Any],
                                as_of: Optional[datetime]) -> None:
    """Make an empty FUNDAMENTAL section audible.

    With no statements the section scores None and renormalizes away, so
    w_fundamental / fw_* / z_veto / altman_variant / fscore_disqualify /
    scale_accel / fundamentals_max_age_days all become GA genes that cannot move
    anything -- a grid then reports a clean OOS number for a search space that
    was half dead. The usual cause is a SHALLOW statement cache: the disk cache
    is keyed without depth, so whichever expert warmed it first fixed the depth
    for everyone (FactorRanker asks for 1 period).
    """
    global _NO_STATEMENTS_WARNED
    if _NO_STATEMENTS_WARNED or any(out.get(k) for k in ("income", "balance", "cashflow")):
        return
    _NO_STATEMENTS_WARNED = True
    logger.warning(
        "DeterministicScorer: NO point-in-time statements for %s at as_of=%s -- the "
        "FUNDAMENTAL section will be empty and every fundamental setting is an inert "
        "GA gene. Re-warm the statement caches (they are keyed without depth, so a "
        "1-period warm pins them) before trusting a grid result.",
        symbol, as_of.date() if as_of else "live")


def reset_statement_warning() -> None:
    """Re-arm the once-per-process warning (tests, and long-lived workers)."""
    global _NO_STATEMENTS_WARNED
    _NO_STATEMENTS_WARNED = False


def fetch_past_earnings(providers, symbol: str, as_of: Optional[datetime],
                        lookback_periods: int = 16) -> list:
    """Reported-vs-estimated quarterly earnings rows, point-in-time.

    An earnings ANNOUNCEMENT is public on its report date, so `end_date=as_of`
    is the correct no-lookahead window here -- unlike a financial statement,
    which additionally needs its FILING date checked. 16 quarters gives the SUE
    standardization (4 years) enough dispersion history.
    """
    # replay_now(as_of): ``ref`` is the tapped request's ``end_date`` (see
    # fetch_statements above).
    ref = replay_now(as_of)
    det = providers.fundamentals_details()
    try:
        out = det.get_past_earnings(symbol=symbol, frequency="quarterly", end_date=ref,
                                    lookback_periods=lookback_periods, format_type="dict")
    except ReplayMiss:
        raise
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer past-earnings fetch failed for %s: %s", symbol, e)
        return []
    return out.get("earnings", []) if isinstance(out, dict) else []


def fetch_grades_history(api_key: str, symbol: str) -> list:
    """Dated FMP analyst-grade history, re-using FMPRating's cached fetcher
    (TTLCache + backtest disk cache; no-lookahead filtering is done by the
    analyst calculator at as_of)."""
    from ba2_experts.FMPRating import fetch_grades_historical_cached
    try:
        return fetch_grades_historical_cached(api_key, symbol) or []
    except ReplayMiss:
        raise
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer grades fetch failed for %s: %s", symbol, e)
        return []


def fetch_price_targets(api_key: str, symbol: str) -> list:
    """Dated individual analyst price targets, re-using FMPRating's cached
    fetcher (TTLCache + backtest disk cache). Each row carries publishedDate, so
    the no-lookahead filtering happens in the pure calculator at as_of."""
    from ba2_experts.FMPRating import fetch_price_target_history_cached
    try:
        return fetch_price_target_history_cached(api_key, symbol) or []
    except ReplayMiss:
        raise
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer price-target fetch failed for %s: %s", symbol, e)
        return []


def fetch_index_closes(providers, as_of: Optional[datetime],
                       index_symbol: str = INDEX_SYMBOL) -> Optional[pd.Series]:
    """Index (SPY) close series for the macro trend input.

    Memoised on (index_symbol, as_of): the answer does not depend on the symbol
    being scored, yet every symbol in a batch asks for it on the same bar -- the
    same SPY slice was rebuilt ~97x per date. The frame's fingerprint is part of
    the stored value (not the key, which would need the fetch to compute), so a
    re-fetched or reset frame misses instead of serving a stale index.
    """
    key = (index_symbol, str(as_of) if as_of is not None else "live", OHLCV_LOOKBACK_DAYS)
    view_key = _ohlcv_key(index_symbol, OHLCV_LOOKBACK_DAYS)
    hit = _INDEX_CLOSES_MEMO.get(key)
    if hit is not None:
        fingerprint, closes = hit
        current = _OHLCV_VIEWS.get(view_key)
        if current is not None and current.fingerprint == fingerprint:
            _INDEX_CLOSES_MEMO.move_to_end(key)
            return closes
        del _INDEX_CLOSES_MEMO[key]
    df = fetch_ohlcv(providers, index_symbol, as_of)
    if df is None or "Close" not in df.columns:
        return None
    closes = df["Close"]
    view = _OHLCV_VIEWS.get(view_key)
    if view is not None:
        _INDEX_CLOSES_MEMO[key] = (view.fingerprint, closes)
        _INDEX_CLOSES_MEMO.move_to_end(key)
        while len(_INDEX_CLOSES_MEMO) > _INDEX_CLOSES_MAX:
            _INDEX_CLOSES_MEMO.popitem(last=False)
    return closes


def _observation_series(rows: Any) -> Optional[pd.Series]:
    """FRED-style [{date, value}, ...] -> ascending float Series.

    The regime calculators need HISTORY (z-scores, trailing averages, Sahm's
    12-month minimum), not a single latest reading: handing them a scalar -- or
    a 1-element Series -- makes every one of them return None, which is how
    three of the seven macro inputs came to be permanently dead.
    """
    if rows is None:
        return None
    if isinstance(rows, dict):
        rows = rows.get("observations", rows.get("values", []))
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    pairs: List[tuple] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        val = r.get("value", r.get("v"))
        if val in (None, ".", ""):
            continue
        try:
            pairs.append((str(r.get("date") or r.get("d") or ""), float(val)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])      # ascending by observation date
    return pd.Series([v for _, v in pairs])


def fetch_macro_series(providers, as_of: Optional[datetime]) -> Dict[str, Any]:
    """Point-in-time macro inputs, read from the FRED disk cache.

    Deliberately does NOT go through the ProviderBundle. This mirrors the analyst path
    above (``fetch_grades_history`` calls a module-level cached fetcher directly): the
    macro store is a flat per-series file, not a provider, and routing it through the
    bundle would mean inventing a bundle method whose only implementation reads those
    same files.

    HISTORY. Until 2026-08-11 this read ``providers.macro()`` behind a
    ``hasattr`` guard. No bundle has ever defined ``macro()``, so the guard was always
    False and every input here returned None -- in live AND backtest. The regime
    composite renormalized onto its one surviving input and "macro" silently became
    ``+1 if SPY > SMA200 else -1``. The guard is gone: a missing series now raises out
    of ``get_series_as_of`` rather than degrading to a plausible-looking zero.

    NO LOOKAHEAD. ``get_series_as_of`` cuts revised monthly series (UNRATE) on their
    true first-publication date, so a bar on 2024-01-31 cannot see January's
    unemployment rate -- it was not published until 2024-02-02.

    There is no PMI input: ISM's NAPM no longer exists on FRED and every free
    stand-in is scaled differently from the 50-boundary ``pmi_score`` expects. See
    ``ba2_providers.macro.fred_series.SERIES_SPEC``.
    """
    from ba2_providers.macro import fred_series

    out: Dict[str, Any] = {"vix": None, "unrate_series": None,
                           "spread_10y3m_series": None, "oas_series": None}

    # str(): as_of may be a datetime (engine) or an ISO string (tools/tests), and
    # get_series_as_of accepts both -- the memo key must not care which.
    #
    # ONE KEY FOR THE WHOLE PROCESS on the live path. These four series are
    # economy-wide: identical for every symbol in a batch and for every analysis in
    # a tick, which is the entire reason the memo exists (~95ms of series rebuilding
    # per analysis without it). CAPTURE MUST NOT CHANGE THAT. A key of
    # "analysis:<id>" turned the memo off for exactly the runs it was measuring --
    # an instrument that changes what it measures is not an instrument -- and the
    # thing it was reaching for (every analysis's bundle carrying its macro reads)
    # is what ``_series`` records below, without touching the cache.
    _key_suffix = str(as_of) if as_of is not None else "live"

    def _series(series_id: str):
        """The series, and the observation that says this analysis read it.

        The tap on ``get_series_as_of`` records the analysis that actually LOADED;
        an analysis served out of the memo never enters that function, so its
        bundle would hold macro values with no observation behind them --
        unreplayable, and silently so. The memo hit is therefore recorded here,
        through the same identity the tap writes (``series_identity``, imported
        rather than restated) and the same provenance: this store never reaches the
        network, so every value here came off disk whichever path served it.
        """
        loaded = []

        def _load():
            loaded.append(series_id)
            return fred_series.get_series_as_of(series_id, as_of)

        series = _MACRO_CACHE.get_or_call(f"{series_id}__{_key_suffix}", _load)
        if not loaded:
            record_observation(
                provider="macro", method="get_series_as_of",
                identity=fred_series.series_identity(
                    {"series_id": series_id, "as_of": as_of}),
                payload=series, provenance=ReplayStatus.PROVENANCE_DISK_CACHE)
        return series

    # Keyed by MACRO_SERIES_IDS so the tuple the dependency adapter declares is the
    # tuple this function reads.
    vix_id, unrate_id, oas_id, spread_id = MACRO_SERIES_IDS
    try:
        vix = _series(vix_id)
        out["vix"] = float(vix.iloc[-1]) if len(vix) else None
        out["unrate_series"] = _series(unrate_id)
        # Credit: Moody's Baa less 10y, NOT the ICE HY OAS the key name still reflects
        # -- FRED serves ICE indices on a rolling ~3y licence. credit_score z-scores
        # its input, so the substitution is unit-safe.
        out["oas_series"] = _series(oas_id)
        out["spread_10y3m_series"] = _series(spread_id)
    except ReplayMiss:
        raise
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer macro series failed: %s", e)
    return out
