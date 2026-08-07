"""DeterministicScorer - data fetchers.

All fetches go through the ProviderBundle (backtest: parquet/SQLite as_of
cache, hermetic; live: provider TTL caches). Point-in-time is enforced by the
platform providers themselves (fundamentals interfaces take as_of_date /
end_date semantics), plus explicit OHLCV slicing to <= as_of here.

Fetcher layout follows the FactorRanker pattern: module-level functions, no
expert instance required, so the testplatform --prewarm hook can call them and
GA workers share one fetch per symbol.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from ba2_common.logger import logger
from ba2_providers.fmp_common import TTLCache

# Process-wide caches keyed by symbol (NOT by as_of): the fetched payloads are
# time-invariant within the TTL window; as_of slicing happens in the pure
# calculators. Shared across instances + GA workers (FMPRating pattern).
_TTL_SECONDS = 900
_OHLCV_CACHE = TTLCache(_TTL_SECONDS)
_OVERVIEW_CACHE = TTLCache(_TTL_SECONDS)
_STATEMENTS_CACHE = TTLCache(_TTL_SECONDS)
_GRADES_CACHE = TTLCache(_TTL_SECONDS)
_MACRO_CACHE = TTLCache(3600)

# History needed for a 252d momentum + 200d SMA + indicator buffers.
OHLCV_LOOKBACK_DAYS = 600

INDEX_SYMBOL = "SPY"


def _cache_key(symbol: str, as_of: Optional[datetime], extra: str = "") -> str:
    """Live (as_of None) and each backtest date get distinct cache slots, but a
    single run re-fetches a symbol only once because the caller passes the same
    as_of for all its bars' gathers... actually bars differ; the OHLCV fetch
    uses a RUN-stable key (symbol only) in backtest because the provider bundle
    already serves cached bars. Live keeps as_of=None."""
    if as_of is None:
        return f"{symbol}{extra}"
    return f"{symbol}|{as_of.date().isoformat()}{extra}"


def fetch_ohlcv(providers, symbol: str, as_of: Optional[datetime],
                lookback_days: int = OHLCV_LOOKBACK_DAYS) -> Optional[pd.DataFrame]:
    """Daily OHLCV ascending by date, sliced to <= as_of (causal)."""
    try:
        end = as_of if as_of is not None else datetime.utcnow()
        key = _cache_key(symbol, None)  # payload is the full-range fetch; sliced per as_of below
        bundle_cached = _OHLCV_CACHE.get(key) if as_of is None else None
        if bundle_cached is not None:
            df = bundle_cached
        else:
            start = end - timedelta(days=lookback_days)
            df = providers.ohlcv().get_ohlcv_data(
                symbol=symbol, start_date=start, end_date=end, interval="1d")
            if as_of is None:
                _OHLCV_CACHE.set(key, df)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        if as_of is not None and "Date" in df.columns:
            cutoff = pd.Timestamp(as_of).tz_localize(None) if getattr(
                df["Date"].dtype, "tz", None) is None else pd.Timestamp(as_of)
            df = df[df["Date"] <= cutoff]
        return df.reset_index(drop=True) if not df.empty else None
    except Exception as e:
        logger.debug(f"DeterministicScorer OHLCV fetch failed for {symbol}: {e}")
        return None


def fetch_fundamentals_overview(providers, symbol: str,
                                as_of: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """Point-in-time fundamentals overview (market cap, ratios, shares...)."""
    try:
        ref = as_of if as_of is not None else datetime.utcnow()
        out = providers.fundamentals_overview().get_fundamentals_overview(
            symbol=symbol, as_of_date=ref, format_type="dict")
        return out if isinstance(out, dict) else None
    except Exception as e:
        logger.debug(f"DeterministicScorer overview fetch failed for {symbol}: {e}")
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
    try:
        ref = as_of if as_of is not None else datetime.utcnow()
        det = providers.fundamentals_details()
        out: Dict[str, Any] = {}
        for key, fn in (("balance", det.get_balance_sheet),
                        ("income", det.get_income_statement),
                        ("cashflow", det.get_cashflow_statement)):
            try:
                kwargs = dict(symbol=symbol, frequency="annual", end_date=ref,
                              lookback_periods=lookback_periods, format_type="dict")
                if as_of is not None:
                    kwargs["as_of"] = as_of  # activates the filing-date filter
                stmts = fn(**kwargs)
                rows = stmts.get("statements", []) if isinstance(stmts, dict) else []
                out[key] = rows
            except Exception as e:
                logger.debug(f"DeterministicScorer {key} fetch failed for {symbol}: {e}")
                out[key] = []
        return out
    except Exception as e:
        logger.debug(f"DeterministicScorer statements fetch failed for {symbol}: {e}")
        return {"balance": [], "income": [], "cashflow": []}


def fetch_grades_history(api_key: str, symbol: str) -> list:
    """Dated FMP analyst-grade history, re-using FMPRating's cached fetcher
    (TTLCache + backtest disk cache; no-lookahead filtering is done by the
    analyst calculator at as_of)."""
    try:
        from ba2_experts.FMPRating import fetch_grades_historical_cached
        return fetch_grades_historical_cached(api_key, symbol) or []
    except Exception as e:
        logger.debug(f"DeterministicScorer grades fetch failed for {symbol}: {e}")
        return []


def fetch_index_closes(providers, as_of: Optional[datetime],
                       index_symbol: str = INDEX_SYMBOL) -> Optional[pd.Series]:
    """Index (SPY) close series for the macro trend input."""
    df = fetch_ohlcv(providers, index_symbol, as_of)
    if df is None or "Close" not in df.columns:
        return None
    return df["Close"]


def fetch_macro_series(providers, as_of: Optional[datetime]) -> Dict[str, Any]:
    """Best-effort macro inputs from the macro provider (FRED). Any failure
    degrades to None per input; the regime composite renormalizes around the
    inputs that ARE available. Live-only for v1 unless the provider is wired
    into the backtest bundle (hermetic builds skip gracefully)."""
    out: Dict[str, Any] = {"vix": None, "pmi": None, "unrate": None,
                           "spread_10y3m": None, "oas": None}
    try:
        macro = providers.macro() if hasattr(providers, "macro") else None
        if macro is None:
            return out
        ref = as_of if as_of is not None else datetime.utcnow()
        start = (ref - timedelta(days=400)).date().isoformat()
        end = ref.date().isoformat()
        try:
            ind = macro.get_economic_indicators(start_date=start, end_date=end)
            series = ind.get("series", ind) if isinstance(ind, dict) else {}
            def last_val(name):
                rows = series.get(name) or series.get(name.lower()) or []
                if isinstance(rows, dict):
                    rows = rows.get("observations", [])
                vals = [float(r.get("value")) for r in rows
                        if isinstance(r, dict) and r.get("value") not in (None, ".", "")]
                return vals[-1] if vals else None
            out["vix"] = last_val("VIXCLS") or last_val("vix")
            out["pmi"] = last_val("NAPM") or last_val("pmi")
            out["unrate"] = last_val("UNRATE") or last_val("unemployment")
        except Exception as e:
            logger.debug(f"DeterministicScorer macro indicators failed: {e}")
        try:
            yc = macro.get_yield_curve(start_date=start, end_date=end)
            if isinstance(yc, dict):
                ten = yc.get("10y") or yc.get("10Y") or []
                three = yc.get("3m") or yc.get("3M") or []
                def last(rows):
                    vals = [float(r.get("value")) for r in rows
                            if isinstance(r, dict) and r.get("value") not in (None, ".", "")]
                    return vals[-1] if vals else None
                t, m = last(ten), last(three)
                if t is not None and m is not None:
                    out["spread_10y3m"] = t - m
        except Exception as e:
            logger.debug(f"DeterministicScorer yield curve failed: {e}")
    except Exception as e:
        logger.debug(f"DeterministicScorer macro provider unavailable: {e}")
    return out
