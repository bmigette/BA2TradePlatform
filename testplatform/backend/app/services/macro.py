"""
Macro Economics Service

Integrates macroeconomic series (interest rates, inflation, unemployment, VIX, yields) with OHLC
datasets for ML model training.

Dataset features read the FIRST-RELEASE FRED cache of ``ba2_providers.macro.fred_series``: a value is
visible to a row from the day FRED first published it (its ``realtime_start``), at its first-published
number, never the revised one. That is the same rule the backtests use, so a trained model and its
backtest see the same macro values.
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
    #
    # Dataset features are read from the first-release cache (ba2_providers.macro.fred_series), which
    # holds BAA10Y, CPIAUCSL, DGS10, DGS3MO, FEDFUNDS, PAYEMS, T10Y3M, UNRATE and VIXCLS. GDP and DGS2
    # have NO cache file, so asking for 'gdp' or 'yield_2y' is refused (_require_cached_series), not
    # served from live revised FRED data.
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

    def _require_cached_series(self, indicator: str) -> str:
        """The FRED series id behind *indicator*, refused when the first-release cache has no file for it."""
        from ba2_providers.macro import fred_series as fs

        if indicator not in self.MACRO_INDICATORS:
            raise ValueError(f"Unknown macro indicator {indicator!r}; supported: {sorted(self.MACRO_INDICATORS)}")
        sid = self.MACRO_INDICATORS[indicator]['series']
        if sid not in fs.SERIES_SPEC:
            raise ValueError(
                f"Macro indicator {indicator!r} (FRED {sid}) has no first-release cache file "
                f"({fs.cache_path(sid)}), so its values cannot be placed point-in-time. "
                f"The cache holds {sorted(fs.SERIES_SPEC)}; deselect {indicator!r}."
            )
        return sid

    def _first_release_columns(self, dates: pd.Series, indicator: str) -> pd.DataFrame:
        """Per-row ``indicator``, ``_yoy_change`` and ``_days_since`` from the first-release cache.

        Row ``t`` gets the latest observation ``fred_series.get_series_as_of(series, t)`` shows: the
        first-published value of every observation FRED had released before ``t``'s decision label
        (see ``fred_series.decision_label``). Rows that share a label share one cache read, so an
        intraday dataset costs one read per day, not per bar. YoY is the change against the
        observation ``yoy_periods`` earlier in that same visible series; ``days_since`` counts from
        the OBSERVATION date of the value in force (the release date is not exposed by the reader).
        A series without a cache file, or a row outside the cache's coverage, raises
        (``MacroAvailabilityUnknown``, naming the file).
        """
        from ba2_providers.fmp_common import frozen_ttl_cache
        from ba2_providers.macro import fred_series as fs

        sid = self._require_cached_series(indicator)
        yoy_periods = self.MACRO_INDICATORS[indicator]['yoy_periods']

        stamps = pd.Index(dates.unique())
        label_of = {t: fs.decision_label(t) for t in stamps}
        per_label = {}
        for t in stamps:
            label = label_of[t]
            if label in per_label:
                continue
            # Frozen: read the synced cache file only. A missing or stale file refuses (naming the
            # file) instead of fetching mid-build, so a dataset never depends on FRED being up.
            with frozen_ttl_cache():
                visible = fs.get_series_as_of(sid, t).dropna()
            # An observation revised after its first release can appear once per vintage, oldest
            # first; the latest vintage visible at this label is the one in force.
            visible = visible[~visible.index.duplicated(keep='last')]
            if visible.empty:
                per_label[label] = (np.nan, pd.NaT, np.nan)
                continue
            yoy = np.nan
            if len(visible) > yoy_periods:
                yoy = (visible.iloc[-1] / visible.iloc[-1 - yoy_periods] - 1) * 100
            per_label[label] = (float(visible.iloc[-1]), visible.index[-1], yoy)

        rows = [per_label[label_of[t]] for t in dates]
        out = pd.DataFrame(rows, columns=[indicator, 'observation_date', f'{indicator}_yoy_change'], index=dates.index)
        out[f'{indicator}_days_since'] = (dates - pd.to_datetime(out['observation_date'])).dt.days
        return out.drop(columns=['observation_date'])

    def __init__(self, api_key: str = None):
        """
        Initialize MacroService.

        Args:
            api_key: FRED API key for the LIVE viewer fetch (``get_macro_data``) only. None -> the
                ONE shared resolver (``ba2_common.core.fred_api_key``: env ``FRED_API_KEY``, else
                the AppSetting ``fred_api_key``), resolved on first use. Dataset features read the
                local first-release cache and need no key.
        """
        self._api_key = api_key

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

    @property
    def api_key(self) -> str:
        """The FRED key for a live fetch. Raises ``FredApiKeyMissing`` when none is configured.

        (A missing key used to log a warning and return empty frames, which became all-NaN macro
        columns in the dataset.)
        """
        if not self._api_key:
            from ba2_common.core.fred_api_key import require_fred_api_key
            self._api_key = require_fred_api_key("the live macro viewer (MacroService.get_macro_data)")
        return self._api_key

    def _fetch_fred_data(
        self,
        series_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> pd.DataFrame:
        """
        Fetch data from the live FRED API: LATEST (revised) values by observation date. For the
        macro viewer only -- dataset features use the first-release cache (_first_release_columns).

        Args:
            series_id: FRED series ID
            start_date: Start date
            end_date: End date

        Returns:
            DataFrame with Date and value columns
        """
        # A failed request RAISES. It used to be logged and turned into an empty frame, which
        # the dataset builders then filled with NaN -- a silent gap in a training set.
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
        Integrate macro data with OHLC dataset: each row carries the latest first-release
        observation FRED had published at that row's decision (see ``_first_release_columns``).
        Macro data is released less often than OHLC bars, so the last known value is held until
        the next release.

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

        result_df = result_df.sort_values('Date').reset_index(drop=True)

        for indicator in indicators:
            cols = self._first_release_columns(result_df['Date'], indicator)
            for col in cols.columns:
                result_df[col] = cols[col]
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

        # Fetch 2Y and 10Y yields from the first-release cache (DGS2 has no cache file: refused)
        for indicator in ('yield_2y', 'yield_10y'):
            result_df[indicator] = self._first_release_columns(result_df['Date'], indicator)[indicator]

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
