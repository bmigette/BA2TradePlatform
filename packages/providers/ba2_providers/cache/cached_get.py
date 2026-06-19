"""Uniform ``get(symbol, as_of=None, lookback=...)`` alias layer across provider
categories.

This is an ALIAS, not a rewrite: each function normalizes the uniform ``as_of`` /
``lookback`` contract to the category's existing native parameters, so the existing
~80%-correct provider signatures are reused unchanged and the ``as_of=None`` path stays
byte-identical to today's live fetch.

Category mapping (per SHARED CONTRACT ``provider_asof.uniform_contract``):
  OHLCV / News / Insider:   as_of -> end_date,    lookback -> lookback_days
  Fundamentals statements:  as_of -> end_date,    lookback -> lookback_periods

``as_of=None`` => latest (live, UNCHANGED): ``end_date`` defaults to "now" exactly as
the live callers did, and the no-lookahead ``as_of`` filter inside the corrected
providers is a no-op (insider/statements both gate their effective-date filter behind
``as_of is not None``). Screener is EXCLUDED (no temporal param; live-only, see its
module docstring).
"""
from datetime import datetime, timezone
from typing import Any, Optional


def ohlcv_get(provider, symbol, as_of=None, lookback=400, interval="1d", format_type="dict"):
    """OHLCV time-series up to ``as_of`` (close). ``as_of=None`` => now (live)."""
    end = as_of or datetime.now(timezone.utc)
    return provider.get_ohlcv_data(symbol, end_date=end, lookback_days=lookback, interval=interval)


def insider_get(provider, symbol, as_of=None, lookback=30, format_type="dict"):
    """Insider transactions. ``as_of`` is threaded so the corrected provider enforces
    the no-lookahead filingDate anchor when set; with ``as_of=None`` the live
    transactionDate-range behaviour is byte-identical."""
    end = as_of or datetime.now(timezone.utc)
    return provider.get_insider_transactions(symbol, end_date=end, lookback_days=lookback,
                                             as_of=as_of, format_type=format_type)


def statement_get(provider, symbol, statement, as_of=None, frequency="annual",
                  lookback_periods=1, format_type="dict"):
    """Financial statement (``balance_sheet`` | ``income_statement`` |
    ``cashflow_statement``). ``as_of`` is threaded so the corrected provider enforces
    the no-lookahead fillingDate/acceptedDate anchor when set."""
    end = as_of or datetime.now(timezone.utc)
    fn = getattr(provider, f"get_{statement}")
    return fn(symbol, frequency, end, lookback_periods=lookback_periods,
              as_of=as_of, format_type=format_type)


def past_earnings_get(provider, symbol, as_of=None, frequency="quarterly",
                      lookback_periods=1, format_type="dict"):
    """Historical earnings up to ``as_of``. The provider's existing report-date
    (``date`` <= ``end_date``) filter is already point-in-time-safe, so ``as_of`` maps
    to ``end_date`` only — the provider takes no ``as_of`` param."""
    end = as_of or datetime.now(timezone.utc)
    return provider.get_past_earnings(symbol, frequency=frequency, end_date=end,
                                      lookback_periods=lookback_periods, format_type=format_type)
