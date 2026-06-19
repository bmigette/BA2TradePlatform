"""
Fundamentals Service

Fetches fundamental data (FCF, P/E, EPS, Revenue) for tickers and creates
derived features for ML model training.

Supports two modes:
1. Legacy mode: Point-in-time features (days_to_last_fcf, last_fcf, etc.)
2. Statement mode: Statement-based features with lookback (bs_q0_total_assets, etc.)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging
import yfinance as yf

logger = logging.getLogger(__name__)

# Statement type to prefix mapping
STATEMENT_PREFIXES = {
    'balance_sheet': 'bs',
    'income_statement': 'is',
    'cash_flow': 'cf',
    'earnings': 'earn'
}

# Legacy metric names mapped to their statement types
# These allow using legacy config names with the statement-based system
LEGACY_TO_STATEMENT = {
    'fcf': 'cash_flow',           # Free cash flow is in cash flow statement
    'pe': 'income_statement',      # P/E ratio comes from EPS in income statement
    'eps': 'income_statement',     # EPS is in income statement
    'revenue': 'income_statement', # Revenue is in income statement
    'debt_equity': 'balance_sheet', # Debt/equity is in balance sheet
    'roe': 'balance_sheet',        # ROE is derived from balance sheet + income
}

# Key fields to extract for each statement type (subset of available fields)
STATEMENT_KEY_FIELDS = {
    'balance_sheet': [
        'total_assets', 'total_liabilities', 'total_stockholders_equity',
        'cash_and_cash_equivalents', 'long_term_debt', 'total_current_assets',
        'total_current_liabilities', 'net_debt', 'working_capital'
    ],
    'income_statement': [
        'total_revenue', 'gross_profit', 'operating_income', 'net_income',
        'basic_eps', 'diluted_eps', 'ebitda', 'operating_expenses'
    ],
    'cash_flow': [
        'operating_cash_flow', 'capital_expenditure', 'free_cash_flow',
        'investing_cash_flow', 'financing_cash_flow', 'dividends_paid'
    ],
    'earnings': [
        'reported_eps', 'estimated_eps', 'surprise', 'surprise_percent'
    ]
}


class FundamentalsService:
    """
    Service for fetching and processing fundamental financial data.

    Creates features like:
    - days_to_last_FCF
    - last_FCF value
    - last_FCF_percent change
    - days_to_next_FCF (estimated)
    - next_FCF_forecast (estimated)
    """

    # Fundamental metrics to fetch
    FUNDAMENTAL_METRICS = [
        'FreeCashFlow',
        'TrailingPE',
        'ForwardPE',
        'EarningsPerShare',
        'TotalRevenue',
        'DebtToEquity',
        'ReturnOnEquity',
        'PriceToBook',
        'DividendYield'
    ]

    @staticmethod
    def _build_provider_service(provider_list: List[str]):
        """Construct the statement provider-service for the dataset builder.

        Secondary re-source seam (Phase 5, Task 6), parallel to the OHLCV seam:
        when ``FEATURES_SOURCE=ba2_providers`` is explicitly selected, statements
        are intended to be sourced through ba2_providers' shared cache (category
        ``fundamentals_details``; names alphavantage/fmp/yfinance). DEFAULT is
        ``legacy`` so nothing changes; verification is DEFERRED to plan Task 8 (do
        NOT flip the default until per-block equivalence of bs_/is_/cf_/earn_
        columns is documented).

        The legacy ``dataproviders.fundamentals.service.FundamentalsService`` is a
        multi-provider ORCHESTRATOR whose surface (``get_balance_sheet`` /
        ``get_income_statement`` / ``get_cash_flow`` / ``get_earnings_merged``)
        differs from a single ba2_providers ``fundamentals_details`` provider
        (``get_cashflow_statement``, no merged-earnings, etc.). A real cutover
        therefore needs an orchestrator-shaped adapter over ba2_providers, which is
        the deferred Task-8 work. Here we only WIRE the flag: when selected we probe
        ba2_providers (so a misconfiguration surfaces) and then fall back to the
        legacy orchestrator, which remains the actual fetch path. Returns the
        constructed provider-service or ``None`` on import/init failure (callers
        treat ``None`` as "no statement features").
        """
        from app.services.features_source import use_ba2_providers, get_ba2_provider

        if use_ba2_providers():
            # Probe ba2_providers for the first requested name so a flag/config
            # error is visible; the legacy orchestrator below is still used until
            # Task 8 lands the orchestrator-shaped adapter (deferred verification).
            probe_name = (provider_list or ['fmp'])[0]
            probe = get_ba2_provider("fundamentals_details", probe_name)
            if probe is not None:
                logger.info(
                    "FEATURES_SOURCE=ba2_providers: fundamentals_details '%s' available "
                    "(verification deferred to Task 8; using legacy orchestrator for the "
                    "statement fetch this phase)",
                    probe_name,
                )
            else:
                logger.warning(
                    "FEATURES_SOURCE=ba2_providers: fundamentals_details '%s' unavailable; "
                    "using legacy orchestrator",
                    probe_name,
                )

        try:
            from ba2_providers.fundamentals.service import (
                FundamentalsService as ProviderService,
            )
        except ImportError as e:
            logger.error(f"Failed to import provider service: {e}")
            return None
        try:
            return ProviderService(providers=provider_list)
        except Exception as e:
            logger.error(f"Failed to initialize provider service: {e}")
            return None

    @staticmethod
    def get_fundamental_data(ticker: str) -> Dict[str, Any]:
        """
        Fetch fundamental data for a ticker using yfinance.

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dictionary with fundamental metrics and dates
        """
        try:
            stock = yf.Ticker(ticker)
            info = stock.info

            # Get quarterly financials for historical data
            quarterly_cashflow = stock.quarterly_cashflow
            quarterly_income = stock.quarterly_income_stmt

            fundamentals = {
                'ticker': ticker,
                'fetch_date': datetime.now().isoformat(),
                'current': {},
                'historical': {}
            }

            # Current values from info
            fundamentals['current'] = {
                'free_cash_flow': info.get('freeCashflow'),
                'trailing_pe': info.get('trailingPE'),
                'forward_pe': info.get('forwardPE'),
                'eps': info.get('trailingEps'),
                'forward_eps': info.get('forwardEps'),
                'revenue': info.get('totalRevenue'),
                'debt_to_equity': info.get('debtToEquity'),
                'roe': info.get('returnOnEquity'),
                'price_to_book': info.get('priceToBook'),
                'dividend_yield': info.get('dividendYield'),
                'market_cap': info.get('marketCap')
            }

            # Historical quarterly data
            if not quarterly_cashflow.empty:
                fcf_data = []
                if 'Free Cash Flow' in quarterly_cashflow.index:
                    for date in quarterly_cashflow.columns:
                        value = quarterly_cashflow.loc['Free Cash Flow', date]
                        if pd.notna(value):
                            fcf_data.append({
                                'date': date.isoformat() if hasattr(date, 'isoformat') else str(date),
                                'value': float(value)
                            })
                fundamentals['historical']['free_cash_flow'] = fcf_data

            if not quarterly_income.empty:
                # Revenue history
                revenue_data = []
                if 'Total Revenue' in quarterly_income.index:
                    for date in quarterly_income.columns:
                        value = quarterly_income.loc['Total Revenue', date]
                        if pd.notna(value):
                            revenue_data.append({
                                'date': date.isoformat() if hasattr(date, 'isoformat') else str(date),
                                'value': float(value)
                            })
                fundamentals['historical']['revenue'] = revenue_data

                # EPS history
                eps_data = []
                if 'Basic EPS' in quarterly_income.index:
                    for date in quarterly_income.columns:
                        value = quarterly_income.loc['Basic EPS', date]
                        if pd.notna(value):
                            eps_data.append({
                                'date': date.isoformat() if hasattr(date, 'isoformat') else str(date),
                                'value': float(value)
                            })
                fundamentals['historical']['eps'] = eps_data

            logger.info(f"Fetched fundamental data for {ticker}")
            return fundamentals

        except Exception as e:
            logger.error(f"Error fetching fundamentals for {ticker}: {e}")
            raise

    @staticmethod
    def create_fundamental_features(
        df: pd.DataFrame,
        ticker: str,
        metrics: List[str] = None
    ) -> pd.DataFrame:
        """
        Create fundamental-derived features for a dataset.

        For each metric, creates:
        - days_to_last_{metric}: Days since last reported value
        - last_{metric}: Most recent value
        - last_{metric}_percent: Percent change from previous period
        - days_to_next_{metric}: Estimated days to next report
        - next_{metric}_forecast: Simple forecast based on trend

        Args:
            df: DataFrame with Date column
            ticker: Stock ticker
            metrics: List of metrics to process (default: ['fcf', 'pe', 'eps', 'revenue'])

        Returns:
            DataFrame with added fundamental features
        """
        if metrics is None:
            metrics = ['fcf', 'pe', 'eps', 'revenue']

        result_df = df.copy()

        # Ensure Date is datetime
        if 'Date' in result_df.columns:
            result_df['Date'] = pd.to_datetime(result_df['Date'])

        try:
            # Fetch fundamental data
            fund_data = FundamentalsService.get_fundamental_data(ticker)

            # Process each metric
            for metric in metrics:
                result_df = FundamentalsService._add_metric_features(
                    result_df, fund_data, metric
                )

            logger.info(f"Created fundamental features for {len(metrics)} metrics")
            return result_df

        except Exception as e:
            logger.error(f"Error creating fundamental features: {e}")
            # Return original dataframe with NaN features
            for metric in metrics:
                result_df[f'days_to_last_{metric}'] = np.nan
                result_df[f'last_{metric}'] = np.nan
                result_df[f'last_{metric}_percent'] = np.nan
                result_df[f'days_to_next_{metric}'] = np.nan
                result_df[f'next_{metric}_forecast'] = np.nan
            return result_df

    @staticmethod
    def _add_metric_features(
        df: pd.DataFrame,
        fund_data: Dict,
        metric: str
    ) -> pd.DataFrame:
        """
        Add features for a specific fundamental metric.

        Args:
            df: DataFrame with Date column
            fund_data: Fundamental data dictionary
            metric: Metric name (fcf, pe, eps, revenue)

        Returns:
            DataFrame with added features
        """
        result_df = df.copy()

        # Map metric names to data keys
        metric_map = {
            'fcf': 'free_cash_flow',
            'pe': 'trailing_pe',
            'eps': 'eps',
            'revenue': 'revenue',
            'de': 'debt_to_equity',
            'roe': 'roe'
        }

        data_key = metric_map.get(metric, metric)

        # Get historical data if available
        historical = fund_data.get('historical', {}).get(data_key, [])
        current_value = fund_data.get('current', {}).get(data_key)

        if not historical and current_value is None:
            # No data available
            result_df[f'days_to_last_{metric}'] = np.nan
            result_df[f'last_{metric}'] = np.nan
            result_df[f'last_{metric}_percent'] = np.nan
            result_df[f'days_to_next_{metric}'] = np.nan
            result_df[f'next_{metric}_forecast'] = np.nan
            return result_df

        # Process historical data
        if historical:
            # Sort by date
            historical = sorted(historical, key=lambda x: x['date'], reverse=True)

            # Create a series for looking up values by date
            dates = [pd.to_datetime(h['date']) for h in historical]
            values = [h['value'] for h in historical]

            # Calculate percent changes
            percent_changes = [0]  # First value has no previous
            for i in range(1, len(values)):
                if values[i] != 0:
                    pct = (values[i-1] - values[i]) / abs(values[i]) * 100
                    percent_changes.append(pct)
                else:
                    percent_changes.append(0)

            # For each date in the dataset, find the most recent fundamental date
            def get_days_since_last(row_date):
                for i, d in enumerate(dates):
                    if d <= row_date:
                        return (row_date - d).days
                return np.nan

            def get_last_value(row_date):
                for i, d in enumerate(dates):
                    if d <= row_date:
                        return values[i]
                return np.nan

            def get_last_percent(row_date):
                for i, d in enumerate(dates):
                    if d <= row_date:
                        return percent_changes[i]
                return np.nan

            def get_days_to_next(row_date):
                # Estimate based on quarterly reports (90 days)
                last_days = get_days_since_last(row_date)
                if pd.isna(last_days):
                    return np.nan
                return max(0, 90 - last_days)

            def get_next_forecast(row_date):
                # Simple forecast: last value * (1 + average percent change)
                last_val = get_last_value(row_date)
                if pd.isna(last_val):
                    return np.nan
                avg_change = np.mean([p for p in percent_changes if p != 0])
                if pd.isna(avg_change):
                    return last_val
                return last_val * (1 + avg_change / 100)

            result_df[f'days_to_last_{metric}'] = result_df['Date'].apply(get_days_since_last)
            result_df[f'last_{metric}'] = result_df['Date'].apply(get_last_value)
            result_df[f'last_{metric}_percent'] = result_df['Date'].apply(get_last_percent)
            result_df[f'days_to_next_{metric}'] = result_df['Date'].apply(get_days_to_next)
            result_df[f'next_{metric}_forecast'] = result_df['Date'].apply(get_next_forecast)

        else:
            # Only current value available
            result_df[f'days_to_last_{metric}'] = np.nan
            result_df[f'last_{metric}'] = current_value
            result_df[f'last_{metric}_percent'] = np.nan
            result_df[f'days_to_next_{metric}'] = np.nan
            result_df[f'next_{metric}_forecast'] = np.nan

        return result_df

    @staticmethod
    def create_statement_features(
        df: pd.DataFrame,
        ticker: str,
        statement_types: List[str],
        lookback_statements: int = 2,
        providers: List[str] = None,
        frequency: str = 'quarterly'
    ) -> pd.DataFrame:
        """
        Create statement-based features with lookback periods.

        For each statement type and lookback period, creates columns like:
        - bs_q0_total_assets (most recent quarter)
        - bs_q1_total_assets (previous quarter)
        - is_q0_net_income, is_q1_net_income, etc.

        Args:
            df: DataFrame with Date column
            ticker: Stock ticker symbol
            statement_types: List of statement types to fetch
                            ('balance_sheet', 'income_statement', 'cash_flow', 'earnings')
            lookback_statements: Number of historical periods to include (default: 2)
            providers: List of providers in priority order (default: ['yfinance'])
            frequency: 'quarterly' or 'annual' (default: 'quarterly')

        Returns:
            DataFrame with added statement features
        """
        result_df = df.copy()

        # Ensure Date is datetime
        if 'Date' in result_df.columns:
            result_df['Date'] = pd.to_datetime(result_df['Date'])

        # Convert legacy metric names to statement types
        converted_types = set()
        for st in statement_types:
            if st in STATEMENT_PREFIXES:
                converted_types.add(st)
            elif st in LEGACY_TO_STATEMENT:
                converted_types.add(LEGACY_TO_STATEMENT[st])
                logger.debug(f"Converted legacy type '{st}' to '{LEGACY_TO_STATEMENT[st]}'")
            else:
                logger.warning(f"Unknown statement type: {st}")

        valid_statement_types = list(converted_types)

        if not valid_statement_types:
            logger.warning("No valid statement types provided")
            return result_df

        logger.info(f"Processing statement types: {valid_statement_types}")

        # Initialize the statement provider-service via the FEATURES_SOURCE seam
        # (default legacy; ba2_providers route wired but verification deferred).
        provider_list = providers or ['yfinance']
        provider_service = FundamentalsService._build_provider_service(provider_list)
        if provider_service is None:
            return result_df

        # Get the date range - fetch enough historical data for lookback
        min_date = result_df['Date'].min()
        max_date = result_df['Date'].max()

        # Fetch extra periods for lookback (e.g., if lookback=2, fetch 2 extra)
        fetch_periods = lookback_statements + 8  # Extra buffer for point-in-time

        # Process each valid statement type
        for stmt_type in valid_statement_types:
            prefix = STATEMENT_PREFIXES[stmt_type]
            key_fields = STATEMENT_KEY_FIELDS.get(stmt_type, [])

            try:
                # Fetch statement data
                if stmt_type == 'balance_sheet':
                    response = provider_service.get_balance_sheet(
                        symbol=ticker,
                        frequency=frequency,
                        end_date=max_date,
                        lookback_periods=fetch_periods
                    )
                elif stmt_type == 'income_statement':
                    response = provider_service.get_income_statement(
                        symbol=ticker,
                        frequency=frequency,
                        end_date=max_date,
                        lookback_periods=fetch_periods
                    )
                elif stmt_type == 'cash_flow':
                    response = provider_service.get_cash_flow(
                        symbol=ticker,
                        frequency=frequency,
                        end_date=max_date,
                        lookback_periods=fetch_periods
                    )
                elif stmt_type == 'earnings':
                    # Use merged earnings to combine data from multiple providers
                    # yfinance provides reported_eps, FMP provides estimated_eps/surprise
                    response = provider_service.get_earnings_merged(
                        symbol=ticker,
                        frequency=frequency,
                        end_date=max_date,
                        lookback_periods=fetch_periods
                    )
                else:
                    continue

                periods = response.periods
                if not periods:
                    logger.warning(f"No {stmt_type} data available for {ticker}")
                    # Add NaN columns
                    for q_idx in range(lookback_statements):
                        for field in key_fields:
                            result_df[f'{prefix}_q{q_idx}_{field}'] = np.nan
                    continue

                logger.info(f"Fetched {len(periods)} {stmt_type} periods for {ticker}")

                # Sort periods by fiscal_date descending (most recent first)
                periods_sorted = sorted(
                    periods,
                    key=lambda p: p.get('fiscal_date', ''),
                    reverse=True
                )

                # Create lookup function for point-in-time data
                def get_periods_for_date(row_date):
                    """Get N most recent periods before or on row_date."""
                    available = []
                    # Normalize row_date to timezone-naive for comparison
                    if hasattr(row_date, 'tzinfo') and row_date.tzinfo is not None:
                        row_date_naive = row_date.replace(tzinfo=None)
                    else:
                        row_date_naive = row_date

                    for period in periods_sorted:
                        fiscal_date = period.get('fiscal_date', '')
                        if fiscal_date:
                            try:
                                period_date = pd.to_datetime(fiscal_date)
                                # Normalize period_date to timezone-naive
                                if hasattr(period_date, 'tzinfo') and period_date.tzinfo is not None:
                                    period_date = period_date.replace(tzinfo=None)
                                if period_date <= row_date_naive:
                                    available.append(period)
                                    if len(available) >= lookback_statements:
                                        break
                            except Exception:
                                continue
                    return available

                # Add columns for each lookback period and field
                for q_idx in range(lookback_statements):
                    for field in key_fields:
                        col_name = f'{prefix}_q{q_idx}_{field}'

                        def get_value(row_date, q_idx=q_idx, field=field):
                            available_periods = get_periods_for_date(row_date)
                            if q_idx < len(available_periods):
                                return available_periods[q_idx].get(field)
                            return np.nan

                        result_df[col_name] = result_df['Date'].apply(get_value)

                # Also add fiscal date columns for reference
                for q_idx in range(lookback_statements):
                    col_name = f'{prefix}_q{q_idx}_date'

                    def get_date(row_date, q_idx=q_idx):
                        available_periods = get_periods_for_date(row_date)
                        if q_idx < len(available_periods):
                            return available_periods[q_idx].get('fiscal_date')
                        return None

                    result_df[col_name] = result_df['Date'].apply(get_date)

                # Add days_since columns for each lookback period
                for q_idx in range(lookback_statements):
                    col_name = f'{prefix}_q{q_idx}_days_since'

                    def get_days_since(row_date, q_idx=q_idx):
                        available_periods = get_periods_for_date(row_date)
                        if q_idx < len(available_periods):
                            fiscal_date_str = available_periods[q_idx].get('fiscal_date')
                            if fiscal_date_str:
                                try:
                                    fiscal_date = pd.to_datetime(fiscal_date_str)
                                    if hasattr(fiscal_date, 'tzinfo') and fiscal_date.tzinfo is not None:
                                        fiscal_date = fiscal_date.replace(tzinfo=None)
                                    row_date_naive = row_date
                                    if hasattr(row_date, 'tzinfo') and row_date.tzinfo is not None:
                                        row_date_naive = row_date.replace(tzinfo=None)
                                    return (row_date_naive - fiscal_date).days
                                except:
                                    pass
                        return np.nan

                    result_df[col_name] = result_df['Date'].apply(get_days_since)

            except Exception as e:
                logger.error(f"Error fetching {stmt_type} for {ticker}: {e}")
                # Add NaN columns on error
                for q_idx in range(lookback_statements):
                    for field in key_fields:
                        result_df[f'{prefix}_q{q_idx}_{field}'] = np.nan

        logger.info(f"Created statement features for {ticker}: {valid_statement_types} with {lookback_statements} periods")
        return result_df

    @staticmethod
    def create_statement_features_v2(
        df: pd.DataFrame,
        ticker: str,
        statement_types: List[str],
        providers: List[str] = None,
        frequency: str = 'quarterly'
    ) -> pd.DataFrame:
        """
        Create statement-based features with simpler structure (v2).

        Instead of quarter-indexed columns (q0, q1, q2...), creates:
        - {prefix}_{field}: Current (most recent) value
        - {prefix}_{field}_days_old: Days since the data was released
        - {prefix}_{field}_qoq_change: Quarter-over-quarter % change
        - {prefix}_{field}_yoy_change: Year-over-year % change

        Includes warmup: backfills earliest known value to start of dataset.

        Args:
            df: DataFrame with Date column
            ticker: Stock ticker symbol
            statement_types: List of statement types to fetch
            providers: List of providers in priority order (default: ['yfinance'])
            frequency: 'quarterly' or 'annual' (default: 'quarterly')

        Returns:
            DataFrame with added statement features
        """
        result_df = df.copy()

        if 'Date' in result_df.columns:
            result_df['Date'] = pd.to_datetime(result_df['Date'])

        # Convert legacy metric names to statement types
        converted_types = set()
        for st in statement_types:
            if st in STATEMENT_PREFIXES:
                converted_types.add(st)
            elif st in LEGACY_TO_STATEMENT:
                converted_types.add(LEGACY_TO_STATEMENT[st])

        valid_statement_types = list(converted_types)
        if not valid_statement_types:
            return result_df

        # Initialize the statement provider-service via the FEATURES_SOURCE seam
        # (default legacy; ba2_providers route wired but verification deferred).
        provider_list = providers or ['yfinance']
        provider_service = FundamentalsService._build_provider_service(provider_list)
        if provider_service is None:
            return result_df

        min_date = result_df['Date'].min()
        max_date = result_df['Date'].max()

        # Calculate how many quarters the dataset spans, plus warmup
        # We need: 4 quarters for YoY + quarters spanning the dataset + buffer
        dataset_days = (max_date - min_date).days
        dataset_quarters = max(1, dataset_days // 90)
        warmup_quarters = 5  # 4 for YoY calculation + 1 buffer
        fetch_periods = dataset_quarters + warmup_quarters + 4  # Extra buffer

        logger.info(f"Fetching {fetch_periods} periods for {dataset_quarters} quarter dataset + {warmup_quarters} warmup")

        for stmt_type in valid_statement_types:
            prefix = STATEMENT_PREFIXES[stmt_type]
            key_fields = STATEMENT_KEY_FIELDS.get(stmt_type, [])

            try:
                # Fetch statement data
                if stmt_type == 'balance_sheet':
                    response = provider_service.get_balance_sheet(
                        symbol=ticker, frequency=frequency,
                        end_date=max_date, lookback_periods=fetch_periods
                    )
                elif stmt_type == 'income_statement':
                    response = provider_service.get_income_statement(
                        symbol=ticker, frequency=frequency,
                        end_date=max_date, lookback_periods=fetch_periods
                    )
                elif stmt_type == 'cash_flow':
                    response = provider_service.get_cash_flow(
                        symbol=ticker, frequency=frequency,
                        end_date=max_date, lookback_periods=fetch_periods
                    )
                elif stmt_type == 'earnings':
                    response = provider_service.get_earnings_merged(
                        symbol=ticker, frequency=frequency,
                        end_date=max_date, lookback_periods=fetch_periods
                    )
                else:
                    continue

                periods = response.periods
                if not periods:
                    logger.warning(f"No {stmt_type} data available for {ticker}")
                    for field in key_fields:
                        result_df[f'{prefix}_{field}'] = np.nan
                        result_df[f'{prefix}_{field}_days_old'] = np.nan
                    continue

                # Sort periods by date (oldest first for processing)
                periods_sorted = sorted(
                    periods,
                    key=lambda p: p.get('fiscal_date', ''),
                    reverse=False
                )

                logger.info(f"Fetched {len(periods_sorted)} {stmt_type} periods for {ticker}")

                # Build a timeline of values for each field
                for field in key_fields:
                    col_name = f'{prefix}_{field}'
                    col_days = f'{prefix}_{field}_days_old'
                    col_qoq = f'{prefix}_{field}_qoq_change'
                    col_yoy = f'{prefix}_{field}_yoy_change'

                    # Create period lookup with values and changes
                    # Earnings are typically announced 30-45 days after fiscal quarter end
                    # Use 45 days delay for point-in-time correctness
                    EARNINGS_ANNOUNCEMENT_DELAY_DAYS = 45

                    period_data = []
                    for i, period in enumerate(periods_sorted):
                        fiscal_date_str = period.get('fiscal_date')
                        if not fiscal_date_str:
                            continue

                        try:
                            fiscal_date = pd.to_datetime(fiscal_date_str)
                            if hasattr(fiscal_date, 'tzinfo') and fiscal_date.tzinfo is not None:
                                fiscal_date = fiscal_date.replace(tzinfo=None)
                            # Estimate announcement date (when data becomes available)
                            available_date = fiscal_date + pd.Timedelta(days=EARNINGS_ANNOUNCEMENT_DELAY_DAYS)
                        except:
                            continue

                        value = period.get(field)
                        if value is None:
                            continue

                        # Calculate QoQ change (previous quarter)
                        qoq_change = None
                        if i > 0:
                            prev_val = periods_sorted[i-1].get(field)
                            if prev_val and prev_val != 0:
                                qoq_change = (value - prev_val) / abs(prev_val)

                        # Calculate YoY change (4 quarters back)
                        yoy_change = None
                        if i >= 4:
                            prev_year_val = periods_sorted[i-4].get(field)
                            if prev_year_val and prev_year_val != 0:
                                yoy_change = (value - prev_year_val) / abs(prev_year_val)

                        period_data.append({
                            'date': available_date,  # Use estimated announcement date for point-in-time
                            'fiscal_date': fiscal_date,
                            'value': value,
                            'qoq_change': qoq_change,
                            'yoy_change': yoy_change
                        })

                    if not period_data:
                        result_df[col_name] = np.nan
                        result_df[col_days] = np.nan
                        result_df[col_qoq] = np.nan
                        result_df[col_yoy] = np.nan
                        continue

                    # Sort by date
                    period_data.sort(key=lambda x: x['date'])

                    # Find first available QoQ and YoY values for warmup
                    first_qoq = next((p['qoq_change'] for p in period_data if p['qoq_change'] is not None), 0.0)
                    first_yoy = next((p['yoy_change'] for p in period_data if p['yoy_change'] is not None), 0.0)

                    # For each row, find the most recent period before or on that date
                    def get_period_for_date(row_date):
                        if hasattr(row_date, 'tzinfo') and row_date.tzinfo is not None:
                            row_date = row_date.replace(tzinfo=None)

                        best = None
                        for pd_item in period_data:
                            if pd_item['date'] <= row_date:
                                best = pd_item
                            else:
                                break
                        return best

                    values = []
                    days_old = []
                    qoq_changes = []
                    yoy_changes = []

                    for row_date in result_df['Date']:
                        period_info = get_period_for_date(row_date)
                        if period_info:
                            values.append(period_info['value'])
                            # days_old = days since fiscal quarter end (not announcement)
                            days = (row_date - period_info['fiscal_date']).days
                            days_old.append(max(0, days))
                            # Use first available change values if current is None
                            qoq_changes.append(period_info['qoq_change'] if period_info['qoq_change'] is not None else first_qoq)
                            yoy_changes.append(period_info['yoy_change'] if period_info['yoy_change'] is not None else first_yoy)
                        else:
                            # Warmup: use earliest known value, calculate days backward
                            earliest = period_data[0]
                            values.append(earliest['value'])
                            days = (earliest['date'] - row_date).days
                            days_old.append(max(0, days))  # How many days until this data exists
                            # Use first available change values for warmup
                            qoq_changes.append(first_qoq)
                            yoy_changes.append(first_yoy)

                    result_df[col_name] = values
                    result_df[col_days] = days_old
                    result_df[col_qoq] = qoq_changes
                    result_df[col_yoy] = yoy_changes

            except Exception as e:
                logger.error(f"Error processing {stmt_type}: {e}", exc_info=True)
                for field in key_fields:
                    result_df[f'{prefix}_{field}'] = np.nan
                    result_df[f'{prefix}_{field}_days_old'] = np.nan

        logger.info(f"Created v2 statement features for {ticker}: {valid_statement_types}")
        return result_df
