"""
Technical Indicator Service

Provides calculations for technical indicators used in prediction targets
and chart visualization.

Supports multi-timeframe indicators: calculate indicators on a higher timeframe
(e.g., 1h) and align to lower timeframe data (e.g., 15m).
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

# Timeframe to pandas resample offset mapping
TIMEFRAME_RESAMPLE_MAP = {
    '1m': '1min',
    '5m': '5min',
    '15m': '15min',
    '30m': '30min',
    '1h': '1h',
    '2h': '2h',
    '4h': '4h',
    '1d': '1D',
    'D1': '1D',
    '1w': '1W',
    'W1': '1W',
}

# Timeframe order for comparison (lower index = higher frequency)
TIMEFRAME_ORDER = ['1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', 'D1', '1w', 'W1']


def get_timeframe_order(tf: str) -> int:
    """Get the order index of a timeframe (lower = higher frequency)."""
    tf_normalized = tf.lower().replace(' ', '')
    for i, t in enumerate(TIMEFRAME_ORDER):
        if t.lower() == tf_normalized:
            return i
    # Default to hourly if unknown
    return TIMEFRAME_ORDER.index('1h')


def resample_ohlcv_to_timeframe(
    df: pd.DataFrame,
    target_timeframe: str,
    source_timeframe: Optional[str] = None
) -> pd.DataFrame:
    """
    Resample OHLCV data to a higher timeframe.

    Args:
        df: DataFrame with Date and OHLC columns
        target_timeframe: Target timeframe (e.g., '1h', '4h', '1d')
        source_timeframe: Optional source timeframe for validation

    Returns:
        Resampled DataFrame with Date, Open, High, Low, Close, Volume
    """
    if target_timeframe not in TIMEFRAME_RESAMPLE_MAP:
        raise ValueError(f"Unknown target timeframe: {target_timeframe}. Supported: {list(TIMEFRAME_RESAMPLE_MAP.keys())}")

    # Validate that target timeframe is higher than source
    if source_timeframe:
        source_order = get_timeframe_order(source_timeframe)
        target_order = get_timeframe_order(target_timeframe)
        if target_order < source_order:
            raise ValueError(f"Cannot resample from {source_timeframe} to lower timeframe {target_timeframe}")

    df = df.copy()

    # Ensure Date is datetime and set as index
    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date')

    # Get pandas offset string
    offset = TIMEFRAME_RESAMPLE_MAP[target_timeframe]

    # Resample OHLCV data
    resampled = df.resample(offset).agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum' if 'Volume' in df.columns else 'first'
    }).dropna()

    # Reset index to get Date column back
    resampled = resampled.reset_index()
    resampled = resampled.rename(columns={'index': 'Date'})

    logger.debug(f"Resampled {len(df)} rows to {len(resampled)} rows at {target_timeframe}")

    return resampled


def align_higher_timeframe_to_lower(
    lower_tf_df: pd.DataFrame,
    higher_tf_data: pd.DataFrame,
    value_columns: list
) -> pd.DataFrame:
    """
    Align higher timeframe data to lower timeframe using forward-fill merge.

    Each bar in the lower timeframe gets the most recent value from the
    higher timeframe bar that contains it.

    Args:
        lower_tf_df: Lower timeframe DataFrame with Date column
        higher_tf_data: Higher timeframe DataFrame with Date and value columns
        value_columns: List of column names to align

    Returns:
        Lower timeframe DataFrame with aligned higher timeframe values
    """
    result = lower_tf_df.copy()
    result['Date'] = pd.to_datetime(result['Date'])

    higher_tf_data = higher_tf_data.copy()
    higher_tf_data['Date'] = pd.to_datetime(higher_tf_data['Date'])

    # Sort both by date for merge_asof
    result = result.sort_values('Date').reset_index(drop=True)
    higher_tf_data = higher_tf_data.sort_values('Date').reset_index(drop=True)

    # Use merge_asof to align higher TF data to lower TF
    # direction='backward' means get the most recent higher TF value at or before each lower TF bar
    cols_to_merge = ['Date'] + value_columns
    result = pd.merge_asof(
        result,
        higher_tf_data[cols_to_merge],
        on='Date',
        direction='backward'
    )

    return result


class IndicatorService:
    """
    Calculate technical indicators for datasets.

    All methods expect a DataFrame with OHLC columns (Open, High, Low, Close)
    and return calculated indicator values as Series or Dict of Series.
    """

    def calculate_rsi(
        self,
        df: pd.DataFrame,
        period: int = 14
    ) -> pd.Series:
        """
        Calculate Relative Strength Index (RSI).

        RSI measures the speed and magnitude of recent price changes
        to evaluate overbought or oversold conditions.

        Args:
            df: DataFrame with 'Close' column
            period: RSI period (default 14)

        Returns:
            Series with RSI values (0-100 scale)
        """
        if 'Close' not in df.columns:
            raise ValueError("DataFrame must have 'Close' column")

        close = df['Close'].values
        n = len(close)

        # Calculate price changes
        delta = np.diff(close, prepend=close[0])

        # Separate gains and losses
        gains = np.where(delta > 0, delta, 0)
        losses = np.where(delta < 0, -delta, 0)

        # Calculate smoothed averages using Wilder's method
        avg_gain = np.zeros(n)
        avg_loss = np.zeros(n)

        # Initial SMA for first period
        if n >= period:
            avg_gain[period - 1] = np.mean(gains[1:period + 1])
            avg_loss[period - 1] = np.mean(losses[1:period + 1])

            # Subsequent values use exponential smoothing
            for i in range(period, n):
                avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
                avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

        # Calculate RS and RSI
        rsi = np.full(n, np.nan)
        for i in range(period - 1, n):
            if avg_loss[i] == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain[i] / avg_loss[i]
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))

        return pd.Series(rsi, index=df.index, name=f'rsi_{period}')

    def calculate_macd(
        self,
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> Dict[str, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence).

        MACD shows the relationship between two EMAs of price,
        with a signal line for trade signals.

        Args:
            df: DataFrame with 'Close' column
            fast: Fast EMA period (default 12)
            slow: Slow EMA period (default 26)
            signal: Signal line EMA period (default 9)

        Returns:
            Dict with 'macd', 'signal', 'histogram' Series
        """
        if 'Close' not in df.columns:
            raise ValueError("DataFrame must have 'Close' column")

        close = df['Close']

        # Calculate EMAs
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()

        # MACD line = Fast EMA - Slow EMA
        macd_line = ema_fast - ema_slow

        # Signal line = EMA of MACD line
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()

        # Histogram = MACD - Signal
        histogram = macd_line - signal_line

        return {
            'macd': pd.Series(macd_line.values, index=df.index, name=f'macd_{fast}_{slow}_{signal}'),
            'signal': pd.Series(signal_line.values, index=df.index, name=f'macd_signal_{fast}_{slow}_{signal}'),
            'histogram': pd.Series(histogram.values, index=df.index, name=f'macd_hist_{fast}_{slow}_{signal}')
        }

    def calculate_sar(
        self,
        df: pd.DataFrame,
        af_start: float = 0.02,
        af_max: float = 0.2
    ) -> pd.Series:
        """
        Calculate Parabolic SAR (Stop and Reverse).

        SAR provides potential entry and exit points, appearing as
        dots above or below price.

        Args:
            df: DataFrame with 'High' and 'Low' columns
            af_start: Starting acceleration factor (default 0.02)
            af_max: Maximum acceleration factor (default 0.2)

        Returns:
            Series with SAR values (price scale)
        """
        if 'High' not in df.columns or 'Low' not in df.columns:
            raise ValueError("DataFrame must have 'High' and 'Low' columns")

        high = df['High'].values
        low = df['Low'].values
        n = len(high)

        if n < 2:
            return pd.Series(np.full(n, np.nan), index=df.index, name='sar')

        sar = np.zeros(n)
        af = af_start
        is_uptrend = True

        # Initialize
        ep = high[0]  # Extreme point
        sar[0] = low[0]

        for i in range(1, n):
            # Calculate SAR for current bar
            sar[i] = sar[i - 1] + af * (ep - sar[i - 1])

            if is_uptrend:
                # In uptrend, SAR cannot be above prior two lows
                sar[i] = min(sar[i], low[i - 1])
                if i >= 2:
                    sar[i] = min(sar[i], low[i - 2])

                # Check for reversal
                if low[i] < sar[i]:
                    is_uptrend = False
                    sar[i] = ep
                    ep = low[i]
                    af = af_start
                else:
                    # Update extreme point and AF
                    if high[i] > ep:
                        ep = high[i]
                        af = min(af + af_start, af_max)
            else:
                # In downtrend, SAR cannot be below prior two highs
                sar[i] = max(sar[i], high[i - 1])
                if i >= 2:
                    sar[i] = max(sar[i], high[i - 2])

                # Check for reversal
                if high[i] > sar[i]:
                    is_uptrend = True
                    sar[i] = ep
                    ep = high[i]
                    af = af_start
                else:
                    # Update extreme point and AF
                    if low[i] < ep:
                        ep = low[i]
                        af = min(af + af_start, af_max)

        return pd.Series(sar, index=df.index, name=f'sar_{af_start}_{af_max}')

    def calculate_zigzag(
        self,
        df: pd.DataFrame,
        deviation_pct: float = 5.0
    ) -> pd.Series:
        """
        Calculate ZigZag indicator.

        ZigZag connects significant swing highs and lows, filtering
        out smaller price movements below the deviation threshold.

        Args:
            df: DataFrame with 'High' and 'Low' columns
            deviation_pct: Minimum percentage move to form new pivot (default 5.0)

        Returns:
            Series with ZigZag values (NaN between pivots, price at pivots)
        """
        if 'High' not in df.columns or 'Low' not in df.columns:
            raise ValueError("DataFrame must have 'High' and 'Low' columns")

        high = df['High'].values
        low = df['Low'].values
        n = len(high)

        if n < 2:
            return pd.Series(np.full(n, np.nan), index=df.index, name='zigzag')

        zigzag = np.full(n, np.nan)
        pivots = []  # List of (index, price, type) where type is 'high' or 'low'

        deviation = deviation_pct / 100.0

        # Find initial direction
        first_high_idx = 0
        first_low_idx = 0

        # Start with first bar
        last_pivot_type = None
        last_pivot_idx = 0
        last_pivot_price = (high[0] + low[0]) / 2

        # Determine initial trend by looking at first significant move
        for i in range(1, min(n, 20)):
            high_change = (high[i] - low[0]) / low[0]
            low_change = (high[0] - low[i]) / high[0]

            if high_change >= deviation:
                last_pivot_type = 'low'
                last_pivot_idx = 0
                last_pivot_price = low[0]
                pivots.append((0, low[0], 'low'))
                break
            elif low_change >= deviation:
                last_pivot_type = 'high'
                last_pivot_idx = 0
                last_pivot_price = high[0]
                pivots.append((0, high[0], 'high'))
                break

        if last_pivot_type is None:
            # No significant move found, use first bar high
            last_pivot_type = 'high'
            last_pivot_idx = 0
            last_pivot_price = high[0]
            pivots.append((0, high[0], 'high'))

        # Scan for pivots
        for i in range(1, n):
            if last_pivot_type == 'high':
                # Looking for a low pivot
                if low[i] < last_pivot_price * (1 - deviation):
                    # Found significant low
                    pivots.append((i, low[i], 'low'))
                    last_pivot_type = 'low'
                    last_pivot_idx = i
                    last_pivot_price = low[i]
                elif high[i] > last_pivot_price:
                    # Extend the high pivot
                    pivots[-1] = (i, high[i], 'high')
                    last_pivot_idx = i
                    last_pivot_price = high[i]
            else:
                # Looking for a high pivot
                if high[i] > last_pivot_price * (1 + deviation):
                    # Found significant high
                    pivots.append((i, high[i], 'high'))
                    last_pivot_type = 'high'
                    last_pivot_idx = i
                    last_pivot_price = high[i]
                elif low[i] < last_pivot_price:
                    # Extend the low pivot
                    pivots[-1] = (i, low[i], 'low')
                    last_pivot_idx = i
                    last_pivot_price = low[i]

        # Fill zigzag array at pivot points
        for idx, price, _ in pivots:
            zigzag[idx] = price

        # Interpolate between pivots for visualization
        zigzag_interp = np.copy(zigzag)
        for i in range(len(pivots) - 1):
            start_idx, start_price, _ = pivots[i]
            end_idx, end_price, _ = pivots[i + 1]

            if end_idx > start_idx:
                for j in range(start_idx, end_idx + 1):
                    t = (j - start_idx) / (end_idx - start_idx)
                    zigzag_interp[j] = start_price + t * (end_price - start_price)

        return pd.Series(zigzag_interp, index=df.index, name=f'zigzag_{deviation_pct}')

    def calculate_donchian(
        self,
        df: pd.DataFrame,
        period: int = 20
    ) -> Dict[str, pd.Series]:
        """
        Calculate Donchian Channels.

        Donchian channels show the highest high and lowest low over a period,
        useful for breakout/trend reversal detection.

        Args:
            df: DataFrame with 'High' and 'Low' columns
            period: Lookback period (default 20)

        Returns:
            Dict with 'upper', 'lower', 'middle' Series
        """
        if 'High' not in df.columns or 'Low' not in df.columns:
            raise ValueError("DataFrame must have 'High' and 'Low' columns")

        # Upper channel = highest high over period
        upper = df['High'].rolling(window=period).max()

        # Lower channel = lowest low over period
        lower = df['Low'].rolling(window=period).min()

        # Middle = average of upper and lower
        middle = (upper + lower) / 2

        return {
            'upper': pd.Series(upper.values, index=df.index, name=f'donchian_upper_{period}'),
            'lower': pd.Series(lower.values, index=df.index, name=f'donchian_lower_{period}'),
            'middle': pd.Series(middle.values, index=df.index, name=f'donchian_middle_{period}')
        }

    def calculate_donchian_breakout(
        self,
        df: pd.DataFrame,
        period: int = 20,
        direction: str = 'both'
    ) -> pd.Series:
        """
        Detect Donchian channel breakouts for trend reversal signals.

        Args:
            df: DataFrame with OHLC columns
            period: Lookback period (default 20)
            direction: 'up', 'down', or 'both'

        Returns:
            Series with values:
                1 = bullish breakout (close above upper channel)
               -1 = bearish breakout (close below lower channel)
                0 = no breakout
        """
        if 'Close' not in df.columns:
            raise ValueError("DataFrame must have 'Close' column")

        donchian = self.calculate_donchian(df, period)

        # Shift channels by 1 to compare current close to prior channel
        upper_prev = donchian['upper'].shift(1)
        lower_prev = donchian['lower'].shift(1)

        result = pd.Series(0, index=df.index, name=f'donchian_breakout_{period}')

        if direction in ('up', 'both'):
            # Bullish breakout: close > previous upper channel
            result = result.where(~(df['Close'] > upper_prev), 1)

        if direction in ('down', 'both'):
            # Bearish breakout: close < previous lower channel
            result = result.where(~(df['Close'] < lower_prev), -1)

        return result

    def calculate_adx(
        self,
        df: pd.DataFrame,
        period: int = 14
    ) -> Dict[str, pd.Series]:
        """
        Calculate Average Directional Index (ADX).

        ADX measures trend strength (not direction). Values > 25 indicate
        a strong trend, < 20 indicates a weak trend or ranging market.

        Args:
            df: DataFrame with 'High', 'Low', 'Close' columns
            period: ADX period (default 14)

        Returns:
            Dict with 'adx', 'plus_di', 'minus_di' Series
        """
        if not all(col in df.columns for col in ['High', 'Low', 'Close']):
            raise ValueError("DataFrame must have 'High', 'Low', 'Close' columns")

        high = df['High']
        low = df['Low']
        close = df['Close']

        # True Range
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Directional Movement
        plus_dm = high.diff()
        minus_dm = -low.diff()

        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        # Smoothed averages (Wilder's smoothing)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()

        # Directional Indicators
        plus_di = 100 * plus_dm_smooth / atr
        minus_di = 100 * minus_dm_smooth / atr

        # DX and ADX
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.ewm(alpha=1/period, adjust=False).mean()

        return {
            'adx': pd.Series(adx.values, index=df.index, name=f'adx_{period}'),
            'plus_di': pd.Series(plus_di.values, index=df.index, name=f'plus_di_{period}'),
            'minus_di': pd.Series(minus_di.values, index=df.index, name=f'minus_di_{period}')
        }

    def calculate_atr(
        self,
        df: pd.DataFrame,
        period: int = 14
    ) -> pd.Series:
        """
        Calculate Average True Range (ATR).

        ATR measures volatility, useful for setting stop-losses and
        detecting volatility expansions/contractions.

        Args:
            df: DataFrame with 'High', 'Low', 'Close' columns
            period: ATR period (default 14)

        Returns:
            Series with ATR values
        """
        if not all(col in df.columns for col in ['High', 'Low', 'Close']):
            raise ValueError("DataFrame must have 'High', 'Low', 'Close' columns")

        high = df['High']
        low = df['Low']
        close = df['Close']

        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        return pd.Series(atr.values, index=df.index, name=f'atr_{period}')

    def calculate_pivot_points(
        self,
        df: pd.DataFrame,
        method: str = 'standard'
    ) -> Dict[str, pd.Series]:
        """
        Calculate Pivot Points for support/resistance levels.

        Args:
            df: DataFrame with 'High', 'Low', 'Close' columns
            method: 'standard', 'fibonacci', 'camarilla', or 'woodie'

        Returns:
            Dict with 'pivot', 'r1', 'r2', 'r3', 's1', 's2', 's3' Series
        """
        if not all(col in df.columns for col in ['High', 'Low', 'Close']):
            raise ValueError("DataFrame must have 'High', 'Low', 'Close' columns")

        # Use previous day's values (shift by 1)
        high = df['High'].shift(1)
        low = df['Low'].shift(1)
        close = df['Close'].shift(1)

        if method == 'standard':
            pivot = (high + low + close) / 3
            r1 = 2 * pivot - low
            s1 = 2 * pivot - high
            r2 = pivot + (high - low)
            s2 = pivot - (high - low)
            r3 = high + 2 * (pivot - low)
            s3 = low - 2 * (high - pivot)

        elif method == 'fibonacci':
            pivot = (high + low + close) / 3
            diff = high - low
            r1 = pivot + 0.382 * diff
            r2 = pivot + 0.618 * diff
            r3 = pivot + 1.0 * diff
            s1 = pivot - 0.382 * diff
            s2 = pivot - 0.618 * diff
            s3 = pivot - 1.0 * diff

        elif method == 'camarilla':
            pivot = (high + low + close) / 3
            diff = high - low
            r1 = close + diff * 1.1 / 12
            r2 = close + diff * 1.1 / 6
            r3 = close + diff * 1.1 / 4
            s1 = close - diff * 1.1 / 12
            s2 = close - diff * 1.1 / 6
            s3 = close - diff * 1.1 / 4

        elif method == 'woodie':
            pivot = (high + low + 2 * close) / 4
            r1 = 2 * pivot - low
            s1 = 2 * pivot - high
            r2 = pivot + (high - low)
            s2 = pivot - (high - low)
            r3 = high + 2 * (pivot - low)
            s3 = low - 2 * (high - pivot)

        else:
            raise ValueError(f"Unknown pivot method: {method}")

        return {
            'pivot': pd.Series(pivot.values, index=df.index, name=f'pivot_{method}'),
            'r1': pd.Series(r1.values, index=df.index, name=f'r1_{method}'),
            'r2': pd.Series(r2.values, index=df.index, name=f'r2_{method}'),
            'r3': pd.Series(r3.values, index=df.index, name=f'r3_{method}'),
            's1': pd.Series(s1.values, index=df.index, name=f's1_{method}'),
            's2': pd.Series(s2.values, index=df.index, name=f's2_{method}'),
            's3': pd.Series(s3.values, index=df.index, name=f's3_{method}')
        }

    def calculate_stochastic(
        self,
        df: pd.DataFrame,
        k_period: int = 14,
        d_period: int = 3
    ) -> Dict[str, pd.Series]:
        """
        Calculate Stochastic Oscillator.

        Shows where the close is relative to the high-low range.
        Overbought > 80, Oversold < 20.

        Args:
            df: DataFrame with 'High', 'Low', 'Close' columns
            k_period: %K period (default 14)
            d_period: %D smoothing period (default 3)

        Returns:
            Dict with 'k', 'd' Series
        """
        if not all(col in df.columns for col in ['High', 'Low', 'Close']):
            raise ValueError("DataFrame must have 'High', 'Low', 'Close' columns")

        low_min = df['Low'].rolling(window=k_period).min()
        high_max = df['High'].rolling(window=k_period).max()

        k = 100 * (df['Close'] - low_min) / (high_max - low_min)
        d = k.rolling(window=d_period).mean()

        return {
            'k': pd.Series(k.values, index=df.index, name=f'stoch_k_{k_period}'),
            'd': pd.Series(d.values, index=df.index, name=f'stoch_d_{k_period}_{d_period}')
        }

    def calculate_obv(
        self,
        df: pd.DataFrame
    ) -> pd.Series:
        """
        Calculate On-Balance Volume (OBV).

        OBV uses volume flow to predict price changes.

        Args:
            df: DataFrame with 'Close', 'Volume' columns

        Returns:
            Series with OBV values
        """
        if 'Close' not in df.columns or 'Volume' not in df.columns:
            raise ValueError("DataFrame must have 'Close' and 'Volume' columns")

        close_diff = df['Close'].diff()
        volume = df['Volume']

        obv = pd.Series(0.0, index=df.index)
        obv = obv.where(close_diff == 0, volume.where(close_diff > 0, -volume))
        obv = obv.cumsum()

        return pd.Series(obv.values, index=df.index, name='obv')

    def calculate_indicators(
        self,
        df: pd.DataFrame,
        indicators: list
    ) -> Dict[str, Any]:
        """
        Calculate multiple indicators at once.

        Args:
            df: DataFrame with OHLC columns
            indicators: List of indicator configs, e.g.:
                [
                    {"type": "rsi", "period": 14},
                    {"type": "macd", "fast": 12, "slow": 26, "signal": 9}
                ]

        Returns:
            Dict with indicator names as keys and Series/Dict as values
        """
        results = {}

        for ind in indicators:
            ind_type = ind.get('type', '').lower()

            try:
                if ind_type == 'rsi':
                    period = ind.get('period', 14)
                    results[f'rsi_{period}'] = self.calculate_rsi(df, period)

                elif ind_type == 'macd':
                    fast = ind.get('fast', 12)
                    slow = ind.get('slow', 26)
                    signal = ind.get('signal', 9)
                    macd_data = self.calculate_macd(df, fast, slow, signal)
                    results[f'macd_{fast}_{slow}_{signal}'] = macd_data['macd']
                    results[f'macd_signal_{fast}_{slow}_{signal}'] = macd_data['signal']
                    results[f'macd_hist_{fast}_{slow}_{signal}'] = macd_data['histogram']

                elif ind_type == 'sar':
                    af_start = ind.get('af_start', 0.02)
                    af_max = ind.get('af_max', 0.2)
                    results[f'sar_{af_start}_{af_max}'] = self.calculate_sar(df, af_start, af_max)

                elif ind_type == 'zigzag':
                    deviation_pct = ind.get('deviation_pct', 5.0)
                    results[f'zigzag_{deviation_pct}'] = self.calculate_zigzag(df, deviation_pct)

                elif ind_type == 'donchian':
                    period = ind.get('period', 20)
                    donchian_data = self.calculate_donchian(df, period)
                    results[f'donchian_upper_{period}'] = donchian_data['upper']
                    results[f'donchian_lower_{period}'] = donchian_data['lower']
                    results[f'donchian_middle_{period}'] = donchian_data['middle']

                elif ind_type == 'donchian_breakout':
                    period = ind.get('period', 20)
                    direction = ind.get('direction', 'both')
                    results[f'donchian_breakout_{period}'] = self.calculate_donchian_breakout(df, period, direction)

                elif ind_type == 'adx':
                    period = ind.get('period', 14)
                    adx_data = self.calculate_adx(df, period)
                    results[f'adx_{period}'] = adx_data['adx']
                    results[f'plus_di_{period}'] = adx_data['plus_di']
                    results[f'minus_di_{period}'] = adx_data['minus_di']

                elif ind_type == 'atr':
                    period = ind.get('period', 14)
                    results[f'atr_{period}'] = self.calculate_atr(df, period)

                elif ind_type == 'pivot_points':
                    method = ind.get('method', 'standard')
                    pivot_data = self.calculate_pivot_points(df, method)
                    for key, series in pivot_data.items():
                        results[f'{key}_{method}'] = series

                elif ind_type == 'stochastic':
                    k_period = ind.get('k_period', 14)
                    d_period = ind.get('d_period', 3)
                    stoch_data = self.calculate_stochastic(df, k_period, d_period)
                    results[f'stoch_k_{k_period}'] = stoch_data['k']
                    results[f'stoch_d_{k_period}_{d_period}'] = stoch_data['d']

                elif ind_type == 'obv':
                    results['obv'] = self.calculate_obv(df)

                else:
                    logger.warning(f"Unknown indicator type: {ind_type}")

            except Exception as e:
                logger.error(f"Error calculating {ind_type}: {e}")
                raise

        return results

    def calculate_indicators_multi_timeframe(
        self,
        df: pd.DataFrame,
        indicators: list,
        target_timeframe: str,
        source_timeframe: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculate indicators on a higher timeframe and align to base timeframe.

        This is used for multi-timeframe targets where you want, e.g., a 1h ZigZag
        indicator calculated and then aligned to 15m data.

        Args:
            df: Base DataFrame with Date and OHLC columns
            indicators: List of indicator configs
            target_timeframe: Timeframe to calculate indicators on (e.g., '1h')
            source_timeframe: Optional source timeframe of the data

        Returns:
            Dict with indicator names as keys and Series (aligned to df) as values
        """
        # Resample to target timeframe
        resampled_df = resample_ohlcv_to_timeframe(df, target_timeframe, source_timeframe)

        if len(resampled_df) < 5:
            logger.warning(f"Not enough data after resampling to {target_timeframe}: {len(resampled_df)} bars")
            return {}

        # Calculate indicators on resampled data
        indicator_results = self.calculate_indicators(resampled_df, indicators)

        # Build a DataFrame with the indicator results for alignment
        indicator_df = resampled_df[['Date']].copy()
        for name, series in indicator_results.items():
            indicator_df[name] = series.values

        # Align back to original timeframe
        aligned_df = align_higher_timeframe_to_lower(
            df,
            indicator_df,
            list(indicator_results.keys())
        )

        # Extract aligned results as series
        results = {}
        for name in indicator_results.keys():
            # Prefix with timeframe for clarity
            tf_name = f"{name}_{target_timeframe}"
            results[tf_name] = pd.Series(
                aligned_df[name].values,
                index=df.index,
                name=tf_name
            )

        logger.info(f"Calculated {len(results)} indicators on {target_timeframe} timeframe")
        return results
