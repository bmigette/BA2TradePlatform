"""Bulk data-fetch adapters for FactorRanker.

Two layers:

* **Pure transforms** (``enterprise_value``, ``return_on_equity``,
  ``accruals_ratio``, ``days_since``, ``estimate_std_from_range``,
  ``build_value_inputs``, ``build_quality_inputs``) — derive the exact dict shapes
  the factor calculators in ``factors.py`` consume. These are unit tested.
* **Thin fetchers** (``fetch_close_prices``, ``fetch_value_inputs``,
  ``fetch_quality_inputs``, ``fetch_pead_inputs``) — loop the universe over the
  existing FMP providers, applying the pure transforms. Network I/O; not unit
  tested (the expert mocks these). Per-symbol failures are logged and skipped so
  one bad symbol never kills the batch.

v1 uses the most recent *annual* statements for value/quality (simpler and robust);
momentum uses ~400 calendar days of daily closes.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from ....logger import logger


# --------------------------------------------------------------------------- #
# Pure transforms (unit tested)
# --------------------------------------------------------------------------- #

def enterprise_value(market_cap: Optional[float], total_debt: Optional[float],
                     cash: Optional[float]) -> float:
    """EV = market cap + total debt - cash. Missing components count as 0."""
    return (market_cap or 0.0) + (total_debt or 0.0) - (cash or 0.0)


def return_on_equity(net_income: Optional[float], equity: Optional[float]) -> Optional[float]:
    """ROE = net income / shareholder equity, or None if not computable."""
    if net_income is None or not equity:
        return None
    return net_income / equity


def accruals_ratio(net_income: Optional[float], operating_cash_flow: Optional[float],
                   total_assets: Optional[float]) -> Optional[float]:
    """Sloan accruals proxy = (net income - operating cash flow) / total assets.

    High accruals (earnings not backed by cash) signal lower quality. None when
    any input is missing or total assets is zero.
    """
    if net_income is None or operating_cash_flow is None or not total_assets:
        return None
    return (net_income - operating_cash_flow) / total_assets


def days_since(report_date: Optional[datetime], as_of: Optional[datetime] = None) -> Optional[int]:
    """Whole days between an earnings report date and ``as_of`` (default: now)."""
    if report_date is None:
        return None
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    return (as_of - report_date).days


def estimate_std_from_range(high: Optional[float], low: Optional[float]) -> Optional[float]:
    """Approximate cross-analyst EPS dispersion as range/4 (None if no usable range)."""
    if high is None or low is None:
        return None
    spread = high - low
    if spread <= 0:
        return None
    return spread / 4.0


def build_value_inputs(eps_ttm: Optional[float], price: Optional[float], fcf_ttm: Optional[float],
                       market_cap: Optional[float], total_debt: Optional[float],
                       cash: Optional[float]) -> Dict[str, Optional[float]]:
    """Assemble the dict consumed by ``factors.value_score``."""
    return {
        "eps_ttm": eps_ttm,
        "price": price,
        "fcf_ttm": fcf_ttm,
        "enterprise_value": enterprise_value(market_cap, total_debt, cash),
    }


def build_quality_inputs(net_income: Optional[float], equity: Optional[float],
                         gross_profit: Optional[float], total_assets: Optional[float],
                         operating_cash_flow: Optional[float]) -> Dict[str, Optional[float]]:
    """Assemble the dict consumed by ``factors.quality_score``."""
    return {
        "roe": return_on_equity(net_income, equity),
        "gross_profit": gross_profit,
        "total_assets": total_assets,
        "accruals_ratio": accruals_ratio(net_income, operating_cash_flow, total_assets),
    }


# --------------------------------------------------------------------------- #
# Thin fetchers (network I/O — not unit tested; the expert mocks these)
# --------------------------------------------------------------------------- #

def _first_statement(result: Optional[dict], key: str) -> dict:
    """Return the most recent statement dict from a provider's dict result."""
    if not result:
        return {}
    items = result.get(key) or []
    return items[0] if items else {}


def fetch_close_prices(symbols, lookback_days: int = 400) -> Dict[str, pd.Series]:
    """{symbol: daily Close Series}. Symbols that fail to fetch are omitted."""
    from ....modules.dataproviders.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    provider = FMPOHLCVProvider()
    out: Dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = provider.get_ohlcv_data(sym, lookback_days=lookback_days, interval="1d")
            if df is not None and not df.empty and "Close" in df:
                out[sym] = df["Close"].reset_index(drop=True)
        except Exception as e:
            logger.warning(f"FactorRanker: price fetch failed for {sym}: {e}")
    return out


def fetch_value_inputs(symbols, as_of: Optional[datetime] = None) -> Dict[str, dict]:
    """{symbol: {eps_ttm, price, fcf_ttm, enterprise_value}} from latest annual fundamentals."""
    from ....modules.dataproviders.fundamentals.overview.FMPCompanyOverviewProvider import FMPCompanyOverviewProvider
    from ....modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    overview = FMPCompanyOverviewProvider()
    details = FMPCompanyDetailsProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            ov = overview.get_fundamentals_overview(sym, as_of_date=as_of, format_type="dict")
            metrics = (ov or {}).get("metrics", {})
            income = _first_statement(details.get_income_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            balance = _first_statement(details.get_balance_sheet(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            cashflow = _first_statement(details.get_cashflow_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            total_debt = (balance.get("short_term_debt") or 0.0) + (balance.get("long_term_debt") or 0.0)
            out[sym] = build_value_inputs(
                eps_ttm=income.get("eps"),
                price=metrics.get("price"),
                fcf_ttm=cashflow.get("free_cash_flow"),
                market_cap=metrics.get("market_cap"),
                total_debt=total_debt,
                cash=balance.get("cash_and_cash_equivalents"),
            )
        except Exception as e:
            logger.warning(f"FactorRanker: value fetch failed for {sym}: {e}")
    return out


def fetch_quality_inputs(symbols, as_of: Optional[datetime] = None) -> Dict[str, dict]:
    """{symbol: {roe, gross_profit, total_assets, accruals_ratio}} from latest annual fundamentals."""
    from ....modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    details = FMPCompanyDetailsProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            income = _first_statement(details.get_income_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            balance = _first_statement(details.get_balance_sheet(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            cashflow = _first_statement(details.get_cashflow_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"), "statements")
            out[sym] = build_quality_inputs(
                net_income=income.get("net_income"),
                equity=balance.get("total_shareholder_equity"),
                gross_profit=income.get("gross_profit"),
                total_assets=balance.get("total_assets"),
                operating_cash_flow=cashflow.get("operating_cash_flow"),
            )
        except Exception as e:
            logger.warning(f"FactorRanker: quality fetch failed for {sym}: {e}")
    return out


def fetch_pead_inputs(symbols, as_of: Optional[datetime] = None) -> Dict[str, dict]:
    """{symbol: {actual, estimate, estimate_std, days_since}} from latest earnings + estimates."""
    from ....modules.dataproviders.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    details = FMPCompanyDetailsProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            earnings = _first_statement(details.get_past_earnings(sym, "quarterly", as_of, lookback_periods=1, format_type="dict"), "earnings")
            estimates = _first_statement(details.get_earnings_estimates(sym, "quarterly", as_of, lookback_periods=1, format_type="dict"), "estimates")
            report_date = _parse_date(earnings.get("report_date"))
            out[sym] = {
                "actual": earnings.get("reported_eps"),
                "estimate": earnings.get("estimated_eps"),
                "estimate_std": estimate_std_from_range(
                    estimates.get("estimated_eps_high"), estimates.get("estimated_eps_low")
                ),
                "days_since": days_since(report_date, as_of),
            }
        except Exception as e:
            logger.warning(f"FactorRanker: PEAD fetch failed for {sym}: {e}")
    return out


def _parse_date(value) -> Optional[datetime]:
    """Best-effort parse of a provider date string/datetime into an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value)[:10])
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
