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
mode the hermetic guard exists to make loud.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from ba2_common.core.failure_modes import absorb_if_benign
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
_STATEMENTS_CACHE = TTLCache(_TTL_SECONDS)
_GRADES_CACHE = TTLCache(_TTL_SECONDS)
_MACRO_CACHE = TTLCache(3600)

# History needed for a 252d momentum + 200d SMA + indicator buffers.
OHLCV_LOOKBACK_DAYS = 600

INDEX_SYMBOL = "SPY"


def reset_caches() -> None:
    """Drop every process-wide cache (tests, and the live /api/reload path).

    TTLCache exposes get_or_call/invalidate, not a bulk clear, so the store is
    reset under its own lock.
    """
    _OHLCV_COVERAGE.clear()
    for cache in (_OHLCV_CACHE, _STATEMENTS_CACHE, _GRADES_CACHE, _MACRO_CACHE):
        with cache._lock:               # type: ignore[attr-defined]
            cache._store.clear()        # type: ignore[attr-defined]


def _utcnow() -> datetime:
    """tz-aware now (utcnow() is deprecated AND naive, which poisons date math)."""
    return datetime.now(timezone.utc)


def _slice_to_as_of(df: pd.DataFrame, as_of: Optional[datetime]) -> Optional[pd.DataFrame]:
    """Causal slice: keep rows dated <= as_of, matching the frame's tz-awareness.

    Both operands are coerced to the SAME awareness before comparing; mixing them
    raises TypeError, and a bare try/except around it would silently return the
    unsliced frame -- i.e. lookahead.
    """
    if df is None or df.empty or "Date" not in df.columns:
        return df
    if as_of is None:
        return df
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
    need_from = ((as_of or _utcnow()) - timedelta(days=lookback_days)).replace(tzinfo=None)
    key = f"ohlcv|{symbol}|{lookback_days}"
    covered_from, df = _OHLCV_COVERAGE.get(key, (None, None))
    if df is None or (covered_from is not None and need_from < covered_from):
        try:
            df = providers.ohlcv().get_ohlcv_data(
                symbol=symbol, start_date=need_from, end_date=_utcnow(), interval="1d")
        except Exception as e:          # noqa: BLE001 - hermetic/defect errors re-raise
            absorb_if_benign(e)
            logger.warning("DeterministicScorer OHLCV fetch failed for %s: %s", symbol, e)
            return None
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            return None
        _OHLCV_COVERAGE[key] = (need_from, df)
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    out = _slice_to_as_of(df, as_of)
    if out is None or out.empty:
        return None
    return out.reset_index(drop=True)


def fetch_statements(providers, symbol: str, as_of: Optional[datetime],
                     lookback_periods: int = 6) -> Dict[str, Any]:
    """Annual income/balance/cashflow statements, latest-first, point-in-time.

    Passes the as_of down so the provider's filing-date (fillingDate) pre-pass
    drops statements not yet FILED at that date (no lookahead). Live (as_of
    None) skips the pre-pass and uses the latest available. Returns
    {'balance': [...], 'income': [...], 'cashflow': [...]} (provider dict
    format, snake_case fields).
    """
    ref = as_of if as_of is not None else _utcnow()
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
    ref = as_of if as_of is not None else _utcnow()
    det = providers.fundamentals_details()
    try:
        out = det.get_past_earnings(symbol=symbol, frequency="quarterly", end_date=ref,
                                    lookback_periods=lookback_periods, format_type="dict")
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
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer price-target fetch failed for %s: %s", symbol, e)
        return []


def fetch_index_closes(providers, as_of: Optional[datetime],
                       index_symbol: str = INDEX_SYMBOL) -> Optional[pd.Series]:
    """Index (SPY) close series for the macro trend input."""
    df = fetch_ohlcv(providers, index_symbol, as_of)
    if df is None or "Close" not in df.columns:
        return None
    return df["Close"]


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
    _key_suffix = str(as_of) if as_of is not None else "live"

    def _series(series_id: str):
        return _MACRO_CACHE.get_or_call(
            f"{series_id}__{_key_suffix}",
            lambda: fred_series.get_series_as_of(series_id, as_of))

    try:
        vix = _series("VIXCLS")
        out["vix"] = float(vix.iloc[-1]) if len(vix) else None
        out["unrate_series"] = _series("UNRATE")
        # Credit: Moody's Baa less 10y, NOT the ICE HY OAS the key name still reflects
        # -- FRED serves ICE indices on a rolling ~3y licence. credit_score z-scores
        # its input, so the substitution is unit-safe.
        out["oas_series"] = _series("BAA10Y")
        out["spread_10y3m_series"] = _series("T10Y3M")
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer macro series failed: %s", e)
    return out
