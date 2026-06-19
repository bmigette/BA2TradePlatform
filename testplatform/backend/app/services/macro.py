"""
Macro Economics Service

Fetches macroeconomic data (interest rates, GDP, inflation) and integrates
it with OHLC datasets using forward-fill for ML model training.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging
import os
import requests

logger = logging.getLogger(__name__)


class MacroService:
    """
    Service for fetching and processing macroeconomic data.

    Supports data from FRED (Federal Reserve Economic Data) and integrates
    macro indicators with OHLC datasets using forward-fill alignment.
    """

    # FRED API endpoint
    FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

    # Economic indicator series IDs from FRED
    # 'yoy_periods' indicates how many observations make 1 year for YoY calculation
    MACRO_INDICATORS = {
        'interest_rate': {
            'series': 'FEDFUNDS',
            'name': 'Federal Funds Rate',
            'description': 'Federal Reserve target interest rate',
            'unit': '%',
            'yoy_periods': 12  # Monthly data
        },
        'gdp': {
            'series': 'GDP',
            'name': 'Gross Domestic Product',
            'description': 'US GDP in billions of dollars',
            'unit': 'Billions USD',
            'yoy_periods': 4  # Quarterly data
        },
        'inflation': {
            'series': 'CPIAUCSL',
            'name': 'Consumer Price Index',
            'description': 'CPI for all urban consumers',
            'unit': 'Index',
            'yoy_periods': 12  # Monthly data
        },
        'unemployment': {
            'series': 'UNRATE',
            'name': 'Unemployment Rate',
            'description': 'Percentage of labor force unemployed',
            'unit': '%',
            'yoy_periods': 12  # Monthly data
        },
        'vix': {
            'series': 'VIXCLS',
            'name': 'VIX Volatility Index',
            'description': 'Market volatility indicator',
            'unit': 'Index',
            'yoy_periods': 252  # Daily data
        },
        'yield_10y': {
            'series': 'DGS10',
            'name': '10-Year Treasury Yield',
            'description': 'Constant maturity treasury rate',
            'unit': '%',
            'yoy_periods': 252  # Daily data
        },
        'yield_2y': {
            'series': 'DGS2',
            'name': '2-Year Treasury Yield',
            'description': 'Constant maturity treasury rate',
            'unit': '%',
            'yoy_periods': 252  # Daily data
        }
    }

    def __init__(self, api_key: str = None):
        """
        Initialize MacroService.

        Args:
            api_key: FRED API key (if None, uses FRED_API_KEY env var)
        """
        self.api_key = api_key or os.getenv('FRED_API_KEY')
        if not self.api_key:
            logger.warning("FRED API key not set. Macro data will use fallback values.")

        # Secondary re-source seam (Phase 5, Task 6), parallel to the OHLCV seam:
        # when FEATURES_SOURCE=ba2_providers is explicitly selected, macro series are
        # intended to be sourced through ba2_providers' shared cache (category
        # "macro"; name "fred"). DEFAULT is legacy (direct FRED HTTP below) so nothing
        # changes; verification is DEFERRED to plan Task 8 (do NOT flip the default
        # until macro_* column equivalence is documented). We resolve the provider
        # here so a flag/config error surfaces at construction; the legacy
        # _fetch_fred_data path remains the actual fetch this phase.
        self._ba2_provider = None
        try:
            from app.services.features_source import use_ba2_providers, get_ba2_provider

            if use_ba2_providers():
                self._ba2_provider = get_ba2_provider("macro", "fred")
                if self._ba2_provider is not None:
                    logger.info(
                        "FEATURES_SOURCE=ba2_providers: macro 'fred' available "
                        "(verification deferred to Task 8; using legacy FRED fetch this phase)"
                    )
                else:
                    logger.warning(
                        "FEATURES_SOURCE=ba2_providers: macro 'fred' unavailable; "
                        "using legacy FRED fetch"
                    )
        except Exception as e:  # never let the seam break construction
            logger.warning(f"FEATURES_SOURCE macro seam init skipped: {e}")
            self._ba2_provider = None

    def _fetch_fred_data(
        self,
        series_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> pd.DataFrame:
        """
        Fetch data from FRED API.

        Args:
            series_id: FRED series ID
            start_date: Start date
            end_date: End date

        Returns:
            DataFrame with Date and value columns
        """
        if not self.api_key:
            logger.warning(f"No FRED API key, returning empty data for {series_id}")
            return pd.DataFrame(columns=['Date', 'value'])

        try:
            params = {
                'series_id': series_id,
                'api_key': self.api_key,
                'file_type': 'json',
                'observation_start': start_date.strftime('%Y-%m-%d'),
                'observation_end': end_date.strftime('%Y-%m-%d'),
                'sort_order': 'asc'
            }

            response = requests.get(self.FRED_API_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            observations = data.get('observations', [])
            if not observations:
                return pd.DataFrame(columns=['Date', 'value'])

            df = pd.DataFrame(observations)
            df['Date'] = pd.to_datetime(df['date'])
            df['value'] = pd.to_numeric(df['value'], errors='coerce')
            df = df[['Date', 'value']].dropna()

            logger.info(f"Fetched {len(df)} observations for {series_id}")
            return df

        except Exception as e:
            logger.error(f"Error fetching FRED data for {series_id}: {e}")
            return pd.DataFrame(columns=['Date', 'value'])

    def get_macro_data(
        self,
        indicators: List[str] = None,
        start_date: datetime = None,
        end_date: datetime = None
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch macro economic data for specified indicators.

        Args:
            indicators: List of indicator names (default: all)
            start_date: Start date (default: 2 years ago)
            end_date: End date (default: today)

        Returns:
            Dictionary mapping indicator names to DataFrames
        """
        if indicators is None:
            indicators = list(self.MACRO_INDICATORS.keys())

        if end_date is None:
            end_date = datetime.now()
        if start_date is None:
            start_date = end_date - timedelta(days=730)  # 2 years

        result = {}

        for indicator in indicators:
            if indicator not in self.MACRO_INDICATORS:
                logger.warning(f"Unknown indicator: {indicator}")
                continue

            config = self.MACRO_INDICATORS[indicator]
            df = self._fetch_fred_data(config['series'], start_date, end_date)
            df = df.rename(columns={'value': indicator})
            result[indicator] = df

            logger.info(f"Fetched {indicator}: {len(df)} observations")

        return result

    def integrate_macro_with_ohlc(
        self,
        ohlc_df: pd.DataFrame,
        indicators: List[str] = None
    ) -> pd.DataFrame:
        """
        Integrate macro data with OHLC dataset using forward-fill.

        Macro data is typically released less frequently than OHLC data
        (monthly vs daily). This method aligns macro data to the OHLC
        dates using forward-fill to propagate the last known value.

        Args:
            ohlc_df: DataFrame with Date column and OHLC data
            indicators: List of indicators to include (default: all)

        Returns:
            DataFrame with OHLC data and macro columns added
        """
        if indicators is None:
            indicators = list(self.MACRO_INDICATORS.keys())

        result_df = ohlc_df.copy()
        result_df['Date'] = pd.to_datetime(result_df['Date'])

        # Normalize dates to timezone-naive for proper merging
        # FRED data is timezone-naive, OHLC data may be timezone-aware (UTC)
        if result_df['Date'].dt.tz is not None:
            result_df['Date'] = result_df['Date'].dt.tz_localize(None)

        # Get date range from OHLC data (convert to naive datetime for API call)
        start_date = result_df['Date'].min()
        end_date = result_df['Date'].max()
        if hasattr(start_date, 'to_pydatetime'):
            start_date = start_date.to_pydatetime()
        if hasattr(end_date, 'to_pydatetime'):
            end_date = end_date.to_pydatetime()

        # Fetch macro data with 13 months of additional history for YoY calculation
        # (12 months lookback + 1 month buffer for quarterly data alignment)
        macro_start_date = start_date - timedelta(days=400)
        macro_data = self.get_macro_data(indicators, macro_start_date, end_date)

        # Merge each indicator using merge_asof for proper time-based alignment
        # This handles the case where FRED releases data monthly (e.g., 1st of month)
        # and OHLC data is daily/intraday
        result_df = result_df.sort_values('Date').reset_index(drop=True)

        for indicator, macro_df in macro_data.items():
            if macro_df.empty:
                result_df[indicator] = np.nan
                logger.warning(f"No macro data for {indicator}, filling with NaN")
                continue

            # Ensure macro dates are also timezone-naive
            macro_df = macro_df.copy()
            macro_df['Date'] = pd.to_datetime(macro_df['Date'])
            if macro_df['Date'].dt.tz is not None:
                macro_df['Date'] = macro_df['Date'].dt.tz_localize(None)

            # Sort macro data for merge_asof
            macro_df = macro_df.sort_values('Date').reset_index(drop=True)

            # Calculate YoY change on raw macro data BEFORE merging
            # Use frequency-specific periods (12 for monthly, 4 for quarterly, 252 for daily)
            yoy_col = f'{indicator}_yoy_change'
            yoy_periods = self.MACRO_INDICATORS.get(indicator, {}).get('yoy_periods', 12)
            macro_df[yoy_col] = macro_df[indicator].pct_change(periods=yoy_periods, fill_method=None) * 100

            # Add a column for the macro report date to calculate days_since
            macro_date_col = f'{indicator}_report_date'
            macro_df[macro_date_col] = macro_df['Date']

            # Use merge_asof to get the most recent macro value for each OHLC row
            # This is the correct way to align less-frequent data with more-frequent data
            result_df = pd.merge_asof(
                result_df,
                macro_df[['Date', indicator, yoy_col, macro_date_col]],
                on='Date',
                direction='backward'  # Get the most recent macro value at or before each OHLC date
            )

            # Calculate days since last macro report
            days_since_col = f'{indicator}_days_since'
            result_df[days_since_col] = (result_df['Date'] - result_df[macro_date_col]).dt.days

            # Drop the report date column (keep only days_since)
            result_df = result_df.drop(columns=[macro_date_col])

            logger.debug(f"Merged {indicator}: {result_df[indicator].notna().sum()} non-null values")

        logger.info(f"Integrated {len(indicators)} macro indicators with OHLC data")
        return result_df

    def create_yield_curve_features(self, ohlc_df: pd.DataFrame) -> pd.DataFrame:
        """
        Create yield curve features (spread, inversion indicator).

        Args:
            ohlc_df: DataFrame with Date column

        Returns:
            DataFrame with yield curve features added
        """
        result_df = ohlc_df.copy()
        result_df['Date'] = pd.to_datetime(result_df['Date'])

        # Normalize dates to timezone-naive for proper merging
        if result_df['Date'].dt.tz is not None:
            result_df['Date'] = result_df['Date'].dt.tz_localize(None)

        # Sort for merge_asof
        result_df = result_df.sort_values('Date').reset_index(drop=True)

        start_date = result_df['Date'].min()
        end_date = result_df['Date'].max()
        if hasattr(start_date, 'to_pydatetime'):
            start_date = start_date.to_pydatetime()
        if hasattr(end_date, 'to_pydatetime'):
            end_date = end_date.to_pydatetime()

        # Fetch 2Y and 10Y yields
        yield_data = self.get_macro_data(['yield_2y', 'yield_10y'], start_date, end_date)

        # Merge yields using merge_asof for proper time alignment
        for indicator, macro_df in yield_data.items():
            if not macro_df.empty:
                macro_df = macro_df.copy()
                macro_df['Date'] = pd.to_datetime(macro_df['Date'])
                if macro_df['Date'].dt.tz is not None:
                    macro_df['Date'] = macro_df['Date'].dt.tz_localize(None)
                macro_df = macro_df.sort_values('Date').reset_index(drop=True)

                result_df = pd.merge_asof(
                    result_df,
                    macro_df[['Date', indicator]],
                    on='Date',
                    direction='backward'
                )

        # Calculate yield curve spread (10Y - 2Y)
        if 'yield_10y' in result_df.columns and 'yield_2y' in result_df.columns:
            result_df['yield_spread'] = result_df['yield_10y'] - result_df['yield_2y']
            # Inversion indicator (1 if inverted, 0 otherwise)
            result_df['yield_inverted'] = (result_df['yield_spread'] < 0).astype(int)

        logger.info("Created yield curve features")
        return result_df

    @staticmethod
    def get_supported_indicators() -> Dict[str, Dict]:
        """
        Get list of supported macro indicators with metadata.

        Returns:
            Dictionary of indicator configurations
        """
        return MacroService.MACRO_INDICATORS.copy()
