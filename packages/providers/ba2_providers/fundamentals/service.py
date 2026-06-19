"""
Unified Fundamentals Service

Provides a single interface for fetching financial statements from multiple
providers with fallback support and data normalization.
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Literal

from ba2_common.logger import logger

from .models import (
    FinancialStatementResponse,
    BalanceSheetPeriod,
    IncomeStatementPeriod,
    CashFlowPeriod,
    EarningsPeriod,
    YFINANCE_BALANCE_SHEET_MAPPING,
    YFINANCE_INCOME_STATEMENT_MAPPING,
    YFINANCE_CASH_FLOW_MAPPING,
    FMP_BALANCE_SHEET_MAPPING,
    FMP_INCOME_STATEMENT_MAPPING,
    FMP_CASH_FLOW_MAPPING,
    ALPHAVANTAGE_BALANCE_SHEET_MAPPING,
    ALPHAVANTAGE_INCOME_STATEMENT_MAPPING,
    ALPHAVANTAGE_CASH_FLOW_MAPPING,
    apply_mapping,
    parse_numeric_value,
)

# Date tolerance for matching periods across providers (in days)
DATE_TOLERANCE_DAYS = 10


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string to datetime object."""
    if not date_str:
        return None
    try:
        # Try common formats
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
            try:
                return datetime.strptime(date_str.split('T')[0].split(' ')[0], "%Y-%m-%d")
            except ValueError:
                continue
        return None
    except Exception:
        return None


def dates_match(date1: str, date2: str, tolerance_days: int = DATE_TOLERANCE_DAYS) -> bool:
    """Check if two dates are within the tolerance period."""
    d1 = parse_date(date1)
    d2 = parse_date(date2)
    if not d1 or not d2:
        return False
    return abs((d1 - d2).days) <= tolerance_days


def get_fiscal_quarter(date_str: str) -> Optional[str]:
    """
    Get the fiscal quarter identifier from a date string.

    For earnings, maps both fiscal quarter end dates and announcement dates
    to the same quarter. Announcement dates are typically 3-6 weeks after quarter end.

    Returns format: "YYYY-Q#" (e.g., "2025-Q4")
    """
    d = parse_date(date_str)
    if not d:
        return None

    # Map month to fiscal quarter
    # Q1: Jan-Mar, Q2: Apr-Jun, Q3: Jul-Sep, Q4: Oct-Dec
    month = d.month
    year = d.year

    if month <= 3:
        quarter = 1
    elif month <= 6:
        quarter = 2
    elif month <= 9:
        quarter = 3
    else:
        quarter = 4

    return f"{year}-Q{quarter}"


def earnings_dates_match(date1: str, date2: str) -> bool:
    """
    Check if two earnings dates match based on fiscal quarter.

    This handles the case where yfinance uses fiscal quarter end dates
    (e.g., 2025-09-30 for Q3) and FMP uses announcement dates
    (e.g., 2025-10-30 for Q3, about 30 days later).

    For earnings specifically, if dates are within 60 days and map to
    adjacent periods, they likely represent the same earnings report.
    """
    d1 = parse_date(date1)
    d2 = parse_date(date2)
    if not d1 or not d2:
        return False

    gap = abs((d1 - d2).days)

    # If within 10 days, definitely a match
    if gap <= DATE_TOLERANCE_DAYS:
        return True

    # For earnings, check if dates are within 60 days
    # The announcement date is typically 30-45 days after quarter end
    if gap <= 60:
        # Check if the later date is within expected announcement window
        # Announcements happen 3-6 weeks after quarter end
        earlier, later = (d1, d2) if d1 < d2 else (d2, d1)

        # If earlier date is a quarter end (last day of month divisible by 3)
        # and later date is within 60 days, consider it a match
        if earlier.month in (3, 6, 9, 12) and earlier.day >= 28:
            return True

        # Also match if they're in the same fiscal quarter or adjacent
        q1 = get_fiscal_quarter(date1)
        q2 = get_fiscal_quarter(date2)
        if q1 and q2:
            # Same quarter is a match
            if q1 == q2:
                return True
            # Adjacent quarters with small gap (announcement in next quarter)
            # e.g., Q3 ends Sept 30, announced Oct 30 (Q4)
            year1, qn1 = q1.split("-Q")
            year2, qn2 = q2.split("-Q")
            if year1 == year2 and abs(int(qn1) - int(qn2)) == 1:
                return True
            # Year boundary case (Q4 -> Q1 next year)
            if int(year2) - int(year1) == 1 and qn1 == "4" and qn2 == "1":
                return True

    return False


def merge_periods(
    all_periods: List[List[Dict[str, Any]]],
    provider_names: List[str],
    use_earnings_matching: bool = False
) -> List[Dict[str, Any]]:
    """
    Merge periods from multiple providers.

    For overlapping dates (within DATE_TOLERANCE_DAYS), takes data from the
    first provider (highest priority), filling in missing fields from
    subsequent providers.

    Args:
        all_periods: List of period lists, one per provider (in priority order)
        provider_names: List of provider names corresponding to all_periods
        use_earnings_matching: Use earnings-specific date matching (handles
                               fiscal end dates vs announcement dates)

    Returns:
        Merged list of periods with combined data
    """
    if not all_periods:
        return []

    # Select matching function based on data type
    match_func = earnings_dates_match if use_earnings_matching else dates_match

    # Use first provider's periods as base
    merged = {}
    used_providers = {}

    # Process each provider in priority order
    for provider_idx, periods in enumerate(all_periods):
        provider_name = provider_names[provider_idx] if provider_idx < len(provider_names) else f"provider_{provider_idx}"

        for period in periods:
            fiscal_date = period.get("fiscal_date", "")
            if not fiscal_date:
                continue

            # Find if this date matches an existing period
            matched_key = None
            for existing_key in merged.keys():
                if match_func(existing_key, fiscal_date):
                    matched_key = existing_key
                    break

            if matched_key:
                # Merge with existing period - only add fields that are missing
                existing = merged[matched_key]
                for key, value in period.items():
                    if key not in existing or existing[key] is None:
                        existing[key] = value
                # Track which providers contributed
                if matched_key not in used_providers:
                    used_providers[matched_key] = []
                if provider_name not in used_providers[matched_key]:
                    used_providers[matched_key].append(provider_name)
            else:
                # New period
                merged[fiscal_date] = period.copy()
                used_providers[fiscal_date] = [provider_name]

    # Add provider info to each period
    result = []
    for fiscal_date in sorted(merged.keys(), reverse=True):
        period_data = merged[fiscal_date]
        period_data["_sources"] = used_providers.get(fiscal_date, [])
        result.append(period_data)

    return result


class FundamentalsService:
    """
    Unified service for fetching financial statements from multiple providers.

    Supports provider priority ordering - tries providers in order and uses
    the first successful result. Can also merge data from multiple providers.
    """

    def __init__(self, providers: List[str] = None):
        """
        Initialize the fundamentals service.

        Args:
            providers: List of provider names in priority order.
                      Default: ['yfinance', 'fmp', 'alphavantage']
        """
        self.provider_priority = providers or ['yfinance', 'fmp', 'alphavantage']
        self._providers = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize available providers."""
        # Lazy import to avoid circular dependencies
        from .details import (
            YFinanceCompanyDetailsProvider,
            FMPCompanyDetailsProvider,
            AlphaVantageCompanyDetailsProvider,
        )

        for provider_name in self.provider_priority:
            try:
                if provider_name == 'yfinance':
                    if YFinanceCompanyDetailsProvider is None:
                        logger.warning("YFinanceCompanyDetailsProvider not available (missing dependency)")
                        continue
                    self._providers['yfinance'] = YFinanceCompanyDetailsProvider()
                elif provider_name == 'fmp':
                    if FMPCompanyDetailsProvider is None:
                        logger.debug("FMPCompanyDetailsProvider not available (missing fmpsdk)")
                        continue
                    self._providers['fmp'] = FMPCompanyDetailsProvider()
                elif provider_name == 'alphavantage':
                    if AlphaVantageCompanyDetailsProvider is None:
                        logger.debug("AlphaVantageCompanyDetailsProvider not available (missing dependency)")
                        continue
                    self._providers['alphavantage'] = AlphaVantageCompanyDetailsProvider()
                logger.debug(f"Initialized provider: {provider_name}")
            except Exception as e:
                logger.warning(f"Failed to initialize provider {provider_name}: {e}")

    def get_balance_sheet(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """
        Get balance sheet data, trying providers in priority order.

        Args:
            symbol: Stock ticker symbol
            frequency: 'quarterly' or 'annual'
            end_date: End date for data range
            start_date: Start date (mutually exclusive with lookback_periods)
            lookback_periods: Number of periods to fetch

        Returns:
            Standardized FinancialStatementResponse
        """
        end_date = end_date or datetime.now()

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_balance_sheet(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_balance_sheet(result, provider_name)
                    logger.info(f"Got balance sheet for {symbol} from {provider_name}: {len(normalized.periods)} periods")
                    return normalized
                else:
                    logger.warning(f"Provider {provider_name} returned error for {symbol}: {result}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        # All providers failed
        return FinancialStatementResponse(
            symbol=symbol,
            provider="none",
            statement_type="balance_sheet",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=[],
            period_count=0
        )

    def get_income_statement(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """Get income statement data, trying providers in priority order."""
        end_date = end_date or datetime.now()

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_income_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_income_statement(result, provider_name)
                    logger.info(f"Got income statement for {symbol} from {provider_name}: {len(normalized.periods)} periods")
                    return normalized
                else:
                    logger.warning(f"Provider {provider_name} returned error for {symbol}: {result}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        return FinancialStatementResponse(
            symbol=symbol,
            provider="none",
            statement_type="income_statement",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=[],
            period_count=0
        )

    def get_cash_flow(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """Get cash flow statement data, trying providers in priority order."""
        end_date = end_date or datetime.now()

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_cashflow_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_cash_flow(result, provider_name)
                    logger.info(f"Got cash flow for {symbol} from {provider_name}: {len(normalized.periods)} periods")
                    return normalized
                else:
                    logger.warning(f"Provider {provider_name} returned error for {symbol}: {result}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        return FinancialStatementResponse(
            symbol=symbol,
            provider="none",
            statement_type="cash_flow",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=[],
            period_count=0
        )

    def get_earnings(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        lookback_periods: int = 8,
    ) -> FinancialStatementResponse:
        """Get earnings data, trying providers in priority order."""
        end_date = end_date or datetime.now()

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]

                # YFinance earnings is deprecated, try to get from income statement
                if provider_name == 'yfinance':
                    result = self._get_yfinance_earnings_from_income(
                        provider, symbol, frequency, end_date, lookback_periods
                    )
                else:
                    result = provider.get_past_earnings(
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_date,
                        lookback_periods=lookback_periods,
                        format_type="dict"
                    )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_earnings(result, provider_name)
                    if normalized.periods:
                        logger.info(f"Got earnings for {symbol} from {provider_name}: {len(normalized.periods)} periods")
                        return normalized
                else:
                    logger.warning(f"Provider {provider_name} returned error for {symbol}: {result}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        return FinancialStatementResponse(
            symbol=symbol,
            provider="none",
            statement_type="earnings",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=[],
            period_count=0
        )

    def _get_yfinance_earnings_from_income(
        self,
        provider,
        symbol: str,
        frequency: str,
        end_date: datetime,
        lookback_periods: int
    ) -> Dict[str, Any]:
        """
        Extract earnings data from YFinance income statement.

        YFinance deprecated the get_earnings method, so we extract EPS
        from the income statement instead.
        """
        try:
            result = provider.get_income_statement(
                symbol=symbol,
                frequency=frequency,
                end_date=end_date,
                lookback_periods=lookback_periods,
                format_type="dict"
            )

            if isinstance(result, dict) and "periods" in result:
                earnings = []
                for period in result.get("periods", []):
                    items = period.get("items", {})

                    # Extract EPS from income statement
                    basic_eps = items.get("Basic EPS", items.get("Diluted EPS", 0))

                    earnings.append({
                        "fiscal_date_ending": period.get("date", ""),
                        "reported_eps": float(basic_eps) if basic_eps else 0,
                        "estimated_eps": None,  # Not available from income statement
                        "surprise": None,
                        "surprise_percent": None
                    })

                return {
                    "symbol": symbol,
                    "frequency": frequency,
                    "earnings": earnings
                }
        except Exception as e:
            logger.warning(f"Failed to extract earnings from income statement: {e}")

        return {"error": "Failed to get earnings from income statement"}

    def _normalize_period(
        self,
        raw_period: Dict[str, Any],
        provider_name: str,
        mapping_type: str
    ) -> Dict[str, Any]:
        """
        Normalize a single period from any provider.

        Args:
            raw_period: Raw period data from provider
            provider_name: Name of the provider
            mapping_type: Type of mapping ('balance_sheet', 'income_statement', 'cash_flow')

        Returns:
            Normalized period dictionary with standardized field names
        """
        # Select appropriate mapping
        mappings = {
            "yfinance": {
                "balance_sheet": YFINANCE_BALANCE_SHEET_MAPPING,
                "income_statement": YFINANCE_INCOME_STATEMENT_MAPPING,
                "cash_flow": YFINANCE_CASH_FLOW_MAPPING,
            },
            "fmp": {
                "balance_sheet": FMP_BALANCE_SHEET_MAPPING,
                "income_statement": FMP_INCOME_STATEMENT_MAPPING,
                "cash_flow": FMP_CASH_FLOW_MAPPING,
            },
            "alphavantage": {
                "balance_sheet": ALPHAVANTAGE_BALANCE_SHEET_MAPPING,
                "income_statement": ALPHAVANTAGE_INCOME_STATEMENT_MAPPING,
                "cash_flow": ALPHAVANTAGE_CASH_FLOW_MAPPING,
            },
        }

        mapping = mappings.get(provider_name, {}).get(mapping_type, {})

        if provider_name == "yfinance":
            # YFinance uses 'date' and 'items' structure
            fiscal_date = raw_period.get("date", "")
            items = raw_period.get("items", {})
            normalized = apply_mapping(items, mapping, strict=True)
            normalized["fiscal_date"] = fiscal_date
        elif provider_name in ("fmp", "alphavantage"):
            # FMP and AlphaVantage use flat structure
            normalized = apply_mapping(raw_period, mapping, strict=True)
        else:
            # Unknown provider - try to extract fiscal_date and parse values
            normalized = {}
            for key, value in raw_period.items():
                if key in ("fiscalDateEnding", "fiscal_date_ending", "date"):
                    normalized["fiscal_date"] = value
                else:
                    parsed = parse_numeric_value(value)
                    if parsed is not None:
                        normalized[key] = parsed

        return normalized

    def _normalize_balance_sheet(
        self,
        result: Dict[str, Any],
        provider_name: str
    ) -> FinancialStatementResponse:
        """Normalize balance sheet data to standard format."""
        periods = []

        # Get the raw periods from the result
        raw_periods = result.get("periods", result.get("statements", []))

        for raw_period in raw_periods:
            normalized = self._normalize_period(raw_period, provider_name, "balance_sheet")
            if normalized.get("fiscal_date"):
                periods.append(normalized)

        return FinancialStatementResponse(
            symbol=result.get("symbol", ""),
            provider=provider_name,
            statement_type="balance_sheet",
            frequency=result.get("frequency", "quarterly"),
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
            periods=periods,
            period_count=len(periods)
        )

    def _normalize_income_statement(
        self,
        result: Dict[str, Any],
        provider_name: str
    ) -> FinancialStatementResponse:
        """Normalize income statement data to standard format."""
        periods = []

        raw_periods = result.get("periods", result.get("statements", []))

        for raw_period in raw_periods:
            normalized = self._normalize_period(raw_period, provider_name, "income_statement")
            if normalized.get("fiscal_date"):
                periods.append(normalized)

        return FinancialStatementResponse(
            symbol=result.get("symbol", ""),
            provider=provider_name,
            statement_type="income_statement",
            frequency=result.get("frequency", "quarterly"),
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
            periods=periods,
            period_count=len(periods)
        )

    def _normalize_cash_flow(
        self,
        result: Dict[str, Any],
        provider_name: str
    ) -> FinancialStatementResponse:
        """Normalize cash flow data to standard format."""
        periods = []

        raw_periods = result.get("periods", result.get("statements", []))

        for raw_period in raw_periods:
            normalized = self._normalize_period(raw_period, provider_name, "cash_flow")
            if normalized.get("fiscal_date"):
                periods.append(normalized)

        return FinancialStatementResponse(
            symbol=result.get("symbol", ""),
            provider=provider_name,
            statement_type="cash_flow",
            frequency=result.get("frequency", "quarterly"),
            start_date=result.get("start_date"),
            end_date=result.get("end_date"),
            periods=periods,
            period_count=len(periods)
        )

    def _normalize_earnings(
        self,
        result: Dict[str, Any],
        provider_name: str
    ) -> FinancialStatementResponse:
        """Normalize earnings data to standard format."""
        periods = []

        raw_earnings = result.get("earnings", [])

        for raw_earning in raw_earnings:
            normalized = {
                "fiscal_date": raw_earning.get("fiscal_date_ending", raw_earning.get("fiscal_date", "")),
                "report_date": raw_earning.get("report_date"),
                "reported_eps": parse_numeric_value(raw_earning.get("reported_eps")),
                "estimated_eps": parse_numeric_value(raw_earning.get("estimated_eps")),
                "surprise": parse_numeric_value(raw_earning.get("surprise")),
                "surprise_percent": parse_numeric_value(raw_earning.get("surprise_percent")),
            }
            # Remove None values
            normalized = {k: v for k, v in normalized.items() if v is not None}
            periods.append(normalized)

        return FinancialStatementResponse(
            symbol=result.get("symbol", ""),
            provider=provider_name,
            statement_type="earnings",
            frequency=result.get("frequency", "quarterly"),
            end_date=result.get("end_date"),
            periods=periods,
            period_count=len(periods)
        )


    def get_balance_sheet_merged(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """
        Get balance sheet data from ALL providers and merge them.

        Periods with dates within 10 days are considered the same period.
        Data is merged with priority given to providers in the order specified.
        """
        end_date = end_date or datetime.now()
        all_periods = []
        provider_names = []

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_balance_sheet(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_balance_sheet(result, provider_name)
                    if normalized.periods:
                        all_periods.append(normalized.periods)
                        provider_names.append(provider_name)
                        logger.info(f"Got {len(normalized.periods)} balance sheet periods from {provider_name}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        # Merge all periods
        merged = merge_periods(all_periods, provider_names)
        primary_provider = provider_names[0] if provider_names else "none"

        return FinancialStatementResponse(
            symbol=symbol,
            provider=",".join(provider_names) if provider_names else "none",
            statement_type="balance_sheet",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=merged,
            period_count=len(merged)
        )

    def get_income_statement_merged(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """Get income statement data from ALL providers and merge them."""
        end_date = end_date or datetime.now()
        all_periods = []
        provider_names = []

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_income_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_income_statement(result, provider_name)
                    if normalized.periods:
                        all_periods.append(normalized.periods)
                        provider_names.append(provider_name)
                        logger.info(f"Got {len(normalized.periods)} income statement periods from {provider_name}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        merged = merge_periods(all_periods, provider_names)

        return FinancialStatementResponse(
            symbol=symbol,
            provider=",".join(provider_names) if provider_names else "none",
            statement_type="income_statement",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=merged,
            period_count=len(merged)
        )

    def get_cash_flow_merged(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        start_date: Optional[datetime] = None,
        lookback_periods: Optional[int] = None,
    ) -> FinancialStatementResponse:
        """Get cash flow data from ALL providers and merge them."""
        end_date = end_date or datetime.now()
        all_periods = []
        provider_names = []

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]
                result = provider.get_cashflow_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_date,
                    start_date=start_date,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_cash_flow(result, provider_name)
                    if normalized.periods:
                        all_periods.append(normalized.periods)
                        provider_names.append(provider_name)
                        logger.info(f"Got {len(normalized.periods)} cash flow periods from {provider_name}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        merged = merge_periods(all_periods, provider_names)

        return FinancialStatementResponse(
            symbol=symbol,
            provider=",".join(provider_names) if provider_names else "none",
            statement_type="cash_flow",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=merged,
            period_count=len(merged)
        )


    def get_earnings_merged(
        self,
        symbol: str,
        frequency: Literal["quarterly", "annual"] = "quarterly",
        end_date: datetime = None,
        lookback_periods: int = 8,
    ) -> FinancialStatementResponse:
        """
        Get earnings data from ALL providers and merge them.

        This is especially useful because yfinance doesn't provide estimated_eps,
        surprise, and surprise_percent, but FMP does.
        """
        end_date = end_date or datetime.now()
        all_periods = []
        provider_names = []

        for provider_name in self.provider_priority:
            if provider_name not in self._providers:
                continue

            try:
                provider = self._providers[provider_name]

                # YFinance earnings is deprecated, try to get from income statement
                if provider_name == 'yfinance':
                    result = self._get_yfinance_earnings_from_income(
                        provider, symbol, frequency, end_date, lookback_periods
                    )
                else:
                    result = provider.get_past_earnings(
                        symbol=symbol,
                        frequency=frequency,
                        end_date=end_date,
                        lookback_periods=lookback_periods,
                        format_type="dict"
                    )

                if isinstance(result, dict) and not result.get("error"):
                    normalized = self._normalize_earnings(result, provider_name)
                    if normalized.periods:
                        all_periods.append(normalized.periods)
                        provider_names.append(provider_name)
                        logger.info(f"Got {len(normalized.periods)} earnings periods from {provider_name}")

            except Exception as e:
                logger.warning(f"Provider {provider_name} failed for {symbol}: {e}")
                continue

        # Merge all periods using earnings-specific date matching
        # This handles yfinance fiscal dates vs FMP announcement dates
        merged = merge_periods(all_periods, provider_names, use_earnings_matching=True)

        return FinancialStatementResponse(
            symbol=symbol,
            provider=",".join(provider_names) if provider_names else "none",
            statement_type="earnings",
            frequency=frequency,
            end_date=end_date.isoformat() if end_date else None,
            periods=merged,
            period_count=len(merged)
        )


def get_fundamentals_service(providers: List[str] = None) -> FundamentalsService:
    """Factory function to create a FundamentalsService instance."""
    return FundamentalsService(providers=providers)
