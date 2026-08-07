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
    """Best-effort macro inputs from the macro provider (FRED).

    Returns SERIES (``*_series``) for every history-based input plus the scalar
    latest reading where a level is what the calculator wants (VIX, PMI). Any
    failure degrades that one input to None and the regime composite
    renormalizes around what IS available.
    """
    out: Dict[str, Any] = {"vix": None, "pmi": None,
                           "unrate_series": None, "spread_10y3m_series": None,
                           "oas_series": None}
    macro = providers.macro() if hasattr(providers, "macro") else None
    if macro is None:
        return out
    ref = as_of if as_of is not None else _utcnow()
    # 3y+ of history: the credit z-score wants ~756 obs, Sahm wants 15 months.
    start = (ref - timedelta(days=1200)).date().isoformat()
    end = ref.date().isoformat()

    try:
        ind = macro.get_economic_indicators(start_date=start, end_date=end)
        series = ind.get("series", ind) if isinstance(ind, dict) else {}

        def rows_for(*names):
            for n in names:
                rows = series.get(n) or series.get(n.lower())
                if rows:
                    return rows
            return None

        vix = _observation_series(rows_for("VIXCLS", "vix"))
        pmi = _observation_series(rows_for("NAPM", "pmi", "ISM"))
        out["vix"] = float(vix.iloc[-1]) if vix is not None and len(vix) else None
        out["pmi"] = float(pmi.iloc[-1]) if pmi is not None and len(pmi) else None
        out["unrate_series"] = _observation_series(rows_for("UNRATE", "unemployment"))
        out["oas_series"] = _observation_series(
            rows_for("BAMLH0A0HYM2", "hy_oas", "oas"))
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer macro indicators failed: %s", e)

    try:
        yc = macro.get_yield_curve(start_date=start, end_date=end)
        if isinstance(yc, dict):
            ten = _observation_series(yc.get("10y") or yc.get("10Y"))
            three = _observation_series(yc.get("3m") or yc.get("3M"))
            if ten is not None and three is not None:
                n = min(len(ten), len(three))
                if n:
                    out["spread_10y3m_series"] = (
                        ten.iloc[-n:].reset_index(drop=True)
                        - three.iloc[-n:].reset_index(drop=True))
            elif isinstance(yc.get("spread_10y3m"), (list, tuple)):
                out["spread_10y3m_series"] = _observation_series(yc["spread_10y3m"])
    except Exception as e:              # noqa: BLE001 - hermetic/defect errors re-raise
        absorb_if_benign(e)
        logger.warning("DeterministicScorer yield curve failed: %s", e)
    return out
