"""
Synthetic Dataset Generators for Model Testing

Creates predictable datasets with known statistical properties for testing
model training, loss functions, and classification thresholds.

Each dataset includes: Date, Open, High, Low, Close, Volume + 5 synthetic indicators + target
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional


def _generate_base_ohlcv(
    n_rows: int,
    start_price: float = 100.0,
    volatility: float = 0.02,
    start_date: Optional[datetime] = None,
    freq_hours: int = 1,
    seed: Optional[int] = None
) -> pd.DataFrame:
    """
    Generate base OHLCV data with realistic price movements.

    Args:
        n_rows: Number of rows to generate
        start_price: Starting price
        volatility: Daily volatility (as fraction)
        start_date: Starting date (defaults to 2020-01-01)
        freq_hours: Hours between bars
        seed: Random seed for reproducibility

    Returns:
        DataFrame with Date, Open, High, Low, Close, Volume columns
    """
    if seed is not None:
        np.random.seed(seed)

    if start_date is None:
        start_date = datetime(2020, 1, 1, 9, 30)

    # Generate dates
    dates = [start_date + timedelta(hours=i * freq_hours) for i in range(n_rows)]

    # Generate price series using geometric brownian motion
    returns = np.random.normal(0, volatility, n_rows)
    prices = start_price * np.exp(np.cumsum(returns))

    # Generate OHLC from prices
    close = prices

    # High/Low based on intrabar volatility
    intrabar_vol = volatility * 0.5
    high = close * (1 + np.abs(np.random.normal(0, intrabar_vol, n_rows)))
    low = close * (1 - np.abs(np.random.normal(0, intrabar_vol, n_rows)))

    # Open is previous close with some gap
    open_prices = np.roll(close, 1) * (1 + np.random.normal(0, volatility * 0.1, n_rows))
    open_prices[0] = start_price

    # Volume with some randomness
    base_volume = 1000000
    volume = base_volume * (1 + np.random.uniform(-0.5, 0.5, n_rows))

    return pd.DataFrame({
        'Date': dates,
        'Open': open_prices,
        'High': high,
        'Low': low,
        'Close': close,
        'Volume': volume.astype(int)
    })


def _add_synthetic_indicators(df: pd.DataFrame, seed: Optional[int] = None) -> pd.DataFrame:
    """
    Add 5 synthetic indicators to DataFrame.

    Adds:
    - SMA_20: 20-period simple moving average (realistic)
    - RSI_14: RSI-like oscillator (0-100)
    - MACD: MACD-like indicator
    - BB_upper: Bollinger Band upper (realistic)
    - BB_lower: Bollinger Band lower (realistic)
    """
    if seed is not None:
        np.random.seed(seed)

    df = df.copy()

    # SMA 20
    df['SMA_20'] = df['Close'].rolling(window=20, min_periods=1).mean()

    # RSI-like (0-100 range)
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(window=14, min_periods=1).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14, min_periods=1).mean()
    rs = gain / (loss + 1e-10)
    df['RSI_14'] = 100 - (100 / (1 + rs))

    # MACD-like
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26

    # Bollinger Bands
    sma20 = df['Close'].rolling(window=20, min_periods=1).mean()
    std20 = df['Close'].rolling(window=20, min_periods=1).std().fillna(0)
    df['BB_upper'] = sma20 + 2 * std20
    df['BB_lower'] = sma20 - 2 * std20

    return df


def generate_balanced_binary(
    n_rows: int = 1000,
    seq_len: int = 24,
    seed: int = 42
) -> pd.DataFrame:
    """
    Generate balanced binary classification dataset.

    Uses a sine wave pattern with noise to create roughly 50/50 up/down classification.
    The target is whether the next bar closes higher than current.

    This dataset should be easy for models to learn because:
    - Clear cyclical pattern in prices
    - Balanced classes (50/50)
    - Low noise-to-signal ratio

    Args:
        n_rows: Number of rows to generate
        seq_len: Sequence length (for ensuring enough data)
        seed: Random seed for reproducibility

    Returns:
        DataFrame with OHLCV, indicators, and 'target' column
    """
    np.random.seed(seed)

    # Generate base data
    df = _generate_base_ohlcv(n_rows, seed=seed)

    # Add a strong cyclical component to make classification easier
    cycle_period = 20  # 20 bars per cycle
    cycle = np.sin(2 * np.pi * np.arange(n_rows) / cycle_period)

    # Add cycle to close prices (scaled by price level)
    df['Close'] = df['Close'] * (1 + 0.02 * cycle)

    # Recalculate high/low based on new close
    df['High'] = np.maximum(df['High'], df['Close'])
    df['Low'] = np.minimum(df['Low'], df['Close'])

    # Add indicators
    df = _add_synthetic_indicators(df, seed=seed)

    # Binary target: next bar up (1) or down (0)
    df['target'] = (df['Close'].shift(-1) > df['Close']).astype(int)

    # Fill NaN from shift
    df = df.dropna()

    # Verify balance
    balance = df['target'].mean()
    assert 0.35 <= balance <= 0.65, f"Dataset not balanced: {balance:.2%} positive"

    return df.reset_index(drop=True)


def generate_imbalanced_binary(
    n_rows: int = 1000,
    positive_ratio: float = 0.1,
    seed: int = 42
) -> pd.DataFrame:
    """
    Generate imbalanced binary classification dataset.

    Creates a random walk with rare "spike" events that serve as the positive class.
    This simulates rare trading signals (e.g., trend reversals).

    This dataset tests:
    - Focal loss effectiveness
    - Class weighting
    - Model's ability to avoid all-zeros prediction

    Args:
        n_rows: Number of rows to generate
        positive_ratio: Target ratio of positive class (0.0-1.0)
        seed: Random seed for reproducibility

    Returns:
        DataFrame with OHLCV, indicators, and 'target' column
    """
    np.random.seed(seed)

    # Generate base data
    df = _generate_base_ohlcv(n_rows, seed=seed)

    # Add indicators
    df = _add_synthetic_indicators(df, seed=seed)

    # Create imbalanced target: rare "spike" signals
    # Use a combination of price and indicator conditions
    n_positive = int(n_rows * positive_ratio)

    # Select random indices for positive class
    positive_indices = np.random.choice(n_rows, size=n_positive, replace=False)
    df['target'] = 0
    df.loc[positive_indices, 'target'] = 1

    # Make the positive signals somewhat learnable:
    # Add a pattern where positive signals tend to have high RSI or volume spikes
    for idx in positive_indices:
        if idx > 0 and idx < n_rows - 1:
            # Spike in volume before signal (cast to int to match dtype)
            df.loc[idx, 'Volume'] = int(df['Volume'].mean() * 2.5)
            # RSI tends to be extreme
            if np.random.random() > 0.5:
                df.loc[idx, 'RSI_14'] = min(100, df.loc[idx, 'RSI_14'] + 20)
            else:
                df.loc[idx, 'RSI_14'] = max(0, df.loc[idx, 'RSI_14'] - 20)

    # Verify imbalance
    actual_ratio = df['target'].mean()
    assert 0.05 <= actual_ratio <= 0.20, f"Dataset ratio unexpected: {actual_ratio:.2%}"

    return df.reset_index(drop=True)


def generate_multiclass(
    n_rows: int = 1000,
    n_classes: int = 3,
    seed: int = 42
) -> pd.DataFrame:
    """
    Generate multiclass classification dataset.

    Creates alternating "regimes" representing different market conditions:
    - Class 0: Bearish (downtrend)
    - Class 1: Neutral (sideways)
    - Class 2: Bullish (uptrend)

    Each regime lasts 10-50 bars, creating learnable patterns.

    Args:
        n_rows: Number of rows to generate
        n_classes: Number of classes (default 3)
        seed: Random seed for reproducibility

    Returns:
        DataFrame with OHLCV, indicators, and 'target' column (0, 1, or 2)
    """
    np.random.seed(seed)

    # Generate base dates and volume
    start_date = datetime(2020, 1, 1, 9, 30)
    dates = [start_date + timedelta(hours=i) for i in range(n_rows)]
    base_volume = 1000000
    volume = base_volume * (1 + np.random.uniform(-0.5, 0.5, n_rows))

    # Generate regime-based prices
    prices = np.zeros(n_rows)
    targets = np.zeros(n_rows, dtype=int)

    current_price = 100.0
    i = 0

    while i < n_rows:
        # Pick regime length and type
        regime_length = np.random.randint(10, 50)
        regime_type = np.random.randint(0, n_classes)

        # Trend parameters per regime
        if regime_type == 0:  # Bearish
            daily_return = -0.002
            volatility = 0.015
        elif regime_type == 1:  # Neutral
            daily_return = 0.0
            volatility = 0.01
        else:  # Bullish
            daily_return = 0.002
            volatility = 0.015

        for j in range(regime_length):
            if i + j >= n_rows:
                break

            # Generate price with trend + noise
            returns = daily_return + np.random.normal(0, volatility)
            current_price = current_price * (1 + returns)
            prices[i + j] = current_price
            targets[i + j] = regime_type

        i += regime_length

    # Build DataFrame
    close = prices
    high = close * (1 + np.abs(np.random.normal(0, 0.005, n_rows)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, n_rows)))
    open_prices = np.roll(close, 1)
    open_prices[0] = 100.0

    df = pd.DataFrame({
        'Date': dates,
        'Open': open_prices,
        'High': high,
        'Low': low,
        'Close': close,
        'Volume': volume.astype(int),
        'target': targets
    })

    # Add indicators
    df = _add_synthetic_indicators(df, seed=seed)

    # Verify class distribution (each class should have some samples)
    class_counts = df['target'].value_counts()
    for c in range(n_classes):
        count = class_counts.get(c, 0)
        assert count >= 10, f"Class {c} has too few samples: {count}"

    return df.reset_index(drop=True)


if __name__ == '__main__':
    # Test generators
    print("Testing synthetic data generators...")

    balanced = generate_balanced_binary(1000)
    print(f"Balanced binary: {len(balanced)} rows, {balanced['target'].mean():.2%} positive")
    print(f"  Columns: {list(balanced.columns)}")

    imbalanced = generate_imbalanced_binary(1000, positive_ratio=0.1)
    print(f"Imbalanced binary: {len(imbalanced)} rows, {imbalanced['target'].mean():.2%} positive")

    multiclass = generate_multiclass(1000, n_classes=3)
    print(f"Multiclass: {len(multiclass)} rows")
    print(f"  Class distribution: {dict(multiclass['target'].value_counts())}")

    print("\nAll generators working correctly!")
