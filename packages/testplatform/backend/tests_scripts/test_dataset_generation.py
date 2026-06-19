#!/usr/bin/env python3
"""
Test script for dataset generation.

Tests the full dataset generation pipeline including:
- Technical indicators
- Fundamentals (statement-based with lookback)
- Macro data
- Sentiment analysis with news parsing

Run from backend directory:
    python scripts/test_dataset_generation.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import logging

# Setup logging
from app.logging_config import setup_logging
setup_logging(console_level=logging.INFO)

logger = logging.getLogger(__name__)

# Import services (same as datasets.py)
from app.indicators import TechnicalIndicators
from app.services.fundamentals import FundamentalsService
from app.services.macro import MacroService
from app.services.sentiment import SentimentService
from ba2_providers.ohlcv.YFinanceDataProvider import YFinanceDataProvider


def generate_test_dataset():
    """Generate a test dataset with all features."""

    # Check for API keys
    fred_api_key = os.getenv('FRED_API_KEY')
    has_fred_key = bool(fred_api_key)
    if not has_fred_key:
        print("⚠ FRED_API_KEY not set - macro indicators will be skipped")

    # Configuration
    ticker = "AAPL"
    timeframe = "4h"
    dataset_start_date = datetime.now() - timedelta(days=90)  # 3 months of dataset
    end_date = datetime.now()

    # Technical indicators config
    technical_indicators = [
        {"type": "sma", "period": 10, "timeframe": "4h"},
        {"type": "sma", "period": 20, "timeframe": "4h"},
        {"type": "sma", "period": 50, "timeframe": "4h"},
        {"type": "sma", "period": 100, "timeframe": "4h"},
        {"type": "sma", "period": 200, "timeframe": "4h"},
        {"type": "ema", "period": 12, "timeframe": "4h"},
        {"type": "ema", "period": 26, "timeframe": "4h"},
        {"type": "ema", "period": 50, "timeframe": "4h"},
        {"type": "ema", "period": 100, "timeframe": "4h"},
        {"type": "ema", "period": 200, "timeframe": "4h"},
        {"type": "rsi", "period": 14, "timeframe": "4h"},
        {"type": "macd", "fast": 12, "slow": 26, "signal": 9, "timeframe": "4h"},
        {"type": "bbands", "period": 20, "std_dev": 2, "timeframe": "4h"},
        {"type": "atr", "period": 14, "timeframe": "4h"},
        {"type": "stochastic", "k_period": 14, "d_period": 3, "smooth_k": 3, "timeframe": "4h"}
    ]

    # Calculate warmup period needed for indicators
    # Find max period from all indicators
    max_period = 0
    for ind in technical_indicators:
        period = ind.get('period', 0)
        slow = ind.get('slow', 0)  # For MACD
        max_period = max(max_period, period, slow)

    # Calculate warmup days based on timeframe
    # For intraday, consider market hours only (6.5 hours/day for US stocks)
    # 1h = ~7 bars/day during market hours
    # 4h = ~2 bars/day during market hours (9:30-4:00 = 6.5 hrs / 4 = 1.6, round to 2)
    bars_per_day = {
        '1m': 390,  # 6.5 hrs * 60
        '5m': 78,   # 6.5 hrs * 12
        '15m': 26,  # 6.5 hrs * 4
        '30m': 13,  # 6.5 hrs * 2
        '1h': 7,    # ~7 bars during market hours
        '4h': 2,    # ~2 bars during market hours
        '1d': 1,
        '1w': 1/5,  # 1 bar per 5 trading days
    }.get(timeframe, 1)
    # Need warmup bars to ensure all dataset rows have indicator values
    # SMA N needs N-1 bars before producing first value, so we need N bars of warmup
    # Add extra buffer for weekends, holidays, and missing data
    warmup_bars_needed = max_period + 50  # Extra buffer for data gaps
    warmup_days = int(warmup_bars_needed / bars_per_day * 1.5)  # 1.5x for weekends/holidays

    # Fetch data starting earlier for warmup
    fetch_start_date = dataset_start_date - timedelta(days=warmup_days)
    print(f"Warmup: fetching {warmup_days} extra days for {max_period}-period indicators")

    # Fundamentals config
    fundamentals_config = {
        "enabled": True,
        "statement_types": ["fcf", "pe", "eps", "revenue", "debt_equity", "roe",
                          "balance_sheet", "income_statement", "cash_flow", "earnings"],
        "lookback_statements": 2,
        "macro_indicators": ["interest_rate", "gdp", "inflation", "unemployment"] if has_fred_key else [],
        "fundamentals_providers": ["yfinance", "fmp"],
        "macro_provider": "fred"
    }

    # Sentiment config
    sentiment_config = {
        "enabled": True,
        "news_sources": ["fmp_news", "finnhub_news"],  # Removed alphavantage (25/day limit)
        "lookback_periods": ["1d", "1w", "1m", "6m"],
        "sentiment_categories": ["positive", "neutral", "negative"],
        "enrich_content": True
    }

    print("=" * 80)
    print("DATASET GENERATION TEST")
    print("=" * 80)
    print(f"Ticker: {ticker}")
    print(f"Timeframe: {timeframe}")
    print(f"Dataset date range: {dataset_start_date.date()} to {end_date.date()}")
    print(f"Fetch date range: {fetch_start_date.date()} to {end_date.date()} (includes warmup)")
    print()

    # Step 1: Fetch OHLC data (with warmup period)
    print("[1/5] Fetching OHLC data...")
    try:
        provider = YFinanceDataProvider()

        # Timeframe mapping
        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1wk", "1mo": "1mo"
        }
        interval = interval_map.get(timeframe, "1d")

        data_points = provider.get_data(
            symbol=ticker,
            start_date=fetch_start_date,  # Use fetch_start_date for warmup
            end_date=end_date,
            interval=interval
        )

        if not data_points:
            print(f"  ✗ No data available for {ticker}")
            return None

        # Convert to DataFrame
        df = pd.DataFrame([{
            'Date': dp.timestamp,
            'Open': dp.open,
            'High': dp.high,
            'Low': dp.low,
            'Close': dp.close,
            'Volume': dp.volume
        } for dp in data_points])

        df = df.sort_values('Date').reset_index(drop=True)
        print(f"  ✓ Fetched {len(df)} rows of OHLC data")
        print(f"  ✓ Columns: {list(df.columns)}")
    except Exception as e:
        print(f"  ✗ OHLC fetch failed: {e}")
        import traceback
        traceback.print_exc()
        return None

    # Step 2: Add technical indicators
    print("\n[2/5] Adding technical indicators...")
    try:
        # Convert list format to dict format (same as datasets.py)
        indicators_dict = {}
        for indicator in technical_indicators:
            indicator_type = indicator.get('type', indicator.get('name', 'unknown'))
            indicator_name = indicator.get('name', f"{indicator_type}_{indicator.get('period', '')}")
            indicators_dict[indicator_name] = indicator

        df = TechnicalIndicators.add_indicators_to_dataframe(df, indicators_dict)
        indicator_cols = [c for c in df.columns if c not in ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']]
        print(f"  ✓ Added {len(indicator_cols)} indicator columns")
        if indicator_cols:
            print(f"  ✓ Sample: {indicator_cols[:5]}...")
    except Exception as e:
        print(f"  ✗ Indicator calculation failed: {e}")
        import traceback
        traceback.print_exc()

    # Step 3: Add sentiment features
    print("\n[3/5] Adding sentiment features...")
    if sentiment_config.get('enabled'):
        try:
            sentiment_service = SentimentService()

            # Get news sources
            news_sources = sentiment_config.get('news_sources', [])
            if not news_sources:
                news_sources = ['fmp']

            print(f"  Fetching news from {len(news_sources)} source(s): {news_sources}")

            all_articles = []
            for source in news_sources:
                provider_name = source.replace('_news', '').replace('_company', '').replace('_global', '')
                try:
                    print(f"    Fetching from {provider_name}...")
                    # Only fetch news for the actual dataset range, not warmup period
                    articles = sentiment_service.fetch_news_for_ticker(
                        ticker=ticker,
                        start_date=dataset_start_date,  # Use dataset start, not df.min() which includes warmup
                        end_date=end_date,
                        provider=provider_name,
                        enrich_content=sentiment_config.get('enrich_content', True)
                    )
                    if articles:
                        print(f"    ✓ Got {len(articles)} articles from {provider_name}")
                        all_articles.extend(articles)
                    else:
                        print(f"    ⚠ No articles from {provider_name}")
                except Exception as e:
                    print(f"    ✗ Error fetching from {provider_name}: {e}")

            if all_articles:
                print(f"  Total articles: {len(all_articles)}")
                df = sentiment_service.create_sentiment_features(df, all_articles)
                sentiment_cols = [c for c in df.columns if c.startswith('news_')]
                print(f"  ✓ Added {len(sentiment_cols)} sentiment columns")
            else:
                print("  ⚠ No articles found for sentiment analysis")
        except Exception as e:
            print(f"  ✗ Sentiment analysis failed: {e}")
            import traceback
            traceback.print_exc()

    # Step 4: Add fundamentals
    print("\n[4/5] Adding fundamentals...")
    if fundamentals_config.get('enabled'):
        try:
            # Statement-based features
            statement_types = fundamentals_config.get('statement_types', [])
            if statement_types:
                lookback = fundamentals_config.get('lookback_statements', 2)
                providers = fundamentals_config.get('fundamentals_providers', ['yfinance'])

                print(f"  Creating statement features...")
                print(f"  Lookback: {lookback}, Providers: {providers}")

                df = FundamentalsService.create_statement_features(
                    df=df,
                    ticker=ticker,
                    statement_types=statement_types,
                    lookback_statements=lookback,
                    providers=providers,
                    frequency='quarterly'
                )

                fundamental_cols = [c for c in df.columns if any(c.startswith(p) for p in ['bs_', 'is_', 'cf_', 'earn_'])]
                print(f"  ✓ Added {len(fundamental_cols)} fundamental columns")
        except Exception as e:
            print(f"  ✗ Fundamentals failed: {e}")
            import traceback
            traceback.print_exc()

        # Macro indicators
        macro_indicators = fundamentals_config.get('macro_indicators', [])
        if macro_indicators:
            try:
                print(f"  Adding macro indicators: {macro_indicators}")
                macro_service = MacroService()
                df = macro_service.integrate_macro_with_ohlc(df, macro_indicators)

                # Rename columns to have macro_ prefix
                for indicator in macro_indicators:
                    if indicator in df.columns:
                        df = df.rename(columns={indicator: f'macro_{indicator}'})
                        if f'{indicator}_yoy_change' in df.columns:
                            df = df.rename(columns={f'{indicator}_yoy_change': f'macro_{indicator}_yoy_change'})

                macro_cols = [c for c in df.columns if c.startswith('macro_')]
                print(f"  ✓ Added {len(macro_cols)} macro columns")
            except Exception as e:
                print(f"  ✗ Macro data failed: {e}")
                import traceback
                traceback.print_exc()

    # Step 5: Filter out warmup rows and validate
    print("\n[5/5] Filtering warmup rows and validating dataset...")

    # Filter to only include rows from dataset_start_date onwards
    rows_before = len(df)
    df['Date'] = pd.to_datetime(df['Date'])
    dataset_start_ts = pd.to_datetime(dataset_start_date)
    # Handle timezone-aware dates
    if df['Date'].dt.tz is not None:
        dataset_start_ts = dataset_start_ts.tz_localize(df['Date'].dt.tz)
    df = df[df['Date'] >= dataset_start_ts].reset_index(drop=True)
    rows_after = len(df)
    print(f"  Filtered {rows_before - rows_after} warmup rows, {rows_after} rows remaining")

    # Check for expected columns
    expected_prefixes = {
        'Technical': ['sma_', 'ema_', 'rsi_', 'macd_', 'bbands_', 'atr_', 'stochastic_'],
        'Sentiment': ['news_'],
        'Fundamentals': ['bs_', 'is_', 'cf_', 'earn_'],
    }
    # Only validate macro if FRED API key was available
    if has_fred_key:
        expected_prefixes['Macro'] = ['macro_']

    validation_results = {}
    for category, prefixes in expected_prefixes.items():
        cols = [c for c in df.columns if any(c.startswith(p) for p in prefixes)]
        non_null_cols = [c for c in cols if df[c].notna().any()]
        validation_results[category] = {
            'total': len(cols),
            'with_data': len(non_null_cols),
            'columns': cols[:5]  # First 5 for display
        }

    print("\nValidation Results:")
    print("-" * 60)
    all_valid = True
    for category, result in validation_results.items():
        status = "✓" if result['with_data'] > 0 else "✗"
        if result['with_data'] == 0 and result['total'] > 0:
            all_valid = False
        print(f"  {status} {category}: {result['with_data']}/{result['total']} columns with data")
        if result['columns']:
            print(f"      Sample: {result['columns']}")

    print("-" * 60)
    print(f"\nTotal columns: {len(df.columns)}")
    print(f"Total rows: {len(df)}")

    # Check for NaN values
    nan_counts = df.isna().sum()
    cols_with_nan = nan_counts[nan_counts > 0]
    if len(cols_with_nan) > 0:
        print(f"\nColumns with NaN values: {len(cols_with_nan)}")
        # Show first few
        for col, count in list(cols_with_nan.items())[:5]:
            pct = count / len(df) * 100
            print(f"  {col}: {count} ({pct:.1f}%)")

    # Save test dataset
    output_path = Path("test_output")
    output_path.mkdir(exist_ok=True)
    output_file = output_path / f"test_dataset_{ticker}_{timeframe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(output_file, index=False)
    print(f"\nSaved dataset to: {output_file}")

    print("\n" + "=" * 80)
    if all_valid:
        print("TEST PASSED: All expected column categories have data")
    else:
        print("TEST FAILED: Some column categories are missing data")
    print("=" * 80)

    return df


if __name__ == "__main__":
    generate_test_dataset()
