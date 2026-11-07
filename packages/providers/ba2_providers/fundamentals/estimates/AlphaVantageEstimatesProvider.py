"""Alpha Vantage point-in-time EPS consensus (``EARNINGS_ESTIMATES`` + ``EARNINGS``).

The estimate-method half of ``fundamentals_details()`` when the estimates source is ``alphavantage``
(``ba2_common.core.estimates_router``). The point-in-time and split-basis rules are pure and live, with the
measured Alpha Vantage behaviour, in ``ba2_common.core.estimates_pit``.

Caching: ONE payload per symbol -- ``{"fetched_on", "estimates", "earnings"}`` (the two raw AV responses,
API key never stored) -- under the ``av_eps_consensus`` namespace of ``fmp_history_disk_cached``. So a
backtest (frozen + hermetic) reads only that file, a miss is recorded by the same hermetic-miss registry
(one symbol disabled, many abort the run), and prewarm fills it. Live is a passthrough with a 6 h in-memory
TTL (the free tier is 25 calls/day, 2 per symbol).

Split basis: the FMP split calendar, namespace ``mc_stock_split`` (the one the market-condition warmup
already fills), is read through the same hermetic cache. A hermetic MISS of the calendar raises instead of
reading as "never split": that would silently return unadjusted values.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.config import get_app_setting
from ba2_common.core.estimates_pit import quarterly_consensus
from ba2_common.logger import logger
from ba2_providers.alpha_vantage_common import make_api_request
from ba2_providers.fmp_common import (
    fmp_history_disk_cached, fmp_live_cached, hermetic_miss_symbols,
)

#: The fmp_history namespace of the combined per-symbol payload.
AV_CONSENSUS_NAMESPACE = "av_eps_consensus"
#: The split calendar namespace (shared with ``market_conditions.fmp_source``).
SPLIT_CALENDAR_NAMESPACE = "mc_stock_split"
SPLIT_CALENDAR_MAX_AGE_DAYS = 1.0
_LIVE_TTL_S = 6 * 3600.0
#: Alpha Vantage allows one request per second on the free tier.
_MIN_CALL_GAP_S = 1.1


class AlphaVantageEstimatesError(RuntimeError):
    """The AV response was not the expected payload (an error body, a throttle notice, no data)."""


class SplitCalendarMissing(RuntimeError):
    """The split calendar of a symbol was not pre-warmed in a hermetic backtest: the estimate basis is
    unknown, so no value is returned (an empty calendar would read as 'never split')."""


def _expect(payload: Any, key: str, function: str, symbol: str) -> Dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        # An AV error / throttle body ("Information", "Error Message", "Note") carries no data list.
        head = {k: (str(v)[:120]) for k, v in payload.items()} if isinstance(payload, dict) else type(payload).__name__
        raise AlphaVantageEstimatesError(f"Alpha Vantage {function} for {symbol} returned no {key!r}: {head}")
    return payload


def fetch_av_consensus_payload(symbol: str) -> Dict[str, Any]:
    """Two AV calls (EARNINGS_ESTIMATES, EARNINGS) for ``symbol`` -> the cacheable combined payload."""
    estimates = _expect(make_api_request("EARNINGS_ESTIMATES", {"symbol": symbol}, source="ba2_estimates"),
                        "estimates", "EARNINGS_ESTIMATES", symbol)
    time.sleep(_MIN_CALL_GAP_S)
    earnings = _expect(make_api_request("EARNINGS", {"symbol": symbol}, source="ba2_estimates"),
                       "quarterlyEarnings", "EARNINGS", symbol)
    return {"fetched_on": date.today().isoformat(), "estimates": estimates, "earnings": earnings}


def load_av_consensus_payload(symbol: str) -> Any:
    """The combined payload through the shared disk-cache/hermetic machinery (``[]`` = a hermetic miss or
    a genuine empty sentinel, exactly as for every ``fmp_history`` namespace)."""
    sym = symbol.upper()
    return fmp_history_disk_cached(
        AV_CONSENSUS_NAMESPACE, sym,
        lambda: fmp_live_cached(f"{AV_CONSENSUS_NAMESPACE}:{sym}", lambda: fetch_av_consensus_payload(sym),
                                _LIVE_TTL_S))


def load_split_calendar(symbol: str) -> List[Tuple[date, float]]:
    """``[(ex_date, ratio)]`` of ``symbol`` (4-for-1 -> 4.0), through the hermetic cache."""
    from ba2_providers import symbol_info
    sym = symbol.upper()
    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        raise ValueError("FMP API key not configured (needed for the split calendar of the estimates basis)")
    payload = fmp_history_disk_cached(
        SPLIT_CALENDAR_NAMESPACE, sym, lambda: symbol_info.fetch_splits(api_key, sym),
        max_age_days=SPLIT_CALENDAR_MAX_AGE_DAYS, retain=False)
    if f"{SPLIT_CALENDAR_NAMESPACE}/{sym}" in hermetic_miss_symbols():
        raise SplitCalendarMissing(
            f"{SPLIT_CALENDAR_NAMESPACE} for {sym} is not pre-warmed; the split basis of its estimates is "
            f"unknown. Run `ba2-test prewarm` with --estimates-source alphavantage.")
    events = symbol_info.parse_splits(payload)
    return [(e.date, e.ratio) for e in events]      # an unusable (None) ratio refuses in split_factor_after


class AlphaVantageEstimatesProvider:
    """``get_quarterly_eps_consensus`` / ``get_earnings_estimates`` from Alpha Vantage, point in time."""

    def __init__(self, source: str = "ba2_estimates"):
        if not get_app_setting("alpha_vantage_api_key"):
            raise ValueError("Alpha Vantage API key not configured. Set 'alpha_vantage_api_key' in app settings.")
        self.source = source

    def get_provider_name(self) -> str:
        return "alphavantage"

    def get_quarterly_eps_consensus(self, symbol: str, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        sym = symbol.upper()
        payload = load_av_consensus_payload(sym)
        result = {"symbol": sym, "as_of": None if as_of is None else as_of.isoformat(),
                  "source": "alphavantage", "quarters": []}
        if not payload:
            logger.warning(f"AV EPS consensus for {sym}: no payload (not warmed / no data)")
            return result
        fetched_on = date.fromisoformat(payload["fetched_on"])
        splits = load_split_calendar(sym)
        result["quarters"] = quarterly_consensus(payload["estimates"], payload["earnings"], splits, as_of,
                                                 fetched_on)
        return result

    def get_earnings_estimates(self, symbol: str, frequency: str, as_of_date: datetime,
                               lookback_periods: int = 4, format_type: str = "markdown"):
        """The ``FMPCompanyDetailsProvider.get_earnings_estimates`` shape, from the point-in-time quarterly
        consensus. ``frequency`` is accepted for signature compatibility: the rows are always the next
        unreported QUARTERS (FMP's are fiscal years), nearest first, ``lookback_periods`` of them."""
        consensus = self.get_quarterly_eps_consensus(symbol, as_of_date)
        rows = [{
            "fiscal_date_ending": q["fiscal_period_end"],
            "estimated_eps_avg": q["eps_avg"],
            "estimated_eps_high": q["eps_high"],
            "estimated_eps_low": q["eps_low"],
            "number_of_analysts": q["analyst_count"],
            "snapshot_date": q["snapshot_date"],
            "report_date": q["report_date"],
        } for q in consensus["quarters"]][:lookback_periods]
        result = {"symbol": consensus["symbol"], "frequency": "quarterly",
                  "as_of_date": as_of_date.isoformat(), "estimates": rows, "source": "alphavantage"}
        if format_type == "dict":
            return result
        lines = [f"# Earnings Estimates for {consensus['symbol']} (Alpha Vantage, quarterly)", ""]
        lines += [f"- {r['fiscal_date_ending']}: avg {r['estimated_eps_avg']:.4f} (snapshot {r['snapshot_date']})"
                  for r in rows] or ["No estimates available."]
        return "\n".join(lines) + "\n"
