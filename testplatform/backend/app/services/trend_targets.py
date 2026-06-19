"""
Trend-Based Target Service

Calculates trend-based prediction targets as an alternative to price percentage targets.
Trend-based targets often provide more balanced class distributions.

Supported trend types:
- Uptrend: Higher highs and higher lows over N periods
- Downtrend: Lower highs and lower lows over N periods
- Sideways/Consolidation: Price range bound within X% for N periods
- Breakout: Price moves beyond recent range by X%
- Reversal: Trend direction change detected

Detection methods:
- moving_average: SMA crossovers (fast > slow = uptrend)
- linear_regression: Slope of linear regression over N periods
- adx: Average Directional Index for trend strength
- pivot_points: Swing high/low analysis
- donchian: Donchian channel breakout detection
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional, Literal
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class TrendType(str, Enum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    SIDEWAYS = "sideways"
    BREAKOUT_UP = "breakout_up"
    BREAKOUT_DOWN = "breakout_down"


class TrendMethod(str, Enum):
    MOVING_AVERAGE = "moving_average"
    LINEAR_REGRESSION = "linear_regression"
    ADX = "adx"
    PIVOT_POINTS = "pivot_points"
    DONCHIAN = "donchian"


class TrendTargetService:
    """
    Service for calculating trend-based prediction targets.

    Provides reusable methods for:
    - Detecting current trends in price data
    - Creating binary or multi-class trend targets
    - Calculating trend strength metrics
    - Preparing trend features for ML training
    """

    def __init__(self):
        pass

    def calculate_trend_targets(
        self,
        df: pd.DataFrame,
        method: str = "moving_average",
        lookback_period: int = 20,
        prediction_horizon: int = 5,
        trend_strength_threshold: float = 0.0,
        consolidation_range_pct: float = 3.0,
        fast_period: int = 10,
        slow_period: int = 30,
        include_strength: bool = True
    ) -> pd.DataFrame:
        """
        Calculate trend-based targets for the dataframe.

        Args:
            df: DataFrame with OHLC data (must have Date, Open, High, Low, Close columns)
            method: Detection method (moving_average, linear_regression, adx, pivot_points, donchian)
            lookback_period: Period for trend detection
            prediction_horizon: How many periods ahead to predict the trend
            trend_strength_threshold: Minimum strength to classify as trend (vs sideways)
            consolidation_range_pct: Range percentage to classify as consolidation
            fast_period: Fast MA period (for moving_average method)
            slow_period: Slow MA period (for moving_average method)
            include_strength: Whether to include trend strength as additional feature

        Returns:
            DataFrame with trend target columns added
        """
        result_df = df.copy()

        # Ensure Date column is datetime
        if 'Date' in result_df.columns:
            result_df['Date'] = pd.to_datetime(result_df['Date'])

        # Calculate trend based on method
        if method == TrendMethod.MOVING_AVERAGE.value:
            result_df = self._detect_trend_ma(result_df, fast_period, slow_period)
        elif method == TrendMethod.LINEAR_REGRESSION.value:
            result_df = self._detect_trend_linreg(result_df, lookback_period)
        elif method == TrendMethod.ADX.value:
            result_df = self._detect_trend_adx(result_df, lookback_period, trend_strength_threshold)
        elif method == TrendMethod.PIVOT_POINTS.value:
            result_df = self._detect_trend_pivots(result_df, lookback_period)
        elif method == TrendMethod.DONCHIAN.value:
            result_df = self._detect_trend_donchian(result_df, lookback_period, consolidation_range_pct)
        else:
            logger.warning(f"Unknown trend method: {method}, using moving_average")
            result_df = self._detect_trend_ma(result_df, fast_period, slow_period)

        # Create future trend targets (what trend will exist N periods ahead)
        if prediction_horizon > 0:
            result_df['trend_target'] = result_df['trend_current'].shift(-prediction_horizon)
            result_df['trend_target_strength'] = result_df['trend_strength'].shift(-prediction_horizon)
        else:
            result_df['trend_target'] = result_df['trend_current']
            result_df['trend_target_strength'] = result_df['trend_strength']

        # Create binary targets for each trend type
        result_df['trend_is_uptrend'] = (result_df['trend_target'] == TrendType.UPTREND.value).astype(int)
        result_df['trend_is_downtrend'] = (result_df['trend_target'] == TrendType.DOWNTREND.value).astype(int)
        result_df['trend_is_sideways'] = (result_df['trend_target'] == TrendType.SIDEWAYS.value).astype(int)

        # Multi-class target (0=sideways, 1=uptrend, 2=downtrend)
        trend_map = {
            TrendType.SIDEWAYS.value: 0,
            TrendType.UPTREND.value: 1,
            TrendType.DOWNTREND.value: 2,
            TrendType.BREAKOUT_UP.value: 1,
            TrendType.BREAKOUT_DOWN.value: 2
        }
        result_df['trend_class'] = result_df['trend_target'].map(trend_map).fillna(0).astype(int)

        if not include_strength:
            result_df = result_df.drop(columns=['trend_strength', 'trend_target_strength'], errors='ignore')

        logger.info(f"Calculated trend targets using {method} method")
        self._log_trend_distribution(result_df)

        return result_df

    def _detect_trend_ma(
        self,
        df: pd.DataFrame,
        fast_period: int = 10,
        slow_period: int = 30
    ) -> pd.DataFrame:
        """Detect trends using moving average crossover."""
        result_df = df.copy()

        # Calculate SMAs
        result_df['_sma_fast'] = result_df['Close'].rolling(window=fast_period).mean()
        result_df['_sma_slow'] = result_df['Close'].rolling(window=slow_period).mean()

        # Calculate trend strength as percentage difference
        result_df['trend_strength'] = (
            (result_df['_sma_fast'] - result_df['_sma_slow']) / result_df['_sma_slow'] * 100
        ).abs()

        # Classify trend
        def classify_ma_trend(row):
            if pd.isna(row['_sma_fast']) or pd.isna(row['_sma_slow']):
                return TrendType.SIDEWAYS.value

            diff_pct = (row['_sma_fast'] - row['_sma_slow']) / row['_sma_slow'] * 100

            if diff_pct > 1.0:  # Fast MA above slow by > 1%
                return TrendType.UPTREND.value
            elif diff_pct < -1.0:  # Fast MA below slow by > 1%
                return TrendType.DOWNTREND.value
            else:
                return TrendType.SIDEWAYS.value

        result_df['trend_current'] = result_df.apply(classify_ma_trend, axis=1)

        # Cleanup temp columns
        result_df = result_df.drop(columns=['_sma_fast', '_sma_slow'])

        return result_df

    def _detect_trend_linreg(
        self,
        df: pd.DataFrame,
        lookback_period: int = 20
    ) -> pd.DataFrame:
        """Detect trends using linear regression slope."""
        result_df = df.copy()

        def calc_slope(series):
            if len(series) < 2 or series.isna().all():
                return 0.0
            y = series.values
            x = np.arange(len(y))

            # Remove NaN values
            mask = ~np.isnan(y)
            if mask.sum() < 2:
                return 0.0

            x_clean = x[mask]
            y_clean = y[mask]

            # Calculate slope using least squares
            n = len(x_clean)
            slope = (n * np.sum(x_clean * y_clean) - np.sum(x_clean) * np.sum(y_clean)) / \
                    (n * np.sum(x_clean ** 2) - np.sum(x_clean) ** 2)

            # Normalize by price level
            return slope / np.mean(y_clean) * 100

        # Calculate rolling slope
        result_df['_slope'] = result_df['Close'].rolling(window=lookback_period).apply(calc_slope, raw=False)

        # Trend strength is absolute slope
        result_df['trend_strength'] = result_df['_slope'].abs()

        # Classify based on slope
        def classify_slope_trend(slope):
            if pd.isna(slope):
                return TrendType.SIDEWAYS.value
            if slope > 0.1:  # Positive slope
                return TrendType.UPTREND.value
            elif slope < -0.1:  # Negative slope
                return TrendType.DOWNTREND.value
            else:
                return TrendType.SIDEWAYS.value

        result_df['trend_current'] = result_df['_slope'].apply(classify_slope_trend)
        result_df = result_df.drop(columns=['_slope'])

        return result_df

    def _detect_trend_adx(
        self,
        df: pd.DataFrame,
        period: int = 14,
        threshold: float = 25.0
    ) -> pd.DataFrame:
        """Detect trends using Average Directional Index (ADX)."""
        result_df = df.copy()

        # Calculate True Range
        result_df['_tr'] = np.maximum(
            result_df['High'] - result_df['Low'],
            np.maximum(
                abs(result_df['High'] - result_df['Close'].shift(1)),
                abs(result_df['Low'] - result_df['Close'].shift(1))
            )
        )

        # Calculate +DM and -DM
        result_df['_plus_dm'] = np.where(
            (result_df['High'] - result_df['High'].shift(1)) > (result_df['Low'].shift(1) - result_df['Low']),
            np.maximum(result_df['High'] - result_df['High'].shift(1), 0),
            0
        )
        result_df['_minus_dm'] = np.where(
            (result_df['Low'].shift(1) - result_df['Low']) > (result_df['High'] - result_df['High'].shift(1)),
            np.maximum(result_df['Low'].shift(1) - result_df['Low'], 0),
            0
        )

        # Smooth with EMA
        result_df['_atr'] = result_df['_tr'].ewm(span=period, adjust=False).mean()
        result_df['_plus_di'] = 100 * (result_df['_plus_dm'].ewm(span=period, adjust=False).mean() / result_df['_atr'])
        result_df['_minus_di'] = 100 * (result_df['_minus_dm'].ewm(span=period, adjust=False).mean() / result_df['_atr'])

        # Calculate DX and ADX
        result_df['_dx'] = 100 * abs(result_df['_plus_di'] - result_df['_minus_di']) / (result_df['_plus_di'] + result_df['_minus_di'])
        result_df['_adx'] = result_df['_dx'].ewm(span=period, adjust=False).mean()

        # Trend strength is ADX value
        result_df['trend_strength'] = result_df['_adx']

        # Classify trend
        def classify_adx_trend(row):
            if pd.isna(row['_adx']):
                return TrendType.SIDEWAYS.value

            if row['_adx'] < threshold:
                return TrendType.SIDEWAYS.value
            elif row['_plus_di'] > row['_minus_di']:
                return TrendType.UPTREND.value
            else:
                return TrendType.DOWNTREND.value

        result_df['trend_current'] = result_df.apply(classify_adx_trend, axis=1)

        # Cleanup
        temp_cols = ['_tr', '_plus_dm', '_minus_dm', '_atr', '_plus_di', '_minus_di', '_dx', '_adx']
        result_df = result_df.drop(columns=temp_cols)

        return result_df

    def _detect_trend_pivots(
        self,
        df: pd.DataFrame,
        lookback_period: int = 10
    ) -> pd.DataFrame:
        """Detect trends using pivot point (swing high/low) analysis."""
        result_df = df.copy()

        # Find swing highs and lows
        result_df['_swing_high'] = result_df['High'].rolling(window=lookback_period, center=True).max()
        result_df['_swing_low'] = result_df['Low'].rolling(window=lookback_period, center=True).min()

        # Check for higher highs/lows or lower highs/lows
        result_df['_hh'] = result_df['_swing_high'] > result_df['_swing_high'].shift(lookback_period)
        result_df['_hl'] = result_df['_swing_low'] > result_df['_swing_low'].shift(lookback_period)
        result_df['_lh'] = result_df['_swing_high'] < result_df['_swing_high'].shift(lookback_period)
        result_df['_ll'] = result_df['_swing_low'] < result_df['_swing_low'].shift(lookback_period)

        # Trend strength based on price range
        result_df['trend_strength'] = (
            (result_df['_swing_high'] - result_df['_swing_low']) / result_df['Close'] * 100
        )

        # Classify trend
        def classify_pivot_trend(row):
            if row['_hh'] and row['_hl']:
                return TrendType.UPTREND.value
            elif row['_lh'] and row['_ll']:
                return TrendType.DOWNTREND.value
            else:
                return TrendType.SIDEWAYS.value

        result_df['trend_current'] = result_df.apply(classify_pivot_trend, axis=1)

        # Cleanup
        temp_cols = ['_swing_high', '_swing_low', '_hh', '_hl', '_lh', '_ll']
        result_df = result_df.drop(columns=temp_cols)

        return result_df

    def _detect_trend_donchian(
        self,
        df: pd.DataFrame,
        period: int = 20,
        consolidation_range_pct: float = 3.0
    ) -> pd.DataFrame:
        """Detect trends using Donchian channel breakouts."""
        result_df = df.copy()

        # Calculate Donchian channels
        result_df['_dc_high'] = result_df['High'].rolling(window=period).max()
        result_df['_dc_low'] = result_df['Low'].rolling(window=period).min()
        result_df['_dc_mid'] = (result_df['_dc_high'] + result_df['_dc_low']) / 2

        # Channel width as percentage
        result_df['_dc_width_pct'] = (result_df['_dc_high'] - result_df['_dc_low']) / result_df['_dc_mid'] * 100

        # Trend strength is channel position
        result_df['trend_strength'] = abs(result_df['Close'] - result_df['_dc_mid']) / (result_df['_dc_high'] - result_df['_dc_low']) * 100

        # Classify trend
        def classify_donchian_trend(row):
            if pd.isna(row['_dc_high']) or pd.isna(row['_dc_low']):
                return TrendType.SIDEWAYS.value

            # Narrow channel = consolidation
            if row['_dc_width_pct'] < consolidation_range_pct:
                return TrendType.SIDEWAYS.value

            # Position in channel
            if row['Close'] >= row['_dc_high']:
                return TrendType.BREAKOUT_UP.value
            elif row['Close'] <= row['_dc_low']:
                return TrendType.BREAKOUT_DOWN.value
            elif row['Close'] > row['_dc_mid']:
                return TrendType.UPTREND.value
            elif row['Close'] < row['_dc_mid']:
                return TrendType.DOWNTREND.value
            else:
                return TrendType.SIDEWAYS.value

        result_df['trend_current'] = result_df.apply(classify_donchian_trend, axis=1)

        # Cleanup
        temp_cols = ['_dc_high', '_dc_low', '_dc_mid', '_dc_width_pct']
        result_df = result_df.drop(columns=temp_cols)

        return result_df

    def _log_trend_distribution(self, df: pd.DataFrame):
        """Log the distribution of trend targets."""
        if 'trend_target' not in df.columns:
            return

        counts = df['trend_target'].value_counts()
        total = len(df.dropna(subset=['trend_target']))

        logger.info("Trend target distribution:")
        for trend_type, count in counts.items():
            pct = count / total * 100 if total > 0 else 0
            logger.info(f"  {trend_type}: {count} ({pct:.1f}%)")

    def get_trend_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Get statistics about trend distribution in the dataframe.

        Returns:
            Dictionary with trend statistics
        """
        stats = {
            'total_rows': len(df),
            'trends': {}
        }

        if 'trend_target' in df.columns:
            target_col = 'trend_target'
        elif 'trend_current' in df.columns:
            target_col = 'trend_current'
        else:
            return stats

        counts = df[target_col].value_counts()
        total = len(df.dropna(subset=[target_col]))

        for trend_type in [TrendType.UPTREND.value, TrendType.DOWNTREND.value,
                          TrendType.SIDEWAYS.value, TrendType.BREAKOUT_UP.value,
                          TrendType.BREAKOUT_DOWN.value]:
            count = counts.get(trend_type, 0)
            stats['trends'][trend_type] = {
                'count': int(count),
                'percentage': round(count / total * 100, 2) if total > 0 else 0
            }

        # Add strength stats if available
        if 'trend_strength' in df.columns:
            strength = df['trend_strength'].dropna()
            stats['strength'] = {
                'mean': round(float(strength.mean()), 2) if len(strength) > 0 else 0,
                'min': round(float(strength.min()), 2) if len(strength) > 0 else 0,
                'max': round(float(strength.max()), 2) if len(strength) > 0 else 0
            }

        return stats

    def get_trend_visualization_data(
        self,
        df: pd.DataFrame,
        date_column: str = 'Date'
    ) -> List[Dict[str, Any]]:
        """
        Get trend data formatted for frontend visualization.

        Returns:
            List of dicts with date, trend type, and strength
        """
        if 'trend_current' not in df.columns:
            return []

        result = []
        for _, row in df.iterrows():
            if pd.isna(row.get('trend_current')):
                continue

            entry = {
                'date': row[date_column].isoformat() if hasattr(row[date_column], 'isoformat') else str(row[date_column]),
                'trend': row['trend_current'],
                'strength': float(row.get('trend_strength', 0)) if not pd.isna(row.get('trend_strength')) else 0,
                'close': float(row['Close']) if 'Close' in row else None
            }
            result.append(entry)

        return result
