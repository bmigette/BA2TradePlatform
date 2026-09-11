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
from typing import Any, Optional

from ba2_common.core.replay.clock import replay_now
from ba2_common.core.replay.observe import observe_provider

# Replay capture (spec step 2): the uniform alias layer is where the insider and
# past-earnings inputs actually enter an expert, so it is where they are recorded
# -- at the RETURN, cache hits included, with no extra request. ``as_of`` is part
# of the identity because it selects the point-in-time window; the api key is not
# passed here at all, and the tap drops credential-shaped keys regardless.
#
# ``replay_now(as_of)`` -- not ``as_of or datetime.now()`` -- for the ``end_date``
# each alias derives: that value is passed DOWN to the provider method, whose own
# tap puts it in ITS request identity, and an identity key holding an un-replayed
# wall clock can never be matched again (see ba2_common.core.replay.observe). With
# ``as_of`` given it is returned unchanged, so the point-in-time path is untouched;
# with no capture context it is the same wall-clock read as before.


def ohlcv_get(provider, symbol, as_of=None, lookback=400, interval="1d", format_type="dict"):
    """OHLCV time-series up to ``as_of`` (close). ``as_of=None`` => now (live)."""
    end = replay_now(as_of)
    return provider.get_ohlcv_data(symbol, end_date=end, lookback_days=lookback, interval=interval)


def insider_get_identity(args):
    """What makes an ``insider_get`` response what it is.

    Named (not an inline lambda) because the offline replay tape has to build the
    SAME identity to look a recorded response up by it -- two copies of this dict
    would drift and turn a real match into a silent miss.
    """
    return {
        "provider": type(args["provider"]).__name__,
        "symbol": args["symbol"],
        "as_of": args["as_of"],
        "lookback": args["lookback"],
        "format_type": args["format_type"],
    }


@observe_provider("provider_cache", "insider_get", identity=insider_get_identity)
def insider_get(provider, symbol, as_of=None, lookback=30, format_type="dict"):
    """Insider transactions. ``as_of`` is threaded so the corrected provider enforces
    the no-lookahead filingDate anchor when set; with ``as_of=None`` the live
    transactionDate-range behaviour is byte-identical."""
    end = replay_now(as_of)
    return provider.get_insider_transactions(symbol, end_date=end, lookback_days=lookback,
                                             as_of=as_of, format_type=format_type)


def statement_get(provider, symbol, statement, as_of=None, frequency="annual",
                  lookback_periods=1, format_type="dict"):
    """Financial statement (``balance_sheet`` | ``income_statement`` |
    ``cashflow_statement``). ``as_of`` is threaded so the corrected provider enforces
    the no-lookahead fillingDate/acceptedDate anchor when set."""
    end = replay_now(as_of)
    fn = getattr(provider, f"get_{statement}")
    return fn(symbol, frequency, end, lookback_periods=lookback_periods,
              as_of=as_of, format_type=format_type)


def past_earnings_get_identity(args):
    """What makes a ``past_earnings_get`` response what it is (see above)."""
    return {
        "provider": type(args["provider"]).__name__,
        "symbol": args["symbol"],
        "as_of": args["as_of"],
        "frequency": args["frequency"],
        "lookback_periods": args["lookback_periods"],
        "format_type": args["format_type"],
    }


@observe_provider("provider_cache", "past_earnings_get", identity=past_earnings_get_identity)
def past_earnings_get(provider, symbol, as_of=None, frequency="quarterly",
                      lookback_periods=1, format_type="dict"):
    """Historical earnings up to ``as_of``. The provider's existing report-date
    (``date`` <= ``end_date``) filter is already point-in-time-safe, so ``as_of`` maps
    to ``end_date`` only — the provider takes no ``as_of`` param."""
    end = replay_now(as_of)
    return provider.get_past_earnings(symbol, frequency=frequency, end_date=end,
                                      lookback_periods=lookback_periods, format_type=format_type)
