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

from ba2_common.logger import logger


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


def _require_statement(result, key: str, label: str) -> dict:
    """Validate a provider result and return its most-recent record dict.

    The FMP providers return one of three shapes on failure that must NOT be
    treated as usable data:
      * an error *string* (statement methods' outer except),
      * a dict with an ``"error"`` key (earnings methods' outer except),
      * an unexpected shape missing the expected ``key``.

    Per the FactorRanker contract, any of these — or an empty record list —
    means the symbol must be dropped, so we raise a clear ValueError that the
    per-symbol caller logs and skips on.

    Args:
        result: Raw provider return value.
        key: Expected container key ("statements"/"earnings"/"estimates").
        label: Human-readable name for the dataset, used in the error message.

    Returns:
        The most recent record dict.

    Raises:
        ValueError: If the result is not a usable dict containing a non-empty
            list under ``key``.
    """
    if not isinstance(result, dict):
        raise ValueError(f"{label} unavailable (provider returned {type(result).__name__}: {result!r})")
    if "error" in result:
        raise ValueError(f"{label} error: {result['error']}")
    if key not in result:
        raise ValueError(f"{label} unexpected shape (missing '{key}'): {result!r}")
    items = result.get(key) or []
    if not items:
        raise ValueError(f"{label} empty")
    return items[0]


def fetch_close_prices(symbols, lookback_days: int = 400,
                       as_of: Optional[datetime] = None,
                       ohlcv_provider=None) -> Dict[str, pd.Series]:
    """{symbol: daily Close Series}. Symbols that fail to fetch are omitted.

    ``as_of`` is the point-in-time anchor (Phase 1 backtest contract): closes are
    fetched with ``end_date=as_of`` so momentum is computed only from bars on or
    before ``as_of``. ``as_of=None`` (live) maps to ``end_date=now``, which is
    byte-identical to the pre-refactor live fetch (the OHLCV provider's default
    end_date is also "now").

    ``ohlcv_provider`` (transparent backtest speedup): when the backtest threads its
    in-memory ``MemoizedOHLCVProvider`` (via ``_gather``), use it instead of a fresh
    ``FMPOHLCVProvider``. The fetch semantics are unchanged (same ``end_date=as_of``,
    ``lookback_days``, ``interval="1d"``); only the provider object differs, so the
    closes — and therefore momentum — are results-identical. ``None`` (live) falls
    back to ``FMPOHLCVProvider()`` exactly as before.
    """
    if ohlcv_provider is not None:
        provider = ohlcv_provider
    else:
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        provider = FMPOHLCVProvider()
    end = as_of or datetime.now(timezone.utc)
    out: Dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = provider.get_ohlcv_data(sym, end_date=end, lookback_days=lookback_days, interval="1d")
            if df is not None and not df.empty and "Close" in df:
                out[sym] = df["Close"].reset_index(drop=True)
        except Exception as e:
            logger.warning(f"FactorRanker: price fetch failed for {sym}: {e}")
    return out


def fetch_value_inputs(symbols, as_of: Optional[datetime] = None,
                       ohlcv_provider=None) -> Dict[str, dict]:
    """{symbol: {eps_ttm, price, fcf_ttm, enterprise_value}} from latest annual fundamentals.

    NO-LOOKAHEAD: ``price`` and ``market_cap`` are computed from AS_OF-correct sources,
    NOT from ``FMPCompanyOverviewProvider`` (→ ``fmpsdk.company_profile``), which returns
    the CURRENT snapshot ``price``/``mktCap`` regardless of ``as_of``. Using those at a
    past ``as_of`` would contaminate earnings-yield (E/P) and FCF/EV with today's prices.

    * **price** = the OHLCV provider's close at ``as_of`` (the most recent bar on/before
      ``as_of`` — the same close the momentum factor + the rest of the backtest use).
    * **market cap** = ``as_of price × shares_outstanding``, where shares come from the
      DATED income statement (``weighted_average_shares_outstanding``, point-in-time
      filtered to statements filed on/before ``as_of``).

    If the as_of close or the dated shares are unavailable for a symbol, the symbol is
    dropped (no guessing, no fall-back to the current snapshot — that would re-introduce
    the lookahead).

    ``ohlcv_provider`` (transparent backtest speedup): when supplied (the backtest's
    in-memory ``MemoizedOHLCVProvider``, threaded via ``_gather``) the as_of close is
    read from it instead of a fresh ``FMPOHLCVProvider``. Same fetch semantics
    (``end_date=as_of``, ``lookback_days=400``, ``interval="1d"``), so the value-factor
    price reuses the memoized series and stays results-identical. ``None`` (live) falls
    back to ``FMPOHLCVProvider()`` as before.
    """
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    details = FMPCompanyDetailsProvider()
    if ohlcv_provider is not None:
        ohlcv = ohlcv_provider
    else:
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
        ohlcv = FMPOHLCVProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            income = _require_statement(
                details.get_income_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "income statement",
            )
            balance = _require_statement(
                details.get_balance_sheet(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "balance sheet",
            )
            cashflow = _require_statement(
                details.get_cashflow_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "cash flow statement",
            )
            # AS_OF price = the OHLCV close at as_of (no lookahead). Anchored at
            # end_date=as_of; the last bar (sorted ascending) is the as_of close.
            price = _as_of_close(ohlcv, sym, as_of)
            if not price or price <= 0:
                logger.warning(
                    f"FactorRanker: dropping {sym} from value inputs "
                    f"(data unavailable): no positive as_of close (price={price!r})"
                )
                continue
            # AS_OF market cap = as_of price × dated shares outstanding (no lookahead).
            # If shares are missing, we cannot reconstruct market cap as_of-correctly;
            # drop the symbol rather than fall back to the current-snapshot mktCap.
            shares = income.get("weighted_average_shares_outstanding")
            if not shares or shares <= 0:
                logger.warning(
                    f"FactorRanker: dropping {sym} from value inputs (data unavailable): "
                    f"no as_of shares outstanding to reconstruct market cap (shares={shares!r})"
                )
                continue
            market_cap = price * float(shares)
            total_debt = (balance.get("short_term_debt") or 0.0) + (balance.get("long_term_debt") or 0.0)
            out[sym] = build_value_inputs(
                eps_ttm=income.get("eps"),
                price=price,
                fcf_ttm=cashflow.get("free_cash_flow"),
                market_cap=market_cap,
                total_debt=total_debt,
                cash=balance.get("cash_and_cash_equivalents"),
            )
        except Exception as e:
            logger.warning(
                f"FactorRanker: dropping {sym} from value inputs (data unavailable): {e}"
            )
    return out


def _as_of_close(ohlcv, symbol: str, as_of: datetime) -> Optional[float]:
    """The OHLCV close at ``as_of`` (most recent bar on/before as_of), or None.

    Closes are fetched with ``end_date=as_of`` and sorted ascending by date, so the
    last row is the as_of close — the same point-in-time price the momentum factor and
    the rest of the backtest use. A short lookback suffices (we only need the last bar),
    but we keep the default window so a recent gap (holiday/halt) still resolves a bar.
    """
    df = ohlcv.get_ohlcv_data(symbol, end_date=as_of, lookback_days=400, interval="1d")
    if df is None or df.empty or "Close" not in df:
        return None
    closes = df["Close"].dropna()
    if closes.empty:
        return None
    return float(closes.iloc[-1])


def fetch_quality_inputs(symbols, as_of: Optional[datetime] = None) -> Dict[str, dict]:
    """{symbol: {roe, gross_profit, total_assets, accruals_ratio}} from latest annual fundamentals."""
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    details = FMPCompanyDetailsProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            income = _require_statement(
                details.get_income_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "income statement",
            )
            balance = _require_statement(
                details.get_balance_sheet(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "balance sheet",
            )
            cashflow = _require_statement(
                details.get_cashflow_statement(sym, "annual", as_of, lookback_periods=1, format_type="dict"),
                "statements", "cash flow statement",
            )
            inputs = build_quality_inputs(
                net_income=income.get("net_income"),
                equity=balance.get("total_shareholder_equity"),
                gross_profit=income.get("gross_profit"),
                total_assets=balance.get("total_assets"),
                operating_cash_flow=cashflow.get("operating_cash_flow"),
            )
            # Quality requires at least one usable signal: ROE present, or both
            # gross_profit and total_assets (for gross profitability). Otherwise
            # the symbol carries no quality information — drop it, don't default.
            has_signal = inputs.get("roe") is not None or (
                inputs.get("gross_profit") is not None and inputs.get("total_assets") is not None
            )
            if not has_signal:
                logger.warning(
                    f"FactorRanker: dropping {sym} from quality inputs "
                    f"(data unavailable): no ROE and no gross_profit/total_assets pair"
                )
                continue
            out[sym] = inputs
        except Exception as e:
            logger.warning(
                f"FactorRanker: dropping {sym} from quality inputs (data unavailable): {e}"
            )
    return out


def fetch_pead_inputs(symbols, as_of: Optional[datetime] = None) -> Dict[str, dict]:
    """{symbol: {actual, estimate, estimate_std, days_since}} from latest earnings + estimates."""
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    as_of = as_of or datetime.now(timezone.utc)
    details = FMPCompanyDetailsProvider()
    out: Dict[str, dict] = {}
    for sym in symbols:
        try:
            # Earnings are the critical PEAD input — drop the symbol if missing/error.
            earnings = _require_statement(
                details.get_past_earnings(sym, "quarterly", as_of, lookback_periods=1, format_type="dict"),
                "earnings", "past earnings",
            )
            # Estimates only feed the (optional) dispersion std; if they are
            # missing/error we keep the symbol with estimate_std=None rather than
            # dropping it, since SUE still needs only actual vs estimate.
            try:
                estimates = _require_statement(
                    details.get_earnings_estimates(sym, "quarterly", as_of, lookback_periods=1, format_type="dict"),
                    "estimates", "earnings estimates",
                )
            except Exception as est_err:
                logger.warning(
                    f"FactorRanker: {sym} earnings estimates unavailable "
                    f"(continuing without dispersion std): {est_err}"
                )
                estimates = {}
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
            logger.warning(
                f"FactorRanker: dropping {sym} from PEAD inputs (data unavailable): {e}"
            )
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
