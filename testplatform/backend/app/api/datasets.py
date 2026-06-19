"""
Dataset API endpoints
Updated for preview endpoint and Parquet export
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List
import logging
import os
from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path
import threading
import concurrent.futures

from app.models.database import get_db, SessionLocal
from app.paths import DATASETS_DIR
from app.models.dataset import Dataset, DatasetStatus
from app.schemas.dataset import DatasetCreate, DatasetResponse, DatasetListResponse, DatasetUpdate, DatasetDuplicate, DatasetRegenerate, BatchRegenerateRequest
from app.indicators import TechnicalIndicators
from app.services.fundamentals import FundamentalsService
from app.services.macro import MacroService
from app.services.sentiment import SentimentService
from app.services.indicators import IndicatorService
from app.services.dataset_handler import (
    add_time_features,
    apply_technical_indicators,
    calculate_warmup_period,
    INTERVAL_MAP,
    BARS_PER_DAY,
)


def get_ohlcv_provider(provider_name: str = "yfinance"):
    """Get the appropriate OHLCV provider from the shared ba2_providers registry.

    The shared providers expose ``get_data(...) -> List[MarketDataPoint]``
    natively (the same public contract the local providers had), so the dataset
    builder (_build_dataset_in_background) reads ``.timestamp/.open/.high/.low/
    .close/.volume`` off the returned points unchanged.

    The shared providers do not carry the backend's parquet-backed, gap-filling
    OHLCV disk-cache layer (``extend_ohlcv_cache`` / ``_get_cache_file``) that the
    OHLCV cache-fetch background task relies on, so the returned provider is
    augmented with that layer via ``wrap_with_cache`` (see
    ``app.services.ohlcv_cache_provider``). The public fetch contract is unchanged.
    """
    from ba2_providers import get_provider
    from app.services.ohlcv_cache_provider import wrap_with_cache
    name = (provider_name or "yfinance").lower()
    if name == "yf":
        name = "yfinance"
    return wrap_with_cache(get_provider("ohlcv", name))

logger = logging.getLogger(__name__)

# Timeframe configuration for multi-timeframe indicators
SUPPORTED_TIMEFRAMES = ["15m", "1h", "4h", "1d"]
from typing import Dict, Any, Optional
TIMEFRAME_INTERVAL_MAP = {
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "D1": "1d"
}

# Default indicators configuration for each timeframe
DEFAULT_INDICATORS = {
    "sma_20": {"type": "sma", "period": 20},
    "sma_50": {"type": "sma", "period": 50},
    "ema_12": {"type": "ema", "period": 12},
    "ema_26": {"type": "ema", "period": 26},
    "rsi_14": {"type": "rsi", "period": 14},
    "macd": {"type": "macd", "fast": 12, "slow": 26, "signal": 9},
    "bbands": {"type": "bollinger", "period": 20, "std_dev": 2.0},
    "atr_14": {"type": "atr", "period": 14},
    "stoch": {"type": "stochastic", "k_period": 14, "d_period": 3, "smooth_k": 3}
}

router = APIRouter()

# Thread pool for background dataset generation (increased to 5 for parallel operations)
_dataset_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="dataset_gen")


def check_dataset_compatibility(dataframes: list) -> dict:
    """Check if multiple DataFrames have identical columns in the same order."""
    if len(dataframes) <= 1:
        return {'compatible': True, 'message': 'Single dataset is always compatible',
                'common_columns': list(dataframes[0].columns) if dataframes else []}

    reference_cols = list(dataframes[0].columns)
    for i, df in enumerate(dataframes[1:], 1):
        current_cols = list(df.columns)
        if current_cols != reference_cols:
            missing = set(reference_cols) - set(current_cols)
            extra = set(current_cols) - set(reference_cols)
            order_diff = current_cols != reference_cols and set(current_cols) == set(reference_cols)
            parts = []
            if missing:
                parts.append(f"missing columns: {missing}")
            if extra:
                parts.append(f"extra columns: {extra}")
            if order_diff:
                parts.append("column order differs")
            return {
                'compatible': False,
                'message': f"Dataset {i+1} incompatible: {'; '.join(parts)}",
                'reference_columns': reference_cols,
                'dataset_columns': current_cols
            }

    return {'compatible': True, 'message': 'All datasets compatible',
            'common_columns': reference_cols}


def calculate_regen_flags(old_config: Dict[str, Any], new_config: Dict[str, Any]) -> DatasetRegenerate:
    """
    Calculate which dataset components need regeneration based on config differences.

    Performs a diff between old and new configuration to determine the minimal
    set of regeneration flags needed. This avoids unnecessary refetching of
    expensive data (e.g., sentiment, fundamentals) when only indicators changed.

    Args:
        old_config: Current dataset configuration
        new_config: New/updated dataset configuration

    Returns:
        DatasetRegenerate with appropriate flags set
    """
    flags = DatasetRegenerate(
        regenerate_ohlcv=False,
        regenerate_technical=False,
        regenerate_sentiment=False,
        regenerate_fundamentals=False,
        regenerate_macro=False
    )

    # Ticker change = full regeneration
    if old_config.get('ticker') != new_config.get('ticker'):
        flags.regenerate_ohlcv = True
        flags.regenerate_technical = True
        flags.regenerate_sentiment = True
        flags.regenerate_fundamentals = True
        flags.regenerate_macro = True
        return flags

    # Data provider change = refetch OHLCV
    if old_config.get('data_provider') != new_config.get('data_provider'):
        flags.regenerate_ohlcv = True
        flags.regenerate_technical = True  # Recalc on new data

    # Date range expanding = refetch OHLCV
    old_start = old_config.get('start_date')
    new_start = new_config.get('start_date')
    old_end = old_config.get('end_date')
    new_end = new_config.get('end_date')

    if old_start and new_start:
        # Convert to comparable format
        try:
            old_start_dt = datetime.strptime(old_start, "%Y-%m-%d") if isinstance(old_start, str) else old_start
            new_start_dt = datetime.strptime(new_start, "%Y-%m-%d") if isinstance(new_start, str) else new_start
            if new_start_dt < old_start_dt:
                flags.regenerate_ohlcv = True
                flags.regenerate_technical = True
        except (ValueError, TypeError):
            pass

    if old_end and new_end:
        try:
            old_end_dt = datetime.strptime(old_end, "%Y-%m-%d") if isinstance(old_end, str) else old_end
            new_end_dt = datetime.strptime(new_end, "%Y-%m-%d") if isinstance(new_end, str) else new_end
            if new_end_dt > old_end_dt:
                flags.regenerate_ohlcv = True
                flags.regenerate_technical = True
        except (ValueError, TypeError):
            pass

    # Date range shrinking = recalc indicators (different lookback context)
    if not flags.regenerate_ohlcv and (old_start != new_start or old_end != new_end):
        flags.regenerate_technical = True

    # Indicator changes - also regenerate OHLCV to get warmup data for new indicators
    old_indicators = old_config.get('technical_indicators', [])
    new_indicators = new_config.get('technical_indicators', [])
    if old_indicators != new_indicators:
        flags.regenerate_technical = True
        flags.regenerate_ohlcv = True  # Need warmup data for new indicators

    # Sentiment config changes
    old_sentiment = old_config.get('sentiment_config', {})
    new_sentiment = new_config.get('sentiment_config', {})
    if old_sentiment != new_sentiment:
        flags.regenerate_sentiment = True

    # Fundamentals config changes
    old_fundamentals = old_config.get('fundamentals_config', {})
    new_fundamentals = new_config.get('fundamentals_config', {})
    if old_fundamentals != new_fundamentals:
        flags.regenerate_fundamentals = True

    # Macro config changes (if applicable)
    old_macro = old_config.get('macro_config', {})
    new_macro = new_config.get('macro_config', {})
    if old_macro != new_macro:
        flags.regenerate_macro = True

    return flags


def _build_dataset_in_background(dataset_id: int, dataset_config: dict):
    """
    Build dataset in a background thread.

    This function runs in a separate thread with its own database session
    to avoid blocking the main event loop and database connection pool.

    Args:
        dataset_id: ID of the dataset record (already created in BUILDING status)
        dataset_config: Dictionary containing all dataset creation parameters
    """
    # Create a new database session for this thread
    db = SessionLocal()

    try:
        logger.info(f"[Thread] Starting background build for dataset {dataset_id}")

        # Load the dataset record
        db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not db_dataset:
            logger.error(f"[Thread] Dataset {dataset_id} not found")
            return

        # Extract config
        ticker = dataset_config['ticker']
        timeframe = dataset_config['timeframe']
        start_date = datetime.strptime(dataset_config['start_date'], "%Y-%m-%d") if dataset_config.get('start_date') else datetime.now() - timedelta(days=365)
        end_date = datetime.strptime(dataset_config['end_date'], "%Y-%m-%d") if dataset_config.get('end_date') else datetime.now()
        provider_name = dataset_config.get('data_provider') or "yfinance"
        technical_indicators = dataset_config.get('technical_indicators', [])
        sentiment_config = dataset_config.get('sentiment_config', {})
        fundamentals_config = dataset_config.get('fundamentals_config', {})
        file_path = Path(db_dataset.file_path)

        # Convert timeframe to interval format
        interval = INTERVAL_MAP.get(timeframe, "1d")

        # Calculate warmup period needed for indicators
        warmup_bars, warmup_days = calculate_warmup_period(technical_indicators, timeframe)

        # Store original requested start date for filtering later
        requested_start_date = start_date

        # Adjust fetch start date to include warmup period
        fetch_start_date = start_date - timedelta(days=warmup_days) if warmup_days > 0 else start_date
        if warmup_days > 0:
            logger.info(f"[Thread] Warmup: fetching {warmup_days} extra days for {warmup_bars}-bar indicators")

        # Fetch OHLC data
        provider = get_ohlcv_provider(provider_name)
        logger.info(f"[Thread] Fetching data from {fetch_start_date.date()} to {end_date.date()} using {provider_name}")

        data_points = provider.get_data(
            symbol=ticker,
            start_date=fetch_start_date,
            end_date=end_date,
            interval=interval
        )

        if not data_points:
            db_dataset.status = DatasetStatus.ERROR.value
            db_dataset.error_message = f"No data available for {ticker}"
            db.commit()
            logger.error(f"[Thread] No data for {ticker}")
            return

        # Convert to DataFrame
        df = pd.DataFrame([{
            'Date': dp.timestamp, 'Open': dp.open, 'High': dp.high,
            'Low': dp.low, 'Close': dp.close, 'Volume': dp.volume
        } for dp in data_points])
        df = df.sort_values('Date').reset_index(drop=True)

        # Add time-based features (day_of_week, hour_of_day)
        df = add_time_features(df)

        logger.info(f"[Thread] Fetched {len(df)} OHLC data points")

        # Validate date range (with 5-day tolerance)
        # Use fetch_start_date for validation since we may have requested earlier data for warmup
        data_start = df['Date'].min().date() if hasattr(df['Date'].min(), 'date') else df['Date'].min()
        data_end = df['Date'].max().date() if hasattr(df['Date'].max(), 'date') else df['Date'].max()
        fetch_start = fetch_start_date.date() if hasattr(fetch_start_date, 'date') else fetch_start_date
        req_end = end_date.date() if hasattr(end_date, 'date') else end_date

        if (data_start - fetch_start).days > 5:
            db_dataset.status = DatasetStatus.ERROR.value
            db_dataset.error_message = f"Data starts at {data_start}, but requested {fetch_start} (including warmup)"
            db.commit()
            return

        if (req_end - data_end).days > 5:
            db_dataset.status = DatasetStatus.ERROR.value
            db_dataset.error_message = f"Data ends at {data_end}, but requested {req_end}"
            db.commit()
            return

        # Apply technical indicators
        if technical_indicators:
            logger.info(f"[Thread] Applying {len(technical_indicators)} technical indicators...")
            try:
                df = apply_technical_indicators(df, technical_indicators)
                logger.info(f"[Thread] Added technical indicators. {len(df.columns)} columns")
            except Exception as e:
                logger.error(f"[Thread] Error applying indicators: {e}")

        # Fetch sentiment features
        if sentiment_config and sentiment_config.get('enabled'):
            logger.info("[Thread] Fetching sentiment data...")
            try:
                sentiment_service = SentimentService()
                news_sources = sentiment_config.get('news_sources', [])
                if not news_sources:
                    news_sources = [sentiment_config.get('provider', 'fmp')]

                all_articles = []
                use_cached_news = sentiment_config.get('use_cached_news', False)

                for source in news_sources:
                    source_provider = source.replace('_news', '').replace('_company', '').replace('_global', '')
                    try:
                        if use_cached_news:
                            from app.services.news_cache import NewsCacheService
                            cache_service = NewsCacheService()
                            sd = df['Date'].min()
                            ed = df['Date'].max()
                            sd = sd.to_pydatetime() if hasattr(sd, 'to_pydatetime') else sd
                            ed = ed.to_pydatetime() if hasattr(ed, 'to_pydatetime') else ed
                            articles = cache_service.get_cached_articles_for_ticker(
                                ticker=ticker, provider=source_provider,
                                start_date=sd, end_date=ed
                            )
                        else:
                            articles = sentiment_service.fetch_news_for_ticker(
                                ticker=ticker,
                                start_date=df['Date'].min() if hasattr(df['Date'].min(), 'to_pydatetime') else start_date,
                                end_date=df['Date'].max() if hasattr(df['Date'].max(), 'to_pydatetime') else end_date,
                                provider=source_provider,
                                enrich_content=sentiment_config.get('enrich_content', True)
                            )
                        if articles:
                            logger.info(f"[Thread] {'Loaded cached' if use_cached_news else 'Fetched'} {len(articles)} articles from {source_provider}")
                            all_articles.extend(articles)
                    except Exception as e:
                        logger.warning(f"[Thread] Error {'loading cached' if use_cached_news else 'fetching'} from {source_provider}: {e}")

                if all_articles:
                    logger.info(f"[Thread] Creating sentiment features from {len(all_articles)} articles")
                    df = sentiment_service.create_sentiment_features(df, all_articles)
            except Exception as e:
                logger.error(f"[Thread] Error fetching sentiment: {e}")

        # Fetch fundamentals
        if fundamentals_config and fundamentals_config.get('enabled'):
            logger.info("[Thread] Fetching fundamentals data...")
            try:
                statement_types = fundamentals_config.get('statement_types')
                if statement_types:
                    providers = fundamentals_config.get('fundamentals_providers', ['yfinance'])
                    df = FundamentalsService.create_statement_features_v2(
                        df=df, ticker=ticker, statement_types=statement_types,
                        providers=providers, frequency='quarterly'
                    )
                else:
                    fundamentals = FundamentalsService.get_fundamental_data(ticker)
                    if fundamentals and fundamentals.get('current'):
                        for key, value in fundamentals['current'].items():
                            if value is not None:
                                df[f'fundamental_{key}'] = value

                # Macro indicators
                macro_indicators = fundamentals_config.get('macro_indicators', [])
                if macro_indicators:
                    macro_service = MacroService()
                    df = macro_service.integrate_macro_with_ohlc(df, macro_indicators)
                    for indicator in macro_indicators:
                        if indicator in df.columns:
                            df = df.rename(columns={indicator: f'macro_{indicator}'})
                            if f'{indicator}_yoy_change' in df.columns:
                                df = df.rename(columns={f'{indicator}_yoy_change': f'macro_{indicator}_yoy_change'})
                            if f'{indicator}_days_since' in df.columns:
                                df = df.rename(columns={f'{indicator}_days_since': f'macro_{indicator}_days_since'})
            except Exception as e:
                logger.error(f"[Thread] Error fetching fundamentals: {e}")

        # Filter out warmup rows - only keep data from the originally requested start date
        if warmup_days > 0:
            rows_before = len(df)
            df['Date'] = pd.to_datetime(df['Date'])
            requested_start_ts = pd.to_datetime(requested_start_date)
            # Handle timezone-aware dates
            if df['Date'].dt.tz is not None:
                requested_start_ts = requested_start_ts.tz_localize(df['Date'].dt.tz)
            df = df[df['Date'] >= requested_start_ts].reset_index(drop=True)
            rows_filtered = rows_before - len(df)
            logger.info(f"[Thread] Filtered {rows_filtered} warmup rows, {len(df)} rows remaining")

        # Save dataset
        df.to_csv(file_path, index=False)
        logger.info(f"[Thread] Saved dataset to {file_path} with {len(df.columns)} columns")

        # Update dataset record
        db_dataset.start_date = df['Date'].min()
        db_dataset.end_date = df['Date'].max()
        db_dataset.rows_count = len(df)
        db_dataset.status = DatasetStatus.READY.value
        db_dataset.error_message = None
        db.commit()

        logger.info(f"[Thread] Dataset {dataset_id} is now READY with {len(df)} rows")

    except Exception as e:
        logger.error(f"[Thread] Error building dataset {dataset_id}: {e}", exc_info=True)
        try:
            db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if db_dataset:
                db_dataset.status = DatasetStatus.ERROR.value
                db_dataset.error_message = str(e)[:500]
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def _regenerate_dataset_in_background(dataset_id: int, regen_config: dict):
    """
    Regenerate dataset in a background thread.

    Args:
        dataset_id: Dataset ID to regenerate
        regen_config: Dict containing all config needed for regeneration:
            - regen_options: DatasetRegenerate flags
            - ticker, timeframe, file_path
            - technical_indicators, fundamentals_config, sentiment_config
            - gen_config, start_date, end_date
    """
    db = SessionLocal()
    try:
        logger.info(f"[Thread] Starting background regeneration for dataset {dataset_id}")

        # Extract config
        regen_options = regen_config['regen_options']
        ticker = regen_config['ticker']
        timeframe = regen_config['timeframe']
        file_path = regen_config['file_path']
        technical_indicators = regen_config.get('technical_indicators')
        fundamentals_config = regen_config.get('fundamentals_config')
        sentiment_config = regen_config.get('sentiment_config')
        gen_config = regen_config.get('gen_config', {})
        start_date = regen_config['start_date']
        end_date = regen_config['end_date']

        # Calculate warmup period needed for indicators
        warmup_bars, warmup_days = calculate_warmup_period(technical_indicators or [], timeframe)
        requested_start_date = start_date
        fetch_start_date = start_date - timedelta(days=warmup_days) if warmup_days > 0 else start_date

        if warmup_days > 0:
            logger.info(f"[Thread] Warmup: fetching {warmup_days} extra days for {warmup_bars}-bar indicators")

        # Fetch or load data based on regen_options
        if regen_options.regenerate_ohlcv:
            provider_name = gen_config.get("data_provider", "yfinance")
            provider = get_ohlcv_provider(provider_name)
            logger.info(f"[Thread] Fetching data from {fetch_start_date.date()} to {end_date.date()} using {provider_name}")

            interval = INTERVAL_MAP.get(timeframe, "1d")

            data_points = provider.get_data(
                symbol=ticker,
                start_date=fetch_start_date,
                end_date=end_date,
                interval=interval
            )

            if not data_points:
                db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
                if db_dataset:
                    db_dataset.status = DatasetStatus.ERROR.value
                    db_dataset.error_message = f"No data available for {ticker}"
                    db.commit()
                return

            df = pd.DataFrame([{
                'Date': dp.timestamp,
                'Open': dp.open,
                'High': dp.high,
                'Low': dp.low,
                'Close': dp.close,
                'Volume': dp.volume
            } for dp in data_points])
            df = df.sort_values('Date').reset_index(drop=True)
            df = add_time_features(df)
            logger.info(f"[Thread] Fetched {len(df)} OHLC data points")
        else:
            # Load existing CSV - start with ALL columns, only drop what we're regenerating
            existing_path = Path(file_path)
            if not existing_path.exists():
                db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
                if db_dataset:
                    db_dataset.status = DatasetStatus.ERROR.value
                    db_dataset.error_message = f"Cannot do partial regeneration: file not found"
                    db.commit()
                return

            df = pd.read_csv(existing_path)
            df['Date'] = pd.to_datetime(df['Date'])
            original_cols = set(df.columns)
            logger.info(f"[Thread] Loaded {len(df)} rows with {len(original_cols)} columns from existing dataset")

            # If regenerating technical indicators, we need warmup data
            # Fetch OHLCV for warmup period if not already present
            if regen_options.regenerate_technical and warmup_days > 0:
                data_start = df['Date'].min()
                # Normalize to tz-naive for comparison
                data_start_naive = data_start.tz_localize(None) if hasattr(data_start, 'tzinfo') and data_start.tzinfo else data_start
                fetch_start_ts = pd.Timestamp(fetch_start_date)
                # Check if we need to fetch warmup data
                if data_start_naive >= fetch_start_ts:
                    logger.info(f"[Thread] Fetching warmup OHLCV from {fetch_start_date.date()} to {data_start_naive.date()}")
                    provider_name = gen_config.get("data_provider", "yfinance")
                    provider = get_ohlcv_provider(provider_name)
                    interval = INTERVAL_MAP.get(timeframe, "1d")

                    warmup_data = provider.get_data(
                        symbol=ticker,
                        start_date=fetch_start_date,
                        end_date=data_start_naive,
                        interval=interval
                    )

                    if warmup_data:
                        warmup_df = pd.DataFrame([{
                            'Date': dp.timestamp,
                            'Open': dp.open,
                            'High': dp.high,
                            'Low': dp.low,
                            'Close': dp.close,
                            'Volume': dp.volume
                        } for dp in warmup_data])
                        warmup_df['Date'] = pd.to_datetime(warmup_df['Date'])
                        # Normalize timezone - strip tz if present for comparison with existing data
                        if warmup_df['Date'].dt.tz is not None:
                            warmup_df['Date'] = warmup_df['Date'].dt.tz_localize(None)
                        warmup_df = warmup_df.sort_values('Date').reset_index(drop=True)
                        warmup_df = add_time_features(warmup_df)

                        # Ensure data_start is also tz-naive for comparison
                        data_start_naive = data_start.tz_localize(None) if data_start.tzinfo else data_start

                        # Remove any overlap with existing data
                        warmup_df = warmup_df[warmup_df['Date'] < data_start_naive]

                        if len(warmup_df) > 0:
                            # Prepend warmup data (only OHLCV columns)
                            ohlcv_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'day_of_week', 'hour_of_day']
                            existing_ohlcv_cols = [c for c in ohlcv_cols if c in df.columns]
                            warmup_df = warmup_df[[c for c in existing_ohlcv_cols if c in warmup_df.columns]]

                            # Prepend warmup rows
                            df = pd.concat([warmup_df, df], ignore_index=True)
                            logger.info(f"[Thread] Added {len(warmup_df)} warmup rows, total {len(df)} rows")

            # Define column patterns for each regeneration type
            ohlcv_time_cols = {'Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'day_of_week', 'hour_of_day'}
            sentiment_prefixes = ('news_',)
            fundamental_prefixes = ('bs_', 'is_', 'cf_', 'earn_', 'fundamental_')
            macro_prefixes = ('macro_',)

            # Helper to identify column type
            def get_column_type(col):
                if col in ohlcv_time_cols:
                    return 'ohlcv'
                if col.startswith(sentiment_prefixes):
                    return 'sentiment'
                if col.startswith(fundamental_prefixes):
                    return 'fundamentals'
                if col.startswith(macro_prefixes):
                    return 'macro'
                return 'technical'  # Everything else is assumed to be technical indicators

            # Drop columns only for components we ARE regenerating
            cols_to_drop = []
            for col in df.columns:
                col_type = get_column_type(col)
                if col_type == 'ohlcv':
                    continue  # Never drop OHLCV
                elif col_type == 'technical' and regen_options.regenerate_technical:
                    cols_to_drop.append(col)
                elif col_type == 'sentiment' and regen_options.regenerate_sentiment:
                    cols_to_drop.append(col)
                elif col_type == 'fundamentals' and regen_options.regenerate_fundamentals:
                    cols_to_drop.append(col)
                elif col_type == 'macro' and regen_options.regenerate_macro:
                    cols_to_drop.append(col)

            if cols_to_drop:
                df = df.drop(columns=cols_to_drop)
                logger.info(f"[Thread] Dropped {len(cols_to_drop)} columns for regeneration")

            preserved_count = len(df.columns) - len(ohlcv_time_cols & set(df.columns))
            if preserved_count > 0:
                logger.info(f"[Thread] Preserved {preserved_count} non-OHLCV columns")

        # Apply technical indicators if configured and regenerate_technical is True
        if technical_indicators and regen_options.regenerate_technical:
            logger.info(f"[Thread] Applying {len(technical_indicators)} technical indicators...")
            try:
                df = apply_technical_indicators(df, technical_indicators)
                logger.info(f"[Thread] Added technical indicators. {len(df.columns)} columns")
            except Exception as e:
                logger.error(f"[Thread] Error applying indicators: {e}")

        # Fetch sentiment if configured and regenerate_sentiment is True
        if sentiment_config and sentiment_config.get('enabled') and regen_options.regenerate_sentiment:
            logger.info("[Thread] Fetching sentiment data...")
            try:
                from app.services.sentiment import SentimentService, get_news_provider
                sentiment_service = SentimentService()
                news_sources = sentiment_config.get('news_sources', ['fmp_news'])
                all_articles = []
                for source in news_sources:
                    try:
                        source_provider = get_news_provider(source.replace('_news', ''))
                        articles = source_provider.get_news(ticker, start_date, end_date)
                        if articles:
                            all_articles.extend(articles)
                            logger.info(f"[Thread] Fetched {len(articles)} articles from {source_provider}")
                    except Exception as e:
                        logger.warning(f"[Thread] Error fetching from {source}: {e}")

                if all_articles:
                    lookback_periods = sentiment_config.get('lookback_periods', ['1d', '1w'])
                    df = sentiment_service.create_sentiment_features(
                        df, all_articles, lookback_periods=lookback_periods
                    )
            except Exception as e:
                logger.error(f"[Thread] Error fetching sentiment: {e}")

        # Fetch fundamentals if configured and regenerate_fundamentals or regenerate_macro is True
        if fundamentals_config and fundamentals_config.get('enabled'):
            try:
                if regen_options.regenerate_fundamentals:
                    logger.info("[Thread] Fetching fundamentals data...")
                    statement_types = fundamentals_config.get('statement_types')
                    if statement_types:
                        providers = fundamentals_config.get('fundamentals_providers', ['yfinance'])
                        df = FundamentalsService.create_statement_features_v2(
                            df, ticker, statement_types,
                            providers=providers
                        )
                        logger.info(f"[Thread] Added statement features for: {statement_types}")

                    # Also get current fundamentals
                    fundamentals = FundamentalsService.get_fundamental_data(ticker)
                    if fundamentals and fundamentals.get('current'):
                        current = fundamentals['current']
                        for key, value in current.items():
                            if value is not None:
                                df[f'fundamental_{key}'] = value

                # Fetch macro indicators if configured and regenerate_macro is True
                macro_indicators = fundamentals_config.get('macro_indicators', [])
                if macro_indicators and regen_options.regenerate_macro:
                    logger.info(f"[Thread] Fetching macro indicators: {macro_indicators}")
                    try:
                        macro_service = MacroService()
                        df = macro_service.integrate_macro_with_ohlc(df, macro_indicators)
                        for indicator in macro_indicators:
                            if indicator in df.columns:
                                df = df.rename(columns={indicator: f'macro_{indicator}'})
                                if f'{indicator}_yoy_change' in df.columns:
                                    df = df.rename(columns={f'{indicator}_yoy_change': f'macro_{indicator}_yoy_change'})
                                if f'{indicator}_days_since' in df.columns:
                                    df = df.rename(columns={f'{indicator}_days_since': f'macro_{indicator}_days_since'})
                        logger.info(f"[Thread] Added macro columns for: {macro_indicators}")
                    except Exception as e:
                        logger.warning(f"[Thread] Error fetching macro data: {e}")

            except Exception as e:
                logger.error(f"[Thread] Error fetching fundamentals: {e}")

        # Filter out warmup rows (applies when OHLCV regenerated OR when warmup fetched for TA)
        if warmup_days > 0 and (regen_options.regenerate_ohlcv or regen_options.regenerate_technical):
            original_len = len(df)
            df['Date'] = pd.to_datetime(df['Date'])
            requested_start_ts = pd.to_datetime(requested_start_date)
            if df['Date'].dt.tz is not None:
                df['Date'] = df['Date'].dt.tz_localize(None)
            if hasattr(requested_start_ts, 'tzinfo') and requested_start_ts.tzinfo is not None:
                requested_start_ts = requested_start_ts.replace(tzinfo=None)
            df = df[df['Date'] >= requested_start_ts].reset_index(drop=True)
            rows_filtered = original_len - len(df)
            if rows_filtered > 0:
                logger.info(f"[Thread] Filtered {rows_filtered} warmup rows, {len(df)} rows remaining")

        # Save to file
        save_path = Path(file_path)
        save_path.parent.mkdir(exist_ok=True)
        df.to_csv(save_path, index=False)
        logger.info(f"[Thread] Saved regenerated dataset to {save_path} with {len(df.columns)} columns")

        # Update DB with success
        db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if db_dataset:
            db_dataset.start_date = df['Date'].min()
            db_dataset.end_date = df['Date'].max()
            db_dataset.rows_count = len(df)
            db_dataset.status = DatasetStatus.READY.value
            db_dataset.error_message = None
            gen_config["regenerated_at"] = datetime.now().isoformat()
            db_dataset.generation_config = gen_config
            db.commit()
            logger.info(f"[Thread] Dataset {dataset_id} regenerated successfully with {len(df)} rows")

    except Exception as e:
        logger.error(f"[Thread] Error regenerating dataset {dataset_id}: {e}", exc_info=True)
        try:
            db_dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if db_dataset:
                db_dataset.status = DatasetStatus.ERROR.value
                db_dataset.error_message = str(e)[:500]
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


@router.post("", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    dataset_create: DatasetCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new dataset by fetching data from a provider and saving it.

    Dataset generation runs in a background thread to avoid blocking the API.
    The dataset is created with BUILDING status and updated to READY when complete.
    Poll GET /datasets/{id} to check status.

    Args:
        dataset_create: Dataset creation parameters
        db: Database session

    Returns:
        Created dataset with BUILDING status (generation runs in background)
    """
    try:
        logger.info(f"Creating dataset for {dataset_create.ticker} with timeframe {dataset_create.timeframe}")

        # Generate dataset name if not provided
        if not dataset_create.name:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dataset_create.name = f"{dataset_create.ticker}_{dataset_create.timeframe}_{timestamp}"

        # Calculate date range if not provided (default to 1 year)
        if not dataset_create.end_date:
            end_date = datetime.now()
        else:
            end_date = datetime.strptime(dataset_create.end_date, "%Y-%m-%d")

        if not dataset_create.start_date:
            start_date = end_date - timedelta(days=365)
        else:
            start_date = datetime.strptime(dataset_create.start_date, "%Y-%m-%d")

        # Build generation config for regeneration capability
        generation_config = {
            "data_provider": dataset_create.data_provider or "yfinance",
            "original_start_date": dataset_create.start_date,
            "original_end_date": dataset_create.end_date,
            "indicator_collection_id": dataset_create.indicator_collection_id,
            "created_at": datetime.now().isoformat()
        }

        # Datasets live under the test-bucket dir (app.paths), not the repo/CWD.
        datasets_dir = DATASETS_DIR
        datasets_dir.mkdir(parents=True, exist_ok=True)
        file_path = datasets_dir / f"{dataset_create.name}.csv"

        # Create database record in BUILDING status first
        db_dataset = Dataset(
            name=dataset_create.name,
            ticker=dataset_create.ticker,
            timeframe=dataset_create.timeframe,
            start_date=start_date,
            end_date=end_date,
            rows_count=0,
            status=DatasetStatus.BUILDING.value,
            technical_indicators=dataset_create.technical_indicators,
            fundamentals_config=dataset_create.fundamentals_config,
            sentiment_config=dataset_create.sentiment_config,
            generation_config=generation_config,
            labels=dataset_create.labels,
            file_path=str(file_path)
        )

        db.add(db_dataset)
        db.commit()
        db.refresh(db_dataset)
        logger.info(f"Created dataset record with ID {db_dataset.id} in BUILDING status")

        # Prepare config dict for background thread
        dataset_config = {
            'ticker': dataset_create.ticker,
            'timeframe': dataset_create.timeframe,
            'start_date': dataset_create.start_date,
            'end_date': dataset_create.end_date,
            'data_provider': dataset_create.data_provider,
            'technical_indicators': dataset_create.technical_indicators,
            'sentiment_config': dataset_create.sentiment_config,
            'fundamentals_config': dataset_create.fundamentals_config,
        }

        # Submit to background thread pool (non-blocking)
        _dataset_executor.submit(_build_dataset_in_background, db_dataset.id, dataset_config)
        logger.info(f"Submitted dataset {db_dataset.id} to background thread for processing")

        # Return immediately with BUILDING status
        return db_dataset

    except Exception as e:
        logger.error(f"Error creating dataset record: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create dataset: {str(e)}"
        )


@router.post("/batch", status_code=status.HTTP_201_CREATED)
async def create_batch_datasets(
    batch_request: dict,
    db: Session = Depends(get_db)
):
    """
    Create multiple datasets from a list of symbols with shared configuration.

    Args:
        batch_request: Dict with symbols list + shared dataset config + optional labels
        db: Database session

    Returns:
        List of created dataset IDs
    """
    try:
        symbols = batch_request.get('symbols', [])
        if not symbols:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="symbols list is required and cannot be empty"
            )

        # Extract shared config
        timeframe = batch_request.get('timeframe', '1d')
        start_date_str = batch_request.get('start_date')
        end_date_str = batch_request.get('end_date')
        data_provider = batch_request.get('data_provider', 'yfinance')
        technical_indicators = batch_request.get('technical_indicators')
        sentiment_config = batch_request.get('sentiment_config')
        fundamentals_config = batch_request.get('fundamentals_config')
        indicator_collection_id = batch_request.get('indicator_collection_id')
        user_labels = batch_request.get('labels', [])
        batch_name = batch_request.get('name')

        # Generate batch label
        if not batch_name:
            batch_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_label = f"batch-{batch_name}"

        # Combine labels: batch label + user labels
        combined_labels = [batch_label] + (user_labels or [])

        # Calculate dates
        if end_date_str:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        else:
            end_date = datetime.now()

        if start_date_str:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        else:
            start_date = end_date - timedelta(days=365)

        created_ids = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for symbol in symbols:
            symbol = symbol.strip().upper()
            if not symbol:
                continue

            dataset_name = f"{symbol}_{timeframe}_{timestamp}"

            generation_config = {
                "data_provider": data_provider,
                "original_start_date": start_date_str,
                "original_end_date": end_date_str,
                "indicator_collection_id": indicator_collection_id,
                "created_at": datetime.now().isoformat(),
                "batch_name": batch_name
            }

            datasets_dir = DATASETS_DIR
            datasets_dir.mkdir(parents=True, exist_ok=True)
            file_path = datasets_dir / f"{dataset_name}.csv"

            db_dataset = Dataset(
                name=dataset_name,
                ticker=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                rows_count=0,
                status=DatasetStatus.BUILDING.value,
                technical_indicators=technical_indicators,
                fundamentals_config=fundamentals_config,
                sentiment_config=sentiment_config,
                generation_config=generation_config,
                labels=combined_labels,
                file_path=str(file_path)
            )

            db.add(db_dataset)
            db.commit()
            db.refresh(db_dataset)

            dataset_config = {
                'ticker': symbol,
                'timeframe': timeframe,
                'start_date': start_date_str,
                'end_date': end_date_str,
                'data_provider': data_provider,
                'technical_indicators': technical_indicators,
                'sentiment_config': sentiment_config,
                'fundamentals_config': fundamentals_config,
            }

            _dataset_executor.submit(_build_dataset_in_background, db_dataset.id, dataset_config)
            created_ids.append(db_dataset.id)

        logger.info(f"Batch created {len(created_ids)} datasets with label '{batch_label}'")

        return {
            "created_ids": created_ids,
            "count": len(created_ids),
            "batch_label": batch_label
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating batch datasets: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create batch datasets: {str(e)}"
        )


@router.post("/check-compatibility")
async def check_compatibility_endpoint(request: dict, db: Session = Depends(get_db)):
    """Check if multiple datasets have identical columns for multi-dataset training."""
    dataset_ids = request.get('dataset_ids', [])
    if len(dataset_ids) < 2:
        return {'compatible': True, 'message': 'Need at least 2 datasets to check'}

    dataframes = []
    dataset_names = []
    for ds_id in dataset_ids:
        dataset = db.query(Dataset).filter(Dataset.id == ds_id).first()
        if not dataset or not dataset.file_path:
            raise HTTPException(status_code=404, detail=f"Dataset {ds_id} not found")
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Dataset {ds_id} file not found")
        df = pd.read_csv(file_path)
        dataframes.append(df)
        dataset_names.append(dataset.name)

    result = check_dataset_compatibility(dataframes)
    result['dataset_names'] = dataset_names
    return result


@router.get("", response_model=DatasetListResponse)
async def list_datasets(db: Session = Depends(get_db)):
    """
    List all datasets

    Args:
        db: Database session

    Returns:
        List of all datasets
    """
    try:
        datasets = db.query(Dataset).order_by(Dataset.created_at.desc()).all()
        return {
            "datasets": datasets,
            "total": len(datasets)
        }
    except Exception as e:
        logger.error(f"Error listing datasets: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list datasets: {str(e)}"
        )


@router.patch("/{dataset_id}/rename", response_model=DatasetResponse)
async def rename_dataset(
    dataset_id: int,
    new_name: str = Query(..., description="New name for the dataset"),
    db: Session = Depends(get_db)
):
    """
    Rename a dataset without regenerating data.

    This is a lightweight operation that only updates the name in the database.
    The underlying file is not renamed.

    Args:
        dataset_id: Dataset ID
        new_name: New name for the dataset
        db: Database session

    Returns:
        Updated dataset
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        old_name = dataset.name
        dataset.name = new_name
        db.commit()
        db.refresh(dataset)

        logger.info(f"Renamed dataset {dataset_id} from '{old_name}' to '{new_name}'")
        return dataset

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error renaming dataset: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rename dataset: {str(e)}"
        )


@router.get("/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """
    Get a single dataset by ID

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Dataset details
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )
        return dataset
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching dataset: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch dataset: {str(e)}"
        )


@router.get("/{dataset_id}/preview")
async def get_dataset_preview(
    dataset_id: int,
    columns: Optional[str] = Query(None, description="Comma-separated list of columns to include (default: all columns)"),
    max_rows: int = Query(2000, description="Maximum rows to return (0 for all, default: 2000)"),
    sample: bool = Query(True, description="Sample evenly if exceeding max_rows (default: True)"),
    db: Session = Depends(get_db)
):
    """
    Get dataset preview data for charting with pagination/sampling.

    For large datasets, returns sampled data to improve chart performance.
    Returns all columns by default with max 2000 rows sampled evenly.

    Args:
        dataset_id: Dataset ID
        columns: Comma-separated columns to include (default: all columns)
        max_rows: Maximum rows to return (default: 2000, 0 for all)
        sample: If True, sample evenly across dataset when exceeding max_rows
        db: Database session

    Returns:
        Dataset preview data
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load CSV file
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        # Determine which columns to load
        if columns:
            usecols = [c.strip() for c in columns.split(',')]
            # Read only specified columns
            try:
                df = pd.read_csv(file_path, usecols=usecols)
            except ValueError:
                # Some columns don't exist, fall back to loading all then filter
                df = pd.read_csv(file_path)
                available_cols = [c for c in usecols if c in df.columns]
                if available_cols:
                    df = df[available_cols]
        else:
            # Default: load all columns
            df = pd.read_csv(file_path)

        # Sort by date to ensure chronological order
        if 'Date' in df.columns:
            df = df.sort_values('Date').reset_index(drop=True)

        total_rows = len(df)

        # Sample if needed
        if max_rows > 0 and total_rows > max_rows:
            if sample:
                # Sample evenly across the dataset
                step = total_rows // max_rows
                indices = list(range(0, total_rows, step))[:max_rows]
                df = df.iloc[indices]
            else:
                # Just take first max_rows
                df = df.head(max_rows)

        # Use pandas to_json with proper NaN handling, then parse back
        import json
        json_str = df.to_json(orient='records', date_format='iso')
        data = json.loads(json_str)

        return {
            "dataset_id": dataset_id,
            "total_rows": total_rows,
            "returned_rows": len(data),
            "sampled": max_rows > 0 and total_rows > max_rows,
            "columns": list(df.columns),
            "data": data
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting dataset preview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get dataset preview: {str(e)}"
        )


@router.post("/{dataset_id}/preview-targets")
async def preview_prediction_targets(
    dataset_id: int,
    request_body: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Preview prediction target distribution for a dataset.

    Calculates prediction targets and returns counts of positive/negative
    samples in train/test splits. Used by job wizard to warn about imbalanced data.

    Args:
        dataset_id: Dataset ID
        request_body: {
            targets: [{profitPercent, maxDrawdownPercent, timePeriodDays}],
            trainRatio: float (0.0-1.0, default 0.8)
        }
        db: Database session

    Returns:
        Target distribution with warnings
    """
    try:
        from app.services.darts_models import PredictionTargetService

        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        total_rows = len(df)

        # Parse request
        targets_input = request_body.get('targets', [])
        train_ratio = request_body.get('trainRatio', 0.8)

        # Calculate train/test split
        split_idx = int(total_rows * train_ratio)
        train_rows = split_idx
        test_rows = total_rows - split_idx

        # Calculate prediction targets
        target_service = PredictionTargetService()
        targets_config = []
        for t in targets_input:
            targets_config.append({
                'profit_pct': t.get('profitPercent', 10),
                'max_dd': t.get('maxDrawdownPercent', 5),
                'days': t.get('timePeriodDays', 7),
                'direction': 'up'
            })

        if not targets_config:
            return {
                "dataset_id": dataset_id,
                "dataset_rows": total_rows,
                "train_rows": train_rows,
                "test_rows": test_rows,
                "targets": []
            }

        df_with_targets = target_service.calculate_prediction_targets(df.copy(), targets_config)

        # Analyze each target
        targets_result = []
        for t in targets_config:
            col_name = f"price_up_{t['profit_pct']}pct_{t['max_dd']}dd_{t['days']}d"
            if col_name not in df_with_targets.columns:
                continue

            values = df_with_targets[col_name]
            train_values = values.iloc[:split_idx]
            test_values = values.iloc[split_idx:]

            train_pos = int((train_values == 1).sum())
            train_neg = int((train_values == 0).sum())
            test_pos = int((test_values == 1).sum())
            test_neg = int((test_values == 0).sum())

            # Generate warnings
            warnings = []
            if train_pos == 0:
                warnings.append("No positive samples in training set - model cannot learn to predict positive cases")
            if test_pos == 0:
                warnings.append("No positive samples in test set - F1/precision/recall will be 0")
            if train_pos > 0 and train_pos / len(train_values) < 0.01:
                warnings.append(f"Very low positive rate in training ({100*train_pos/len(train_values):.1f}%) - model may struggle")
            if test_pos > 0 and test_pos / len(test_values) < 0.01:
                warnings.append(f"Very low positive rate in test ({100*test_pos/len(test_values):.1f}%)")

            targets_result.append({
                "name": col_name,
                "label": f"{t['profit_pct']}% profit / {t['max_dd']}% DD / {t['days']}d",
                "train_positive": train_pos,
                "train_negative": train_neg,
                "train_positive_pct": round(100 * train_pos / len(train_values), 2) if len(train_values) > 0 else 0,
                "test_positive": test_pos,
                "test_negative": test_neg,
                "test_positive_pct": round(100 * test_pos / len(test_values), 2) if len(test_values) > 0 else 0,
                "warnings": warnings
            })

        return {
            "dataset_id": dataset_id,
            "dataset_rows": total_rows,
            "train_rows": train_rows,
            "test_rows": test_rows,
            "targets": targets_result
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error previewing prediction targets: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to preview prediction targets: {str(e)}"
        )


@router.post("/{dataset_id}/calculate-indicators")
async def calculate_indicators(
    dataset_id: int,
    request_body: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Calculate technical indicators for a dataset.

    Used for live visualization and prediction target experimentation.
    Returns calculated indicator values without modifying the dataset.

    Supports multi-timeframe indicators: calculate on a higher timeframe
    and align to the dataset's base timeframe.

    Args:
        dataset_id: Dataset ID
        request_body: {
            indicators: [
                {"type": "rsi", "period": 14},
                {"type": "macd", "fast": 12, "slow": 26, "signal": 9},
                {"type": "sar", "af_start": 0.02, "af_max": 0.2},
                {"type": "zigzag", "deviation_pct": 5.0}
            ],
            timeframe: Optional[str] - If specified, calculate indicators on this
                       higher timeframe and align to dataset's base timeframe.
                       E.g., "1h" for 1h indicators on a 15m dataset.
        }
        db: Database session

    Returns:
        Calculated indicator data series
    """
    try:
        from app.services.indicators import IndicatorService

        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)

        # Sort by date to ensure consistent ordering with target calculations
        if 'Date' in df.columns:
            df = df.sort_values('Date').reset_index(drop=True)

        # Parse request
        indicators = request_body.get('indicators', [])
        target_timeframe = request_body.get('timeframe')

        if not indicators:
            return {
                "dataset_id": dataset_id,
                "data": []
            }

        # Calculate indicators
        indicator_service = IndicatorService()

        if target_timeframe and target_timeframe != dataset.timeframe:
            # Multi-timeframe calculation: resample, calculate, align back
            logger.info(f"Calculating indicators on {target_timeframe} timeframe (dataset is {dataset.timeframe})")
            results = indicator_service.calculate_indicators_multi_timeframe(
                df,
                indicators,
                target_timeframe,
                source_timeframe=dataset.timeframe
            )
        else:
            # Same timeframe: calculate directly
            results = indicator_service.calculate_indicators(df, indicators)

        # Convert to list of dicts for JSON response
        # Use ISO format for dates to match preview endpoint (important for JS timestamp parsing)
        data = []
        if 'Date' in df.columns:
            # Convert to datetime if not already, then use isoformat for consistency
            df['Date'] = pd.to_datetime(df['Date'])

        for i in range(len(df)):
            if 'Date' in df.columns:
                # Use isoformat to match the preview endpoint's date_format='iso'
                date_val = df['Date'].iloc[i]
                row = {"date": date_val.isoformat() if hasattr(date_val, 'isoformat') else str(date_val)}
            else:
                row = {"date": str(i)}
            for col_name, series in results.items():
                val = series.iloc[i]
                row[col_name] = None if pd.isna(val) else float(val)
            data.append(row)

        return {
            "dataset_id": dataset_id,
            "indicators": list(results.keys()),
            "timeframe_used": target_timeframe or dataset.timeframe,
            "data": data
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate indicators: {str(e)}"
        )


@router.post("/{dataset_id}/calculate-targets")
async def calculate_prediction_targets_v2(
    dataset_id: int,
    request_body: Dict[str, Any],
    db: Session = Depends(get_db)
):
    """
    Calculate prediction targets for a dataset (V2 - all target types).

    Supports: price_based, directional, triple_barrier, trend_reversal, volatility

    Args:
        dataset_id: Dataset ID
        request_body: {
            targets: [
                {"type": "directional", "direction": "up", "horizon": 5},
                {"type": "triple_barrier", "profit_pct": 3, "stop_pct": 2, "max_bars": 10},
                {"type": "trend_reversal", "indicator": "rsi", "params": {"period": 14},
                 "threshold": 30, "direction": "bullish"},
                {"type": "volatility", "horizon": 5, "method": "std"}
            ]
        }
        db: Database session

    Returns:
        Calculated target data with statistics
    """
    try:
        from app.services.darts_models import PredictionTargetService

        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)

        # Parse request
        targets = request_body.get('targets', [])

        if not targets:
            return {
                "dataset_id": dataset_id,
                "targets": []
            }

        # Calculate targets with dataset's timeframe for multi-timeframe support
        target_service = PredictionTargetService()
        results = target_service.calculate_all_targets(
            df, targets,
            dataset_timeframe=dataset.timeframe
        )

        return {
            "dataset_id": dataset_id,
            "total_rows": len(df),
            "dataset_timeframe": dataset.timeframe,
            "targets": results
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating prediction targets: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate prediction targets: {str(e)}"
        )


@router.get("/{dataset_id}/stats")
async def get_dataset_stats(dataset_id: int, db: Session = Depends(get_db)):
    """
    Get comprehensive statistics for a dataset

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Dataset statistics including row count, column count, date range,
        missing data percentages, and basic statistics for numeric columns
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load CSV file
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)

        # Basic counts
        total_rows = len(df)
        total_columns = len(df.columns)

        # Date range
        date_column = 'Date' if 'Date' in df.columns else df.columns[0]
        if date_column in df.columns:
            df[date_column] = pd.to_datetime(df[date_column])
            date_range = {
                "start": str(df[date_column].min()),
                "end": str(df[date_column].max())
            }
        else:
            date_range = None

        # Missing data percentage per column
        missing_data = {}
        for col in df.columns:
            missing_count = df[col].isna().sum()
            missing_pct = (missing_count / total_rows * 100) if total_rows > 0 else 0
            missing_data[col] = {
                "count": int(missing_count),
                "percentage": round(missing_pct, 2)
            }

        # Basic statistics for numeric columns
        numeric_stats = {}
        numeric_columns = df.select_dtypes(include=['int64', 'float64']).columns

        for col in numeric_columns:
            col_data = df[col].dropna()
            if len(col_data) > 0:
                numeric_stats[col] = {
                    "count": int(len(col_data)),
                    "mean": round(float(col_data.mean()), 4),
                    "std": round(float(col_data.std()), 4),
                    "min": round(float(col_data.min()), 4),
                    "max": round(float(col_data.max()), 4),
                    "median": round(float(col_data.median()), 4)
                }

        # Column types
        column_types = {col: str(dtype) for col, dtype in df.dtypes.items()}

        return {
            "dataset_id": dataset_id,
            "total_rows": total_rows,
            "total_columns": total_columns,
            "date_range": date_range,
            "columns": list(df.columns),
            "column_types": column_types,
            "missing_data": missing_data,
            "numeric_statistics": numeric_stats
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating dataset statistics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate dataset statistics: {str(e)}"
        )


@router.get("/{dataset_id}/columns")
async def get_dataset_columns(dataset_id: int, db: Session = Depends(get_db)):
    """
    Get all columns in a dataset, categorized by type.

    Categories:
    - price: OHLCV data (Date, Open, High, Low, Close, Volume)
    - technical: Technical indicators (SMA, EMA, RSI, MACD, etc.)
    - fundamental: Fundamental data (P/E, EPS, FCF, etc.)
    - sentiment: Sentiment features (news counts, scores)
    - macro: Macro economic indicators (interest rates, GDP, etc.)
    - target: Prediction targets (price_up_*, price_down_*)
    - other: Unclassified columns

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Categorized column information with data types
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        # Load dataset to get columns
        df = pd.read_csv(file_path, nrows=5)  # Just need headers and dtypes

        # Categorize columns
        price_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'Adj Close']
        technical_patterns = ['SMA', 'EMA', 'RSI', 'MACD', 'BB_', 'BBANDS', 'ATR', 'ADX', 'CCI', 'MFI',
                             'OBV', 'VWAP', 'STOCH', 'WILLR', 'WILLIAMS', 'ROC', 'MOM', 'TRIX',
                             'DX', 'PLUS_DI', 'MINUS_DI', 'AROON', 'CMO', 'PPO', 'UO',
                             'SLOWK', 'SLOWD', 'FASTK', 'FASTD', 'UPPER', 'MIDDLE', 'LOWER',
                             'REAL', 'BBAND', 'SAR', 'ZIGZAG', 'PIVOT', 'DONCHIAN', 'ADX_']
        fundamental_patterns = ['fundamental_', 'PE', 'EPS', 'FCF', 'Revenue', 'Debt', 'ROE', 'ROA',
                               'BookValue', 'Dividend', 'MarketCap', 'PB', 'PS',
                               'days_to', 'last_', 'next_']
        sentiment_patterns = ['news_', 'sentiment_', 'positive', 'negative', 'neutral']
        macro_patterns = ['macro_', 'interest_rate', 'gdp', 'inflation', 'unemployment', 'cpi',
                         'fed_', 'treasury', 'yield_', 'vix']
        target_patterns = ['price_up_', 'price_down_', 'target_', 'label_']

        def categorize_column(col_name: str) -> str:
            col_lower = col_name.lower()
            col_upper = col_name.upper()

            if col_name in price_cols:
                return 'price'
            if any(p in col_upper for p in technical_patterns):
                return 'technical'
            if any(p in col_lower for p in fundamental_patterns):
                return 'fundamental'
            if any(p in col_lower for p in sentiment_patterns):
                return 'sentiment'
            if any(p in col_lower for p in macro_patterns):
                return 'macro'
            if any(p in col_lower for p in target_patterns):
                return 'target'
            return 'other'

        columns = {}
        for col in df.columns:
            category = categorize_column(col)
            if category not in columns:
                columns[category] = []
            columns[category].append({
                'name': col,
                'dtype': str(df[col].dtype),
                'category': category
            })

        # Count by category
        category_counts = {cat: len(cols) for cat, cols in columns.items()}

        return {
            'dataset_id': dataset_id,
            'total_columns': len(df.columns),
            'category_counts': category_counts,
            'columns': columns,
            'all_columns': list(df.columns)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting dataset columns: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get dataset columns: {str(e)}"
        )


@router.get("/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """
    Get dataset details by ID

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Dataset details
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        return dataset

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting dataset: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get dataset: {str(e)}"
        )


@router.get("/{dataset_id}/export")
async def export_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """
    Export dataset as CSV file for download

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        CSV file download
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Check if file exists
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        logger.info(f"Exporting dataset {dataset_id}: {file_path}")

        # Return the CSV file as a download
        return FileResponse(
            path=str(file_path),
            media_type="text/csv",
            filename=f"{dataset.name}.csv",
            headers={"Content-Disposition": f"attachment; filename={dataset.name}.csv"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting dataset: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export dataset: {str(e)}"
        )


@router.get("/{dataset_id}/export/parquet")
async def export_dataset_parquet(dataset_id: int, db: Session = Depends(get_db)):
    """
    Export dataset as Parquet file for download

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Parquet file download
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Check if CSV file exists
        csv_path = Path(dataset.file_path)
        if not csv_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {csv_path}"
            )

        logger.info(f"Exporting dataset {dataset_id} to Parquet: {csv_path}")

        # Read CSV and convert to Parquet
        df = pd.read_csv(csv_path)

        # Create temporary Parquet file
        parquet_path = csv_path.with_suffix('.parquet')
        df.to_parquet(parquet_path, engine='pyarrow', compression='snappy', index=False)

        logger.info(f"Created Parquet file: {parquet_path}")

        # Return the Parquet file as a download
        return FileResponse(
            path=str(parquet_path),
            media_type="application/octet-stream",
            filename=f"{dataset.name}.parquet",
            headers={"Content-Disposition": f"attachment; filename={dataset.name}.parquet"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting dataset to Parquet: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export dataset to Parquet: {str(e)}"
        )


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(dataset_id: int, db: Session = Depends(get_db)):
    """
    Delete a dataset

    Args:
        dataset_id: Dataset ID
        db: Database session
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Delete file from disk
        file_path = Path(dataset.file_path)
        if file_path.exists():
            file_path.unlink()
            logger.info(f"Deleted file: {file_path}")

        # Delete from database
        db.delete(dataset)
        db.commit()

        logger.info(f"Deleted dataset with ID {dataset_id}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting dataset: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete dataset: {str(e)}"
        )


@router.post("/{dataset_id}/regenerate", response_model=DatasetResponse)
async def regenerate_dataset(
    dataset_id: int,
    regen_options: Optional[DatasetRegenerate] = None,
    db: Session = Depends(get_db)
):
    """
    Regenerate a dataset, optionally selecting which components to regenerate.

    Dataset regeneration runs in a background thread to avoid blocking the API.
    The dataset is set to BUILDING status and updated to READY when complete.
    Poll GET /datasets/{id} to check status.

    This endpoint can be used to:
    - Retry a failed dataset generation (full regeneration)
    - Refresh data for an existing dataset
    - Partially regenerate (e.g., recalculate TA/macro without re-fetching news)

    Args:
        dataset_id: Dataset ID to regenerate
        regen_options: Optional partial regeneration settings (default: regenerate all)
        db: Database session

    Returns:
        Dataset with BUILDING status (regeneration runs in background)
    """
    # Default to regenerating everything if no options provided
    if regen_options is None:
        regen_options = DatasetRegenerate()

    # Get dataset from DB
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset with ID {dataset_id} not found"
        )

    logger.info(f"Regenerating dataset {dataset_id} ({dataset.name})")

    # Extract all config into local variables for background thread
    gen_config = (dataset.generation_config or {}).copy()

    # Parse dates
    if gen_config.get("original_start_date"):
        start_date = datetime.strptime(gen_config["original_start_date"], "%Y-%m-%d")
    else:
        start_date = dataset.start_date

    if gen_config.get("original_end_date"):
        end_date = datetime.strptime(gen_config["original_end_date"], "%Y-%m-%d")
    else:
        end_date = dataset.end_date

    # Prepare config dict for background thread
    regen_config = {
        'regen_options': regen_options,
        'ticker': dataset.ticker,
        'timeframe': dataset.timeframe,
        'file_path': dataset.file_path,
        'technical_indicators': dataset.technical_indicators.copy() if dataset.technical_indicators else None,
        'fundamentals_config': dataset.fundamentals_config.copy() if dataset.fundamentals_config else None,
        'sentiment_config': dataset.sentiment_config.copy() if dataset.sentiment_config else None,
        'gen_config': gen_config,
        'start_date': start_date,
        'end_date': end_date,
    }

    # Set status to BUILDING and commit
    dataset.status = DatasetStatus.BUILDING.value
    dataset.error_message = None
    db.commit()

    # Log regeneration options
    logger.info(f"Regeneration options: OHLCV={regen_options.regenerate_ohlcv}, "
               f"TA={regen_options.regenerate_technical}, "
               f"Sentiment={regen_options.regenerate_sentiment}, "
               f"Fundamentals={regen_options.regenerate_fundamentals}, "
               f"Macro={regen_options.regenerate_macro}")

    # Submit to background thread pool (non-blocking)
    _dataset_executor.submit(_regenerate_dataset_in_background, dataset_id, regen_config)
    logger.info(f"Submitted dataset {dataset_id} regeneration to background thread")

    # Refresh and return with BUILDING status
    db.refresh(dataset)
    return dataset



@router.post("/batch-regenerate")
async def batch_regenerate_datasets(
    request: BatchRegenerateRequest,
    db: Session = Depends(get_db)
):
    """
    Regenerate multiple datasets with the same options.

    Each dataset is submitted to the shared background thread pool (max 5 workers).
    Datasets are set to BUILDING status immediately; poll GET /datasets/{id} for progress.

    Args:
        request: List of dataset IDs and regeneration options
        db: Database session

    Returns:
        Dict with queued_ids and count
    """
    regen_options = request.regenerate_options or DatasetRegenerate()
    queued: list[tuple[int, dict]] = []  # (dataset_id, regen_config) pairs
    not_found_ids = []

    for dataset_id in request.dataset_ids:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            not_found_ids.append(dataset_id)
            continue

        gen_config = (dataset.generation_config or {}).copy()

        if gen_config.get("original_start_date"):
            start_date = datetime.strptime(gen_config["original_start_date"], "%Y-%m-%d")
        else:
            start_date = dataset.start_date

        if gen_config.get("original_end_date"):
            end_date = datetime.strptime(gen_config["original_end_date"], "%Y-%m-%d")
        else:
            end_date = dataset.end_date

        regen_config = {
            'regen_options': regen_options,
            'ticker': dataset.ticker,
            'timeframe': dataset.timeframe,
            'file_path': dataset.file_path,
            'technical_indicators': dataset.technical_indicators.copy() if dataset.technical_indicators else None,
            'fundamentals_config': dataset.fundamentals_config.copy() if dataset.fundamentals_config else None,
            'sentiment_config': dataset.sentiment_config.copy() if dataset.sentiment_config else None,
            'gen_config': gen_config,
            'start_date': start_date,
            'end_date': end_date,
        }

        dataset.status = DatasetStatus.BUILDING.value
        dataset.error_message = None
        queued.append((dataset_id, regen_config))

    db.commit()

    # Submit each dataset with its own config to the thread pool
    for dataset_id, regen_config in queued:
        _dataset_executor.submit(_regenerate_dataset_in_background, dataset_id, regen_config)

    queued_ids = [did for did, _ in queued]
    logger.info(f"Batch regeneration queued for {len(queued_ids)} datasets: {queued_ids}")

    return {
        "queued_ids": queued_ids,
        "count": len(queued_ids),
        "not_found_ids": not_found_ids,
    }


@router.post("/{dataset_id}/duplicate", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_dataset(
    dataset_id: int,
    duplicate_request: DatasetDuplicate,
    db: Session = Depends(get_db)
):
    """
    Duplicate a dataset, optionally with a different ticker.

    Re-fetches data using the stored generation_config with optional new ticker.

    Args:
        dataset_id: ID of dataset to duplicate
        duplicate_request: Optional new ticker and name
        db: Database session

    Returns:
        Newly created dataset
    """
    try:
        # Get original dataset
        original = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not original:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Determine new ticker and name
        new_ticker = duplicate_request.new_ticker or original.ticker
        if duplicate_request.new_name:
            new_name = duplicate_request.new_name
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_name = f"{new_ticker}_{original.timeframe}_{timestamp}"

        logger.info(f"Duplicating dataset {dataset_id} to {new_name} with ticker {new_ticker}")

        # Fetch data using the original generation config
        gen_config = original.generation_config or {}
        provider_name = gen_config.get("data_provider", "yfinance")
        provider = get_ohlcv_provider(provider_name)
        logger.info(f"Using OHLCV provider: {provider_name}")

        # Use original dates from generation_config or dataset
        start_date = datetime.strptime(gen_config.get("original_start_date"), "%Y-%m-%d") if gen_config.get("original_start_date") else original.start_date
        end_date = datetime.strptime(gen_config.get("original_end_date"), "%Y-%m-%d") if gen_config.get("original_end_date") else original.end_date

        # Calculate warmup period needed for indicators
        technical_indicators = original.technical_indicators or []
        warmup_bars, warmup_days = calculate_warmup_period(technical_indicators, original.timeframe)
        fetch_start_date = start_date - timedelta(days=warmup_days) if warmup_days > 0 else start_date

        if warmup_days > 0:
            logger.info(f"[Duplicate] Warmup: fetching {warmup_days} extra days for {warmup_bars}-bar indicators")

        # Convert timeframe to interval
        interval = INTERVAL_MAP.get(original.timeframe, "1d")

        data_points = provider.get_data(
            symbol=new_ticker,
            start_date=fetch_start_date,
            end_date=end_date,
            interval=interval
        )

        if not data_points:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No data available for {new_ticker}"
            )

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

        # Add time-based features (day_of_week, hour_of_day)
        df = add_time_features(df)

        # Apply technical indicators if configured
        if technical_indicators:
            logger.info(f"[Duplicate] Applying {len(technical_indicators)} technical indicators...")
            try:
                df = apply_technical_indicators(df, technical_indicators)
                logger.info(f"[Duplicate] Added technical indicators. {len(df.columns)} columns")
            except Exception as e:
                logger.error(f"[Duplicate] Error applying indicators: {e}")

        # Trim to requested date range (remove warmup period)
        if warmup_days > 0:
            df = df[df['Date'] >= pd.Timestamp(start_date)].reset_index(drop=True)
            logger.info(f"[Duplicate] Trimmed to {len(df)} rows after removing warmup period")

        # Save to new file
        datasets_dir = DATASETS_DIR
        datasets_dir.mkdir(parents=True, exist_ok=True)
        file_path = datasets_dir / f"{new_name}.csv"
        df.to_csv(file_path, index=False)

        # Build new generation config
        new_gen_config = {
            "data_provider": gen_config.get("data_provider", "yfinance"),
            "original_start_date": gen_config.get("original_start_date"),
            "original_end_date": gen_config.get("original_end_date"),
            "indicator_collection_id": gen_config.get("indicator_collection_id"),
            "duplicated_from": dataset_id,
            "created_at": datetime.now().isoformat()
        }

        # Create new database record
        new_dataset = Dataset(
            name=new_name,
            ticker=new_ticker,
            timeframe=original.timeframe,
            start_date=df['Date'].min(),
            end_date=df['Date'].max(),
            rows_count=len(df),
            technical_indicators=original.technical_indicators,
            fundamentals_config=original.fundamentals_config,
            sentiment_config=original.sentiment_config,
            generation_config=new_gen_config,
            file_path=str(file_path)
        )

        db.add(new_dataset)
        db.commit()
        db.refresh(new_dataset)

        logger.info(f"Created duplicate dataset with ID {new_dataset.id}")
        return new_dataset

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error duplicating dataset: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to duplicate dataset: {str(e)}"
        )


@router.put("/{dataset_id}", response_model=DatasetResponse)
async def update_dataset(
    dataset_id: int,
    dataset_update: DatasetUpdate
):
    """
    Update dataset properties and regenerate the dataset.

    OHLCV data is fetched synchronously for validation. If validation passes,
    heavy processing (indicators, sentiment, fundamentals) is done in background.

    Uses short-lived DB sessions to prevent database locking during OHLCV fetch.

    Args:
        dataset_id: Dataset ID to update
        dataset_update: Fields to update

    Returns:
        Updated dataset with status="building"
    """
    from app.services.task_queue import get_task_queue
    from app.models.database import SessionLocal

    # Phase 1: Read dataset and update fields (short session)
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        logger.info(f"Updating and regenerating dataset {dataset_id}")

        # Log full payload received
        logger.debug(f"Update payload received: {dataset_update.dict()}")
        logger.info(f"Update request: start_date={dataset_update.start_date}, end_date={dataset_update.end_date}, data_provider={dataset_update.data_provider}")
        logger.info(f"Current dataset: start_date={dataset.start_date}, end_date={dataset.end_date}, data_provider={dataset.generation_config.get('data_provider') if dataset.generation_config else 'N/A'}")

        # Track what's being updated
        updates = []

        # Update simple fields first
        if dataset_update.name:
            updates.append(f"name: {dataset.name} -> {dataset_update.name}")
            dataset.name = dataset_update.name

        if dataset_update.technical_indicators is not None:
            updates.append(f"technical_indicators: {len(dataset.technical_indicators or [])} -> {len(dataset_update.technical_indicators)} indicators")
            dataset.technical_indicators = dataset_update.technical_indicators

        if dataset_update.sentiment_config is not None:
            updates.append(f"sentiment_config updated")
            dataset.sentiment_config = dataset_update.sentiment_config

        if dataset_update.fundamentals_config is not None:
            updates.append(f"fundamentals_config updated")
            dataset.fundamentals_config = dataset_update.fundamentals_config

        # Update ticker/timeframe if provided
        new_ticker = dataset_update.ticker or dataset.ticker
        new_timeframe = dataset_update.timeframe or dataset.timeframe
        if new_ticker != dataset.ticker:
            updates.append(f"ticker: {dataset.ticker} -> {new_ticker}")
        if new_timeframe != dataset.timeframe:
            updates.append(f"timeframe: {dataset.timeframe} -> {new_timeframe}")
        dataset.ticker = new_ticker
        dataset.timeframe = new_timeframe

        # Parse dates - copy dict to ensure SQLAlchemy detects changes
        gen_config = dict(dataset.generation_config) if dataset.generation_config else {}
        old_start = gen_config.get("original_start_date")
        old_end = gen_config.get("original_end_date")
        old_provider = gen_config.get("data_provider", "yfinance")

        if dataset_update.start_date:
            start_date = datetime.strptime(dataset_update.start_date, "%Y-%m-%d")
            gen_config["original_start_date"] = dataset_update.start_date
            if old_start != dataset_update.start_date:
                updates.append(f"start_date: {old_start} -> {dataset_update.start_date}")
        elif gen_config.get("original_start_date"):
            start_date = datetime.strptime(gen_config["original_start_date"], "%Y-%m-%d")
        else:
            start_date = dataset.start_date

        if dataset_update.end_date:
            end_date = datetime.strptime(dataset_update.end_date, "%Y-%m-%d")
            gen_config["original_end_date"] = dataset_update.end_date
            if old_end != dataset_update.end_date:
                updates.append(f"end_date: {old_end} -> {dataset_update.end_date}")
        elif gen_config.get("original_end_date"):
            end_date = datetime.strptime(gen_config["original_end_date"], "%Y-%m-%d")
        else:
            end_date = dataset.end_date

        # Update data provider if provided
        if dataset_update.data_provider:
            if old_provider != dataset_update.data_provider:
                updates.append(f"data_provider: {old_provider} -> {dataset_update.data_provider}")
            gen_config["data_provider"] = dataset_update.data_provider

        # Save updated generation_config with new dates and provider
        dataset.generation_config = gen_config
        dataset_name = dataset.name  # Save for later use

        # Log all updates
        if updates:
            logger.info(f"Updating fields: {', '.join(updates)}")
        else:
            logger.info("No field changes detected, regenerating with current settings")

        # Set status to BUILDING
        dataset.status = DatasetStatus.BUILDING.value
        dataset.error_message = None
        db.commit()

        # Get provider name for OHLCV fetch
        provider_name = gen_config.get("data_provider", "yfinance")

    finally:
        db.close()  # Release DB connection before OHLCV fetch

    # Phase 2: Fetch OHLCV data (NO DB connection held)
    try:
        provider = get_ohlcv_provider(provider_name)
        interval = INTERVAL_MAP.get(new_timeframe, "1d")

        logger.info(f"Fetching data for {new_ticker} from {start_date.date()} to {end_date.date()} using {provider_name}")

        data_points = provider.get_data(
            symbol=new_ticker,
            start_date=start_date,
            end_date=end_date,
            interval=interval
        )
    except Exception as e:
        # Update status to error with short session
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.status = DatasetStatus.ERROR.value
                dataset.error_message = f"OHLCV fetch failed: {str(e)}"
                db.commit()
        finally:
            db.close()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch OHLCV data: {str(e)}"
        )

    # Phase 3: Validate OHLCV data (NO DB connection held)
    if not data_points:
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.status = DatasetStatus.ERROR.value
                dataset.error_message = f"No data available for {new_ticker}"
                db.commit()
        finally:
            db.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No data available for {new_ticker}"
        )

    # Convert to DataFrame for validation
    ohlcv_data = [{
        'Date': dp.timestamp.isoformat() if hasattr(dp.timestamp, 'isoformat') else str(dp.timestamp),
        'Open': dp.open,
        'High': dp.high,
        'Low': dp.low,
        'Close': dp.close,
        'Volume': dp.volume
    } for dp in data_points]

    df = pd.DataFrame(ohlcv_data)
    df['Date'] = pd.to_datetime(df['Date'])
    df = df.sort_values('Date').reset_index(drop=True)

    # Add time-based features (day_of_week, hour_of_day)
    df = add_time_features(df)

    logger.info(f"Fetched {len(df)} OHLC data points")

    # Validate that fetched data covers the requested date range (with 5-day tolerance for weekends/holidays)
    data_start_date = df['Date'].min().date() if hasattr(df['Date'].min(), 'date') else df['Date'].min()
    data_end_date = df['Date'].max().date() if hasattr(df['Date'].max(), 'date') else df['Date'].max()
    req_start_date = start_date.date() if hasattr(start_date, 'date') else start_date
    req_end_date = end_date.date() if hasattr(end_date, 'date') else end_date
    tolerance_days = 5

    if (data_start_date - req_start_date).days > tolerance_days:
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.status = DatasetStatus.ERROR.value
                dataset.error_message = f"Data starts at {data_start_date}, but requested {req_start_date}"
                db.commit()
        finally:
            db.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Data starts at {data_start_date}, but requested start date was {req_start_date}. Data may not be available for this range."
        )

    if (req_end_date - data_end_date).days > tolerance_days:
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.status = DatasetStatus.ERROR.value
                dataset.error_message = f"Data ends at {data_end_date}, but requested {req_end_date}"
                db.commit()
        finally:
            db.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Data ends at {data_end_date}, but requested end date was {req_end_date}. Data may not be available for this range."
        )

    # Phase 4: Queue background task and return (short session)
    logger.info(f"OHLCV validation passed. Queuing background task for dataset {dataset_id}")

    task_queue = get_task_queue()
    task_id = task_queue.queue_task(
        task_type='dataset_regeneration',
        name=f'Regenerate dataset {dataset_name}',
        description=f'Processing indicators, sentiment, and fundamentals for dataset {dataset_id}',
        payload={
            'dataset_id': dataset_id,
            'ohlcv_data': ohlcv_data,
            'ticker': new_ticker,
            'timeframe': new_timeframe,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        },
        max_retries=1,
        timeout_seconds=600
    )

    # Store task_id on dataset for tracking
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if dataset:
            dataset.task_id = task_id
            db.commit()
            db.refresh(dataset)
            logger.info(f"Dataset {dataset_id} update queued as background task {task_id}")
            return dataset
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset {dataset_id} not found after queuing task"
            )
    finally:
        db.close()


@router.post("/{dataset_id}/calculate-indicators")
async def calculate_multi_timeframe_indicators(
    dataset_id: int,
    timeframes: List[str] = None,
    indicators: dict = None,
    db: Session = Depends(get_db)
):
    """
    Calculate multi-timeframe technical indicators for a dataset.

    This endpoint fetches data at multiple timeframes (15m, 1h, 4h, D1) and calculates
    technical indicators for each, then merges them into the dataset.

    Args:
        dataset_id: Dataset ID to add indicators to
        timeframes: List of timeframes to calculate (default: ["15m", "1h", "4h", "1d"])
        indicators: Custom indicator configuration (default: uses DEFAULT_INDICATORS)
        db: Database session

    Returns:
        Updated dataset with indicator columns
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Use default timeframes if not specified
        if timeframes is None:
            timeframes = SUPPORTED_TIMEFRAMES

        # Validate timeframes
        invalid_timeframes = [tf for tf in timeframes if tf not in TIMEFRAME_INTERVAL_MAP]
        if invalid_timeframes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid timeframes: {invalid_timeframes}. Supported: {list(TIMEFRAME_INTERVAL_MAP.keys())}"
            )

        # Use default indicators if not specified
        if indicators is None:
            indicators = DEFAULT_INDICATORS

        # Load the dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        df['Date'] = pd.to_datetime(df['Date'])

        logger.info(f"Calculating multi-timeframe indicators for dataset {dataset_id}")
        logger.info(f"Timeframes: {timeframes}, Indicators: {list(indicators.keys())}")

        # Get base timeframe data range
        start_date = df['Date'].min()
        end_date = df['Date'].max()

        # Initialize provider
        gen_config = dataset.generation_config or {}
        provider_name = gen_config.get("data_provider", "yfinance")
        provider = get_ohlcv_provider(provider_name)

        # Calculate indicators for each timeframe
        all_indicators = {}

        for tf in timeframes:
            logger.info(f"Fetching data for timeframe {tf}")

            # Fetch data at this timeframe
            interval = TIMEFRAME_INTERVAL_MAP[tf]
            try:
                data_points = provider.get_data(
                    symbol=dataset.ticker,
                    start_date=start_date,
                    end_date=end_date,
                    interval=interval
                )

                if not data_points:
                    logger.warning(f"No data available for timeframe {tf}")
                    continue

                # Convert to DataFrame
                tf_df = pd.DataFrame([{
                    'Date': dp.timestamp,
                    'Open': dp.open,
                    'High': dp.high,
                    'Low': dp.low,
                    'Close': dp.close,
                    'Volume': dp.volume
                } for dp in data_points])

                tf_df = tf_df.sort_values('Date').reset_index(drop=True)

                # Prepare indicators config with timeframe prefix
                tf_indicators = {}
                for ind_name, ind_config in indicators.items():
                    prefixed_name = f"{ind_name}_{tf}"
                    tf_indicators[prefixed_name] = ind_config

                # Calculate indicators for this timeframe
                tf_df_with_indicators = TechnicalIndicators.add_indicators_to_dataframe(
                    tf_df, tf_indicators
                )

                # Extract indicator columns (exclude OHLCV)
                ohlcv_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
                indicator_cols = [col for col in tf_df_with_indicators.columns if col not in ohlcv_cols]

                # Store indicator data with dates
                for col in indicator_cols:
                    all_indicators[col] = tf_df_with_indicators[['Date', col]].copy()

                logger.info(f"Calculated {len(indicator_cols)} indicators for timeframe {tf}")

            except Exception as e:
                logger.warning(f"Failed to calculate indicators for timeframe {tf}: {e}")
                continue

        # Merge indicators into main dataset
        result_df = df.copy()

        for ind_name, ind_df in all_indicators.items():
            # Merge on Date using forward fill for different timeframe resolutions
            ind_df = ind_df.rename(columns={ind_name: ind_name})
            ind_df = ind_df.set_index('Date')

            # Resample to match main dataset timeframe and forward fill
            result_df = result_df.set_index('Date') if 'Date' not in result_df.index.names else result_df

            # Align indicator data to main dataset dates
            aligned_indicator = ind_df.reindex(result_df.index, method='ffill')
            result_df[ind_name] = aligned_indicator[ind_name]
            result_df = result_df.reset_index()

        # Save updated dataset
        result_df.to_csv(file_path, index=False)

        # Update dataset metadata
        indicator_config = dataset.technical_indicators or {}
        indicator_config['multi_timeframe'] = {
            'timeframes': timeframes,
            'indicators': list(indicators.keys()),
            'calculated_at': datetime.now().isoformat()
        }
        dataset.technical_indicators = indicator_config
        db.commit()
        db.refresh(dataset)

        logger.info(f"Successfully calculated multi-timeframe indicators for dataset {dataset_id}")

        return {
            "dataset_id": dataset_id,
            "timeframes": timeframes,
            "indicators_added": list(all_indicators.keys()),
            "total_columns": len(result_df.columns),
            "rows": len(result_df),
            "message": f"Successfully calculated {len(all_indicators)} indicator columns across {len(timeframes)} timeframes"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating multi-timeframe indicators: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate indicators: {str(e)}"
        )


@router.get("/supported-indicators")
async def get_supported_indicators():
    """
    Get list of supported technical indicators and timeframes.

    Returns:
        Dictionary with supported timeframes and indicators
    """
    return {
        "timeframes": SUPPORTED_TIMEFRAMES,
        "indicators": DEFAULT_INDICATORS,
        "description": "Multi-timeframe technical indicators for financial datasets"
    }


@router.post("/{dataset_id}/calculate-fundamentals")
async def calculate_fundamental_features(
    dataset_id: int,
    metrics: List[str] = None,
    db: Session = Depends(get_db)
):
    """
    Calculate fundamental-derived features for a dataset.

    Creates features for each fundamental metric:
    - days_to_last_{metric}: Days since last reported value
    - last_{metric}: Most recent value
    - last_{metric}_percent: Percent change from previous period
    - days_to_next_{metric}: Estimated days to next report
    - next_{metric}_forecast: Simple forecast based on trend

    Args:
        dataset_id: Dataset ID
        metrics: List of metrics (default: ['fcf', 'pe', 'eps', 'revenue'])
        db: Database session

    Returns:
        Updated dataset with fundamental features
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Default metrics
        if metrics is None:
            metrics = ['fcf', 'pe', 'eps', 'revenue', 'de', 'roe']

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        df['Date'] = pd.to_datetime(df['Date'])

        logger.info(f"Calculating fundamental features for dataset {dataset_id}, ticker: {dataset.ticker}")

        # Calculate fundamental features
        result_df = FundamentalsService.create_fundamental_features(
            df, dataset.ticker, metrics
        )

        # Save updated dataset
        result_df.to_csv(file_path, index=False)

        # Count added columns
        added_columns = [col for col in result_df.columns if col not in df.columns]

        # Update dataset metadata
        fundamentals_config = dataset.fundamentals_config or {}
        fundamentals_config['calculated_metrics'] = metrics
        fundamentals_config['calculated_at'] = datetime.now().isoformat()
        fundamentals_config['feature_columns'] = added_columns
        dataset.fundamentals_config = fundamentals_config
        db.commit()
        db.refresh(dataset)

        logger.info(f"Successfully calculated {len(added_columns)} fundamental features for dataset {dataset_id}")

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "metrics": metrics,
            "features_added": added_columns,
            "total_columns": len(result_df.columns),
            "rows": len(result_df),
            "message": f"Successfully calculated {len(added_columns)} fundamental features"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating fundamental features: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate fundamental features: {str(e)}"
        )


@router.get("/{dataset_id}/fundamentals")
async def get_fundamental_data(dataset_id: int, db: Session = Depends(get_db)):
    """
    Get fundamental data for the ticker in a dataset.

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Fundamental data for the ticker
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        fund_data = FundamentalsService.get_fundamental_data(dataset.ticker)

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "fundamentals": fund_data
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting fundamental data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get fundamental data: {str(e)}"
        )


@router.post("/{dataset_id}/calculate-macro")
async def calculate_macro_features(
    dataset_id: int,
    indicators: List[str] = None,
    include_yield_curve: bool = True,
    db: Session = Depends(get_db)
):
    """
    Integrate macro economic data with OHLC dataset using forward-fill.

    Fetches macroeconomic indicators (interest rates, GDP, inflation, etc.)
    and aligns them with the dataset's time series using forward-fill.

    Args:
        dataset_id: Dataset ID
        indicators: List of indicators (default: all available)
        include_yield_curve: Include yield curve features (default: True)
        db: Database session

    Returns:
        Updated dataset with macro features
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        df['Date'] = pd.to_datetime(df['Date'])

        logger.info(f"Calculating macro features for dataset {dataset_id}")

        # Initialize macro service
        macro_service = MacroService()

        # Integrate macro data
        result_df = macro_service.integrate_macro_with_ohlc(df, indicators)

        # Add yield curve features if requested
        if include_yield_curve:
            result_df = macro_service.create_yield_curve_features(result_df)

        # Save updated dataset
        result_df.to_csv(file_path, index=False)

        # Count added columns
        added_columns = [col for col in result_df.columns if col not in df.columns]

        logger.info(f"Successfully calculated {len(added_columns)} macro features for dataset {dataset_id}")

        return {
            "dataset_id": dataset_id,
            "indicators": indicators or list(MacroService.MACRO_INDICATORS.keys()),
            "include_yield_curve": include_yield_curve,
            "features_added": added_columns,
            "total_columns": len(result_df.columns),
            "rows": len(result_df),
            "message": f"Successfully calculated {len(added_columns)} macro features"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating macro features: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate macro features: {str(e)}"
        )


@router.get("/supported-macro-indicators")
async def get_supported_macro_indicators():
    """
    Get list of supported macroeconomic indicators.

    Returns:
        Dictionary of available macro indicators with metadata
    """
    return {
        "indicators": MacroService.get_supported_indicators(),
        "description": "Macroeconomic indicators from FRED (Federal Reserve Economic Data)"
    }


@router.post("/{dataset_id}/calculate-sentiment")
async def calculate_sentiment_features(
    dataset_id: int,
    db: Session = Depends(get_db)
):
    """
    Calculate sentiment features for a dataset.

    Fetches news articles for the ticker, runs sentiment analysis using
    Transformers (FinBERT), and creates aggregated sentiment features:
    - news_1d_positive_short, news_1d_positive_medium, news_1d_positive_long
    - news_1w_negative_short, news_1w_negative_medium, news_1w_negative_long
    - And combinations for 1d, 1w, 1m, 6m periods

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Updated dataset with sentiment features
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        df['Date'] = pd.to_datetime(df['Date'])

        logger.info(f"Calculating sentiment features for dataset {dataset_id}, ticker: {dataset.ticker}")

        # Initialize sentiment service
        sentiment_service = SentimentService()

        # Get date range
        start_date = df['Date'].min()
        end_date = df['Date'].max()

        # Fetch news articles
        news_articles = sentiment_service.fetch_news_for_ticker(
            dataset.ticker, start_date, end_date
        )

        # Analyze sentiment
        analyzed_articles = sentiment_service.analyze_news_articles(news_articles)

        # Create sentiment features
        result_df = sentiment_service.create_sentiment_features(df, analyzed_articles)

        # Save updated dataset
        result_df.to_csv(file_path, index=False)

        # Count added columns
        added_columns = [col for col in result_df.columns if col not in df.columns]

        # Update dataset metadata
        sentiment_config = dataset.sentiment_config or {}
        sentiment_config['calculated_at'] = datetime.now().isoformat()
        sentiment_config['articles_analyzed'] = len(analyzed_articles)
        sentiment_config['feature_columns'] = added_columns
        dataset.sentiment_config = sentiment_config
        db.commit()
        db.refresh(dataset)

        logger.info(f"Successfully calculated {len(added_columns)} sentiment features for dataset {dataset_id}")

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "articles_analyzed": len(analyzed_articles),
            "features_added": added_columns,
            "total_columns": len(result_df.columns),
            "rows": len(result_df),
            "message": f"Successfully calculated {len(added_columns)} sentiment features from {len(analyzed_articles)} articles"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating sentiment features: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate sentiment features: {str(e)}"
        )


@router.get("/sentiment-feature-descriptions")
async def get_sentiment_feature_descriptions():
    """
    Get descriptions for all sentiment features.

    Returns:
        Dictionary mapping feature names to descriptions
    """
    return {
        "features": SentimentService.get_feature_descriptions(),
        "lookback_periods": SentimentService.LOOKBACK_PERIODS,
        "sentiment_categories": SentimentService.SENTIMENT_CATEGORIES,
        "description": "Aggregated news sentiment features for ML model training"
    }


@router.post("/{dataset_id}/analyze-news")
async def analyze_news_for_dataset(
    dataset_id: int,
    db: Session = Depends(get_db)
):
    """
    Fetch and analyze news articles for a dataset's ticker.

    Args:
        dataset_id: Dataset ID
        db: Database session

    Returns:
        Analyzed news articles with sentiment
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()

        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        # Load dataset to get date range
        file_path = Path(dataset.file_path)
        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset file not found: {file_path}"
            )

        df = pd.read_csv(file_path)
        df['Date'] = pd.to_datetime(df['Date'])

        start_date = df['Date'].min()
        end_date = df['Date'].max()

        # Fetch and analyze news
        sentiment_service = SentimentService()
        news_articles = sentiment_service.fetch_news_for_ticker(
            dataset.ticker, start_date, end_date
        )
        analyzed_articles = sentiment_service.analyze_news_articles(news_articles)

        # Convert dates to strings for JSON serialization
        for article in analyzed_articles:
            if isinstance(article.get('date'), datetime):
                article['date'] = article['date'].isoformat()

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "article_count": len(analyzed_articles),
            "articles": analyzed_articles
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing news: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze news: {str(e)}"
        )


# ============= Multi-Dataset Endpoints =============

@router.post("/validate-compatibility")
async def validate_dataset_compatibility(
    dataset_ids: List[int],
    db: Session = Depends(get_db)
):
    """
    Validate compatibility of multiple datasets for combined training.

    Checks:
    - All datasets exist
    - Timeframes match
    - Compatible date ranges
    - Compatible feature columns

    Args:
        dataset_ids: List of dataset IDs to validate
        db: Database session

    Returns:
        Compatibility report with warnings and combined statistics
    """
    if len(dataset_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least 2 datasets required for compatibility check"
        )

    datasets = []
    for dataset_id in dataset_ids:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )
        datasets.append(dataset)

    # Check timeframe compatibility
    timeframes = set(d.timeframe for d in datasets)
    timeframe_compatible = len(timeframes) == 1

    # Check tickers
    tickers = list(set(d.ticker for d in datasets))

    # Load and analyze features
    feature_sets = []
    date_ranges = []
    total_rows = 0

    for dataset in datasets:
        file_path = Path(dataset.file_path)
        if file_path.exists():
            df = pd.read_csv(file_path)
            feature_sets.append(set(df.columns.tolist()))
            total_rows += len(df)

            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'])
                date_ranges.append({
                    'dataset_id': dataset.id,
                    'ticker': dataset.ticker,
                    'start': df['Date'].min().isoformat(),
                    'end': df['Date'].max().isoformat(),
                    'rows': len(df)
                })

    # Find common features
    if feature_sets:
        common_features = set.intersection(*feature_sets)
        all_features = set.union(*feature_sets)
        missing_features = {ds.id: list(all_features - fs) for ds, fs in zip(datasets, feature_sets)}
    else:
        common_features = set()
        all_features = set()
        missing_features = {}

    # Build warnings
    warnings = []
    if not timeframe_compatible:
        warnings.append(f"Timeframes do not match: {', '.join(timeframes)}")
    if len(tickers) > 1:
        warnings.append(f"Multiple tickers: {', '.join(tickers)} - training will handle each separately")
    if len(common_features) < len(all_features):
        warnings.append(f"{len(all_features) - len(common_features)} features not present in all datasets")

    return {
        "compatible": timeframe_compatible and len(common_features) > 5,
        "dataset_count": len(datasets),
        "tickers": tickers,
        "timeframe_match": timeframe_compatible,
        "timeframe": list(timeframes)[0] if timeframe_compatible else None,
        "common_features": len(common_features),
        "total_features": len(all_features),
        "total_rows": total_rows,
        "date_ranges": date_ranges,
        "missing_features_by_dataset": missing_features,
        "warnings": warnings
    }


@router.post("/combine-preview")
async def combine_datasets_preview(
    dataset_ids: List[int],
    db: Session = Depends(get_db)
):
    """
    Preview combined statistics for multiple datasets.

    Args:
        dataset_ids: List of dataset IDs to combine
        db: Database session

    Returns:
        Combined statistics and chronological overview
    """
    if len(dataset_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least 2 datasets required"
        )

    datasets = []
    for dataset_id in dataset_ids:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )
        datasets.append(dataset)

    # Load and combine data
    combined_stats = []
    all_dates = []
    total_rows = 0

    for dataset in datasets:
        file_path = Path(dataset.file_path)
        if file_path.exists():
            df = pd.read_csv(file_path)
            df['Date'] = pd.to_datetime(df['Date'])

            stats = {
                'dataset_id': dataset.id,
                'name': dataset.name,
                'ticker': dataset.ticker,
                'timeframe': dataset.timeframe,
                'rows': len(df),
                'start_date': df['Date'].min().isoformat(),
                'end_date': df['Date'].max().isoformat(),
                'columns': len(df.columns)
            }

            # Add numeric column stats
            numeric_cols = df.select_dtypes(include=['float64', 'int64']).columns.tolist()
            if 'Close' in df.columns:
                stats['price_range'] = {
                    'min': float(df['Close'].min()),
                    'max': float(df['Close'].max()),
                    'mean': float(df['Close'].mean())
                }

            combined_stats.append(stats)
            all_dates.extend(df['Date'].tolist())
            total_rows += len(df)

    # Calculate combined timeline
    if all_dates:
        all_dates = sorted(set(all_dates))
        timeline = {
            'start': all_dates[0].isoformat(),
            'end': all_dates[-1].isoformat(),
            'unique_dates': len(all_dates)
        }
    else:
        timeline = None

    return {
        "datasets": combined_stats,
        "combined": {
            "total_datasets": len(datasets),
            "total_rows": total_rows,
            "timeline": timeline,
            "unique_tickers": list(set(d.ticker for d in datasets))
        }
    }


@router.post("/check-timeframe-match")
async def check_timeframe_match(
    dataset_ids: List[int],
    db: Session = Depends(get_db)
):
    """
    Check if datasets have matching timeframes.

    Args:
        dataset_ids: List of dataset IDs to check
        db: Database session

    Returns:
        Timeframe compatibility status
    """
    if len(dataset_ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least 2 datasets required"
        )

    timeframe_info = []
    for dataset_id in dataset_ids:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )
        timeframe_info.append({
            'dataset_id': dataset.id,
            'name': dataset.name,
            'ticker': dataset.ticker,
            'timeframe': dataset.timeframe
        })

    timeframes = set(d['timeframe'] for d in timeframe_info)

    return {
        "match": len(timeframes) == 1,
        "common_timeframe": list(timeframes)[0] if len(timeframes) == 1 else None,
        "timeframes_found": list(timeframes),
        "datasets": timeframe_info,
        "message": (
            f"All datasets use {list(timeframes)[0]} timeframe"
            if len(timeframes) == 1
            else f"Timeframe mismatch: {', '.join(timeframes)}"
        )
    }


@router.get("/{dataset_id}/sentiment")
async def get_dataset_sentiment(
    dataset_id: int,
    provider: str = Query("fmp", description="News provider (fmp, alphavantage, google, finnhub, alpaca)"),
    db: Session = Depends(get_db)
):
    """
    Get sentiment markers for a dataset.

    Fetches real news articles for the dataset's ticker and date range,
    analyzes sentiment, and returns markers for chart visualization.

    Args:
        dataset_id: Dataset ID
        provider: News provider to use (fmp, alphavantage, google, finnhub, alpaca)
        db: Database session

    Returns:
        List of sentiment markers with date, sentiment, score, headline, source
    """
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset with ID {dataset_id} not found"
            )

        logger.info(f"Fetching sentiment for dataset {dataset_id} ({dataset.ticker}) using {provider}")

        sentiment_service = SentimentService()

        # Fetch news articles
        articles = sentiment_service.fetch_news_for_ticker(
            ticker=dataset.ticker,
            start_date=dataset.start_date,
            end_date=dataset.end_date,
            provider=provider
        )

        # Analyze sentiment for each article
        analyzed = sentiment_service.analyze_news_articles(articles)

        # Convert to markers for chart
        markers = []
        for article in analyzed:
            # Handle date conversion
            article_date = article.get('date', '')
            if isinstance(article_date, datetime):
                date_str = article_date.isoformat()
            else:
                date_str = str(article_date)

            markers.append({
                "date": date_str,
                "sentiment": article.get('sentiment', 'neutral'),
                "score": article.get('sentiment_score', 0.5),
                "headline": article.get('title', ''),
                "source": article.get('source', 'Unknown')
            })

        logger.info(f"Returning {len(markers)} sentiment markers for dataset {dataset_id}")

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "provider": provider,
            "markers": markers,
            "total_count": len(markers),
            "is_mock": any(m['source'].startswith('Mock') for m in markers)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sentiment for dataset {dataset_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch sentiment: {str(e)}"
        )


@router.get("/{dataset_id}/trends")
async def get_dataset_trends(
    dataset_id: int,
    method: str = "moving_average",
    lookback_period: int = 20,
    prediction_horizon: int = 5,
    fast_period: int = 10,
    slow_period: int = 30,
    trend_threshold: float = 25.0,
    db: Session = Depends(get_db)
):
    """
    Calculate and return trend analysis for a dataset.

    This endpoint analyzes the dataset's price data to detect trends
    using various methods. Results can be used for visualization or
    as targets for ML training.

    Args:
        dataset_id: ID of the dataset
        method: Detection method (moving_average, linear_regression, adx, pivot_points, donchian)
        lookback_period: Period for trend detection
        prediction_horizon: How many periods ahead to predict
        fast_period: Fast MA period (for moving_average method)
        slow_period: Slow MA period (for moving_average method)
        trend_threshold: Threshold for ADX method

    Returns:
        Trend analysis results with statistics and visualization data
    """
    from app.services.trend_targets import TrendTargetService

    try:
        # Get dataset
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset {dataset_id} not found"
            )

        # Check if file exists
        if not dataset.file_path or not Path(dataset.file_path).exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dataset file not found"
            )

        # Load dataset
        df = pd.read_csv(dataset.file_path)

        # Ensure we have required columns
        required_cols = ['Date', 'Open', 'High', 'Low', 'Close']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dataset missing required columns: {missing_cols}"
            )

        # Calculate trends
        trend_service = TrendTargetService()
        df_with_trends = trend_service.calculate_trend_targets(
            df=df,
            method=method,
            lookback_period=lookback_period,
            prediction_horizon=prediction_horizon,
            fast_period=fast_period,
            slow_period=slow_period,
            trend_strength_threshold=trend_threshold,
            include_strength=True
        )

        # Get statistics
        stats = trend_service.get_trend_statistics(df_with_trends)

        # Get visualization data (limit to avoid huge response)
        viz_data = trend_service.get_trend_visualization_data(df_with_trends)

        return {
            "dataset_id": dataset_id,
            "ticker": dataset.ticker,
            "method": method,
            "parameters": {
                "lookback_period": lookback_period,
                "prediction_horizon": prediction_horizon,
                "fast_period": fast_period,
                "slow_period": slow_period,
                "trend_threshold": trend_threshold
            },
            "statistics": stats,
            "trends": viz_data,
            "total_rows": len(df_with_trends)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating trends for dataset {dataset_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate trends: {str(e)}"
        )
