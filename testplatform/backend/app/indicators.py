"""
Technical Indicators Module

Calculates technical indicators for financial time series data.
Uses pandas and pandas_ta for indicator calculations.
"""

import pandas as pd
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """
    Technical indicators calculator for financial datasets.
    Provides common indicators like SMA, EMA, RSI, MACD, etc.
    """

    @staticmethod
    def calculate_sma(df: pd.DataFrame, column: str = 'Close', period: int = 20) -> pd.Series:
        """
        Calculate Simple Moving Average (SMA).

        Args:
            df: DataFrame with OHLC data
            column: Column name to calculate SMA on (default: 'Close')
            period: Period for moving average (default: 20)

        Returns:
            Series with SMA values
        """
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in DataFrame")

        sma = df[column].rolling(window=period, min_periods=period).mean()
        logger.debug(f"Calculated SMA({period}) on {column}")
        return sma

    @staticmethod
    def calculate_ema(df: pd.DataFrame, column: str = 'Close', period: int = 20) -> pd.Series:
        """
        Calculate Exponential Moving Average (EMA).

        Args:
            df: DataFrame with OHLC data
            column: Column name to calculate EMA on (default: 'Close')
            period: Period for exponential average (default: 20)

        Returns:
            Series with EMA values
        """
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in DataFrame")

        ema = df[column].ewm(span=period, adjust=False).mean()
        logger.debug(f"Calculated EMA({period}) on {column}")
        return ema

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, column: str = 'Close', period: int = 14) -> pd.Series:
        """
        Calculate Relative Strength Index (RSI).

        Args:
            df: DataFrame with OHLC data
            column: Column name to calculate RSI on (default: 'Close')
            period: Period for RSI calculation (default: 14)

        Returns:
            Series with RSI values (0-100)
        """
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in DataFrame")

        # Calculate price changes
        delta = df[column].diff()

        # Separate gains and losses
        gains = delta.where(delta > 0, 0.0)
        losses = -delta.where(delta < 0, 0.0)

        # Calculate average gains and losses
        avg_gains = gains.rolling(window=period, min_periods=period).mean()
        avg_losses = losses.rolling(window=period, min_periods=period).mean()

        # Calculate RS and RSI
        rs = avg_gains / avg_losses
        rsi = 100 - (100 / (1 + rs))

        logger.debug(f"Calculated RSI({period}) on {column}")
        return rsi

    @staticmethod
    def calculate_macd(
        df: pd.DataFrame,
        column: str = 'Close',
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Dict[str, pd.Series]:
        """
        Calculate MACD (Moving Average Convergence Divergence).

        Args:
            df: DataFrame with OHLC data
            column: Column name to calculate MACD on (default: 'Close')
            fast_period: Fast EMA period (default: 12)
            slow_period: Slow EMA period (default: 26)
            signal_period: Signal line EMA period (default: 9)

        Returns:
            Dictionary with 'macd', 'signal', and 'histogram' Series
        """
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in DataFrame")

        # Calculate fast and slow EMAs
        fast_ema = df[column].ewm(span=fast_period, adjust=False).mean()
        slow_ema = df[column].ewm(span=slow_period, adjust=False).mean()

        # Calculate MACD line
        macd_line = fast_ema - slow_ema

        # Calculate signal line
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()

        # Calculate histogram
        histogram = macd_line - signal_line

        logger.debug(f"Calculated MACD({fast_period},{slow_period},{signal_period}) on {column}")

        return {
            'macd': macd_line,
            'signal': signal_line,
            'histogram': histogram
        }

    @staticmethod
    def calculate_bollinger_bands(
        df: pd.DataFrame,
        column: str = 'Close',
        period: int = 20,
        std_dev: float = 2.0
    ) -> Dict[str, pd.Series]:
        """
        Calculate Bollinger Bands.

        Args:
            df: DataFrame with OHLC data
            column: Column name to calculate bands on (default: 'Close')
            period: Period for moving average (default: 20)
            std_dev: Number of standard deviations (default: 2.0)

        Returns:
            Dictionary with 'upper', 'middle', and 'lower' Series
        """
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found in DataFrame")

        # Calculate middle band (SMA)
        middle_band = df[column].rolling(window=period, min_periods=period).mean()

        # Calculate standard deviation
        std = df[column].rolling(window=period, min_periods=period).std()

        # Calculate upper and lower bands
        upper_band = middle_band + (std * std_dev)
        lower_band = middle_band - (std * std_dev)

        logger.debug(f"Calculated Bollinger Bands({period},{std_dev}) on {column}")

        return {
            'upper': upper_band,
            'middle': middle_band,
            'lower': lower_band
        }

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        Calculate Average True Range (ATR).

        Args:
            df: DataFrame with OHLC data (must have High, Low, Close)
            period: Period for ATR calculation (default: 14)

        Returns:
            Series with ATR values
        """
        required_columns = ['High', 'Low', 'Close']
        for col in required_columns:
            if col not in df.columns:
                raise ValueError(f"Column '{col}' not found in DataFrame. ATR requires High, Low, Close.")

        # Calculate true range components
        high_low = df['High'] - df['Low']
        high_close = (df['High'] - df['Close'].shift()).abs()
        low_close = (df['Low'] - df['Close'].shift()).abs()

        # True range is the maximum of the three
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

        # ATR is the moving average of true range
        atr = true_range.rolling(window=period, min_periods=period).mean()

        logger.debug(f"Calculated ATR({period})")
        return atr

    @staticmethod
    def calculate_stochastic(
        df: pd.DataFrame,
        k_period: int = 14,
        d_period: int = 3,
        smooth_k: int = 3
    ) -> Dict[str, pd.Series]:
        """
        Calculate Stochastic Oscillator (%K and %D).

        Args:
            df: DataFrame with OHLC data (must have High, Low, Close)
            k_period: Period for %K calculation (default: 14)
            d_period: Period for %D (SMA of %K) (default: 3)
            smooth_k: Smoothing period for %K (default: 3)

        Returns:
            Dictionary with 'k' (%K) and 'd' (%D) Series (values 0-100)
        """
        required_columns = ['High', 'Low', 'Close']
        for col in required_columns:
            if col not in df.columns:
                raise ValueError(f"Column '{col}' not found in DataFrame. Stochastic requires High, Low, Close.")

        # Calculate rolling high and low
        low_min = df['Low'].rolling(window=k_period, min_periods=k_period).min()
        high_max = df['High'].rolling(window=k_period, min_periods=k_period).max()

        # Calculate raw %K (Fast Stochastic)
        raw_k = 100 * (df['Close'] - low_min) / (high_max - low_min)

        # Smooth %K if smooth_k > 1 (Slow Stochastic)
        if smooth_k > 1:
            k = raw_k.rolling(window=smooth_k, min_periods=smooth_k).mean()
        else:
            k = raw_k

        # Calculate %D (SMA of %K)
        d = k.rolling(window=d_period, min_periods=d_period).mean()

        logger.debug(f"Calculated Stochastic({k_period},{d_period},{smooth_k})")

        return {
            'k': k,
            'd': d
        }

    @staticmethod
    def calculate_sar(
        df: pd.DataFrame,
        af_start: float = 0.02,
        af_max: float = 0.2
    ) -> pd.Series:
        """
        Calculate Parabolic SAR (Stop and Reverse).

        Args:
            df: DataFrame with High and Low columns
            af_start: Starting acceleration factor (default 0.02)
            af_max: Maximum acceleration factor (default 0.2)

        Returns:
            Series with SAR values
        """
        import numpy as np

        high = df['High'].values
        low = df['Low'].values
        n = len(high)

        if n < 2:
            return pd.Series([np.nan] * n, index=df.index)

        sar = np.zeros(n)
        af = af_start
        is_uptrend = True
        ep = high[0]
        sar[0] = low[0]

        for i in range(1, n):
            if is_uptrend:
                sar[i] = sar[i-1] + af * (ep - sar[i-1])
                sar[i] = min(sar[i], low[i-1], low[i-2] if i >= 2 else low[i-1])
                if low[i] < sar[i]:
                    is_uptrend = False
                    sar[i] = ep
                    ep = low[i]
                    af = af_start
                else:
                    if high[i] > ep:
                        ep = high[i]
                        af = min(af + af_start, af_max)
            else:
                sar[i] = sar[i-1] + af * (ep - sar[i-1])
                sar[i] = max(sar[i], high[i-1], high[i-2] if i >= 2 else high[i-1])
                if high[i] > sar[i]:
                    is_uptrend = True
                    sar[i] = ep
                    ep = high[i]
                    af = af_start
                else:
                    if low[i] < ep:
                        ep = low[i]
                        af = min(af + af_start, af_max)

        logger.debug(f"Calculated SAR({af_start},{af_max})")
        return pd.Series(sar, index=df.index)

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> Dict[str, pd.Series]:
        """
        Calculate ADX (Average Directional Index) with +DI and -DI.

        Args:
            df: DataFrame with High, Low, Close columns
            period: ADX period (default 14)

        Returns:
            Dictionary with 'adx', 'plus_di', 'minus_di' Series
        """
        import numpy as np

        high = df['High'].values
        low = df['Low'].values
        close = df['Close'].values
        n = len(high)

        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        tr = np.zeros(n)

        for i in range(1, n):
            up_move = high[i] - high[i-1]
            down_move = low[i-1] - low[i]
            plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0
            minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))

        # Smooth with Wilder's method
        atr = pd.Series(tr).rolling(window=period).mean().values
        smooth_plus_dm = pd.Series(plus_dm).rolling(window=period).mean().values
        smooth_minus_dm = pd.Series(minus_dm).rolling(window=period).mean().values

        plus_di = np.where(atr > 0, 100 * smooth_plus_dm / atr, 0)
        minus_di = np.where(atr > 0, 100 * smooth_minus_dm / atr, 0)

        dx = np.where((plus_di + minus_di) > 0, 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di), 0)
        adx = pd.Series(dx).rolling(window=period).mean().values

        logger.debug(f"Calculated ADX({period})")
        return {
            'adx': pd.Series(adx, index=df.index),
            'plus_di': pd.Series(plus_di, index=df.index),
            'minus_di': pd.Series(minus_di, index=df.index)
        }

    @staticmethod
    def calculate_obv(df: pd.DataFrame) -> pd.Series:
        """
        Calculate On-Balance Volume (OBV).

        Args:
            df: DataFrame with Close and Volume columns

        Returns:
            Series with OBV values
        """
        import numpy as np

        close = df['Close'].values
        volume = df['Volume'].values
        n = len(close)

        obv = np.zeros(n)
        obv[0] = volume[0]

        for i in range(1, n):
            if close[i] > close[i-1]:
                obv[i] = obv[i-1] + volume[i]
            elif close[i] < close[i-1]:
                obv[i] = obv[i-1] - volume[i]
            else:
                obv[i] = obv[i-1]

        logger.debug("Calculated OBV")
        return pd.Series(obv, index=df.index)

    @staticmethod
    def calculate_donchian(df: pd.DataFrame, period: int = 20) -> Dict[str, pd.Series]:
        """
        Calculate Donchian Channel.

        Args:
            df: DataFrame with High and Low columns
            period: Lookback period (default 20)

        Returns:
            Dictionary with 'upper', 'lower', 'middle' Series
        """
        upper = df['High'].rolling(window=period).max()
        lower = df['Low'].rolling(window=period).min()
        middle = (upper + lower) / 2

        logger.debug(f"Calculated Donchian({period})")
        return {
            'upper': upper,
            'lower': lower,
            'middle': middle
        }

    @staticmethod
    def calculate_zigzag(df: pd.DataFrame, deviation_pct: float = 5.0) -> pd.Series:
        """
        Calculate ZigZag indicator.

        Args:
            df: DataFrame with High and Low columns
            deviation_pct: Minimum percentage move to form a pivot (default 5.0)

        Returns:
            Series with ZigZag values (NaN for non-pivot points)
        """
        import numpy as np

        high = df['High'].values
        low = df['Low'].values
        n = len(high)

        zigzag = np.full(n, np.nan)
        if n < 2:
            return pd.Series(zigzag, index=df.index)

        direction = 0  # 1 = up, -1 = down
        last_pivot_idx = 0
        last_pivot_val = (high[0] + low[0]) / 2

        for i in range(1, n):
            if direction == 0:
                if high[i] >= last_pivot_val * (1 + deviation_pct/100):
                    direction = 1
                    last_pivot_val = high[i]
                    last_pivot_idx = i
                elif low[i] <= last_pivot_val * (1 - deviation_pct/100):
                    direction = -1
                    last_pivot_val = low[i]
                    last_pivot_idx = i
            elif direction == 1:
                if high[i] > last_pivot_val:
                    last_pivot_val = high[i]
                    last_pivot_idx = i
                elif low[i] <= last_pivot_val * (1 - deviation_pct/100):
                    zigzag[last_pivot_idx] = last_pivot_val
                    direction = -1
                    last_pivot_val = low[i]
                    last_pivot_idx = i
            else:  # direction == -1
                if low[i] < last_pivot_val:
                    last_pivot_val = low[i]
                    last_pivot_idx = i
                elif high[i] >= last_pivot_val * (1 + deviation_pct/100):
                    zigzag[last_pivot_idx] = last_pivot_val
                    direction = 1
                    last_pivot_val = high[i]
                    last_pivot_idx = i

        zigzag[last_pivot_idx] = last_pivot_val

        logger.debug(f"Calculated ZigZag({deviation_pct}%)")
        return pd.Series(zigzag, index=df.index)

    @staticmethod
    def calculate_pivot_points(df: pd.DataFrame, method: str = 'standard') -> Dict[str, pd.Series]:
        """
        Calculate Pivot Points.

        Args:
            df: DataFrame with High, Low, Close columns
            method: 'standard' or 'fibonacci' (default 'standard')

        Returns:
            Dictionary with pivot, r1, r2, r3, s1, s2, s3 Series
        """
        high = df['High'].shift(1)
        low = df['Low'].shift(1)
        close = df['Close'].shift(1)

        pivot = (high + low + close) / 3

        if method == 'fibonacci':
            diff = high - low
            r1 = pivot + 0.382 * diff
            r2 = pivot + 0.618 * diff
            r3 = pivot + 1.0 * diff
            s1 = pivot - 0.382 * diff
            s2 = pivot - 0.618 * diff
            s3 = pivot - 1.0 * diff
        else:  # standard
            r1 = 2 * pivot - low
            s1 = 2 * pivot - high
            r2 = pivot + (high - low)
            s2 = pivot - (high - low)
            r3 = pivot + 2 * (high - low)
            s3 = pivot - 2 * (high - low)

        logger.debug(f"Calculated Pivot Points ({method})")
        return {
            'pivot': pivot,
            'r1': r1, 'r2': r2, 'r3': r3,
            's1': s1, 's2': s2, 's3': s3
        }

    @staticmethod
    def add_indicators_to_dataframe(
        df: pd.DataFrame,
        indicators_config: Dict[str, Any]
    ) -> pd.DataFrame:
        """
        Add multiple technical indicators to a DataFrame.

        Args:
            df: DataFrame with OHLC data
            indicators_config: Dictionary with indicator configurations
                Example: {
                    'sma_20': {'type': 'sma', 'period': 20},
                    'ema_50': {'type': 'ema', 'period': 50},
                    'rsi_14': {'type': 'rsi', 'period': 14},
                    'macd': {'type': 'macd', 'fast': 12, 'slow': 26, 'signal': 9}
                }

        Returns:
            DataFrame with added indicator columns
        """
        df_copy = df.copy()

        for indicator_name, config in indicators_config.items():
            indicator_type = config.get('type', '').lower()

            try:
                if indicator_type == 'sma':
                    period = config.get('period', 20)
                    column = config.get('column', 'Close')
                    df_copy[indicator_name] = TechnicalIndicators.calculate_sma(df_copy, column, period)

                elif indicator_type == 'ema':
                    period = config.get('period', 20)
                    column = config.get('column', 'Close')
                    df_copy[indicator_name] = TechnicalIndicators.calculate_ema(df_copy, column, period)

                elif indicator_type == 'rsi':
                    period = config.get('period', 14)
                    column = config.get('column', 'Close')
                    df_copy[indicator_name] = TechnicalIndicators.calculate_rsi(df_copy, column, period)

                elif indicator_type == 'macd':
                    fast = config.get('fast', 12)
                    slow = config.get('slow', 26)
                    signal = config.get('signal', 9)
                    column = config.get('column', 'Close')
                    macd_data = TechnicalIndicators.calculate_macd(df_copy, column, fast, slow, signal)
                    df_copy[f'{indicator_name}_line'] = macd_data['macd']
                    df_copy[f'{indicator_name}_signal'] = macd_data['signal']
                    df_copy[f'{indicator_name}_histogram'] = macd_data['histogram']

                elif indicator_type == 'bollinger' or indicator_type == 'bbands':
                    period = config.get('period', 20)
                    std_dev = config.get('std_dev', 2.0)
                    column = config.get('column', 'Close')
                    bb_data = TechnicalIndicators.calculate_bollinger_bands(df_copy, column, period, std_dev)
                    df_copy[f'{indicator_name}_upper'] = bb_data['upper']
                    df_copy[f'{indicator_name}_middle'] = bb_data['middle']
                    df_copy[f'{indicator_name}_lower'] = bb_data['lower']

                elif indicator_type == 'atr':
                    period = config.get('period', 14)
                    df_copy[indicator_name] = TechnicalIndicators.calculate_atr(df_copy, period)

                elif indicator_type == 'stochastic' or indicator_type == 'stoch':
                    k_period = config.get('k_period', 14)
                    d_period = config.get('d_period', 3)
                    smooth_k = config.get('smooth_k', 3)
                    stoch_data = TechnicalIndicators.calculate_stochastic(df_copy, k_period, d_period, smooth_k)
                    df_copy[f'{indicator_name}_k'] = stoch_data['k']
                    df_copy[f'{indicator_name}_d'] = stoch_data['d']

                elif indicator_type == 'sar':
                    af_start = config.get('af_start', 0.02)
                    af_max = config.get('af_max', 0.2)
                    df_copy[indicator_name] = TechnicalIndicators.calculate_sar(df_copy, af_start, af_max)

                elif indicator_type == 'adx':
                    period = config.get('period', 14)
                    adx_data = TechnicalIndicators.calculate_adx(df_copy, period)
                    df_copy[f'{indicator_name}_adx'] = adx_data['adx']
                    df_copy[f'{indicator_name}_plus_di'] = adx_data['plus_di']
                    df_copy[f'{indicator_name}_minus_di'] = adx_data['minus_di']

                elif indicator_type == 'obv':
                    df_copy[indicator_name] = TechnicalIndicators.calculate_obv(df_copy)

                elif indicator_type == 'donchian':
                    period = config.get('period', 20)
                    donchian_data = TechnicalIndicators.calculate_donchian(df_copy, period)
                    df_copy[f'{indicator_name}_upper'] = donchian_data['upper']
                    df_copy[f'{indicator_name}_lower'] = donchian_data['lower']
                    df_copy[f'{indicator_name}_middle'] = donchian_data['middle']

                elif indicator_type == 'zigzag':
                    deviation_pct = config.get('deviation_pct', 5.0)
                    df_copy[indicator_name] = TechnicalIndicators.calculate_zigzag(df_copy, deviation_pct)

                elif indicator_type == 'pivot_points' or indicator_type == 'pivot':
                    method = config.get('method', 'standard')
                    pivot_data = TechnicalIndicators.calculate_pivot_points(df_copy, method)
                    df_copy[f'{indicator_name}_pivot'] = pivot_data['pivot']
                    df_copy[f'{indicator_name}_r1'] = pivot_data['r1']
                    df_copy[f'{indicator_name}_r2'] = pivot_data['r2']
                    df_copy[f'{indicator_name}_r3'] = pivot_data['r3']
                    df_copy[f'{indicator_name}_s1'] = pivot_data['s1']
                    df_copy[f'{indicator_name}_s2'] = pivot_data['s2']
                    df_copy[f'{indicator_name}_s3'] = pivot_data['s3']

                else:
                    logger.warning(f"Unknown indicator type: {indicator_type}")

            except Exception as e:
                logger.error(f"Error calculating indicator {indicator_name}: {e}")

        return df_copy
