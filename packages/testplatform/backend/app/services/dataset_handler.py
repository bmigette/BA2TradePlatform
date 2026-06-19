"""
Dataset Background Processing Handler

Handles dataset regeneration tasks in the background using the TaskQueueService.
Uses short-lived database sessions to prevent database locking.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

import pandas as pd

from app.models.database import SessionLocal
from app.models.dataset import Dataset, DatasetStatus
from app.indicators import TechnicalIndicators
from app.services.fundamentals import FundamentalsService
from app.services.macro import MacroService
from app.services.sentiment import SentimentService
from app.services.task_queue import get_task_queue

logger = logging.getLogger(__name__)


# ============================================================================
# Shared Constants
# ============================================================================

# Bars per day for each timeframe (trading hours ~6.5h/day for stocks)
BARS_PER_DAY = {
    '1m': 390,    # 6.5h * 60
    '5m': 78,     # 6.5h * 12
    '15m': 26,    # 6.5h * 4
    '30m': 13,    # 6.5h * 2
    '1h': 7,      # ~6.5h (rounded)
    '2h': 3,      # ~3
    '4h': 2,      # ~2 (might span multiple days)
    '1d': 1,
    'D1': 1,
    '1w': 0.2,    # 1/5 (5 trading days per week)
    'W1': 0.2,
    '1mo': 0.05,  # ~1/20 trading days per month
}

# Timeframe to provider interval mapping
INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1wk", "1mo": "1mo"
}


# ============================================================================
# Shared Helper Functions
# ============================================================================

def calculate_warmup_period(indicators: list, timeframe: str) -> tuple[int, int]:
    """
    Calculate warmup period needed for indicators.

    Args:
        indicators: List of indicator configs with 'period' and/or 'slow' fields
        timeframe: Dataset timeframe (e.g., '1h', '15m')

    Returns:
        Tuple of (warmup_bars, warmup_days)
    """
    max_period = 0
    if indicators:
        for ind in indicators:
            period = ind.get('period', 0)
            slow = ind.get('slow', 0)
            max_period = max(max_period, period, slow)

    if max_period == 0:
        return 0, 0

    bars_per_day = BARS_PER_DAY.get(timeframe, 1)
    warmup_bars = max_period + 50  # Extra buffer for data gaps
    warmup_days = int(warmup_bars / max(bars_per_day, 0.1) * 1.5)  # 1.5x for weekends/holidays

    return warmup_bars, warmup_days


def apply_technical_indicators(df: pd.DataFrame, indicators: list) -> pd.DataFrame:
    """
    Apply technical indicators to a DataFrame.

    Converts indicator list format to dict format expected by TechnicalIndicators
    and calculates all indicators.

    Args:
        df: DataFrame with OHLCV columns
        indicators: List of indicator configs, e.g.:
            [{"type": "sma", "name": "SMA 20", "period": 20}, ...]

    Returns:
        DataFrame with indicator columns added
    """
    if not indicators:
        return df

    indicators_dict = {}
    for ind in indicators:
        ind_type = ind.get('type', ind.get('name', 'unknown'))
        period = ind.get('period')
        timeframe = ind.get('timeframe', '')
        if period is not None:
            ind_name = f"{ind_type}_{period}"
        elif timeframe:
            ind_name = f"{ind_type}_{timeframe}"
        else:
            ind_name = ind_type
        indicators_dict[ind_name] = ind

    return TechnicalIndicators.add_indicators_to_dataframe(df, indicators_dict)


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add time-based features to OHLCV DataFrame.

    Adds:
    - day_of_week: 0=Monday, 6=Sunday
    - hour_of_day: 0-23

    These features can help models learn time-dependent patterns
    like day-of-week effects or intraday patterns.

    Args:
        df: DataFrame with 'Date' column (must be datetime)

    Returns:
        DataFrame with added time features
    """
    if 'Date' not in df.columns:
        logger.warning("Cannot add time features: 'Date' column not found")
        return df

    df = df.copy()

    # Ensure Date is datetime
    if not pd.api.types.is_datetime64_any_dtype(df['Date']):
        df['Date'] = pd.to_datetime(df['Date'])

    # Add time-based features
    df['day_of_week'] = df['Date'].dt.dayofweek  # 0=Monday, 6=Sunday
    df['hour_of_day'] = df['Date'].dt.hour        # 0-23

    logger.debug(f"Added time features: day_of_week and hour_of_day")
    return df


def update_dataset_progress(dataset_id: int, message: str, task_id: Optional[str] = None):
    """
    Update dataset progress message using a short-lived DB session.

    Args:
        dataset_id: Dataset ID to update
        message: Progress message to display
        task_id: Optional task ID for logging
    """
    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if dataset:
            dataset.progress_message = message
            db.commit()
            if task_id:
                logger.debug(f"[Task {task_id}] Progress: {message}")
    except Exception as e:
        logger.warning(f"Failed to update dataset progress: {e}")
        db.rollback()
    finally:
        db.close()


def create_progress_callback(dataset_id: int, task_id: str, base_message: str) -> Callable[[int, int, int], None]:
    """
    Create a progress callback for detailed progress updates.

    Args:
        dataset_id: Dataset ID to update
        task_id: Task ID for logging
        base_message: Base message prefix (e.g., "Fetching news")

    Returns:
        Callback function that accepts (processed, total, resolved)
    """
    def callback(processed: int, total: int, resolved: int = 0):
        pct = (processed / total * 100) if total > 0 else 0
        message = f"{base_message}: {processed}/{total} ({pct:.0f}%)"
        if resolved > 0:
            message += f" - {resolved} resolved"
        update_dataset_progress(dataset_id, message, task_id)

    return callback


def handle_dataset_regeneration(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Background task handler for dataset regeneration.

    This function is called by the TaskQueueService to process dataset
    regeneration in the background. It uses short-lived DB sessions
    to avoid database locking issues.

    Args:
        task_id: The background task ID
        payload: Task payload containing:
            - dataset_id: ID of the dataset to regenerate
            - ohlcv_data: List of OHLCV data points (already fetched)
            - ticker: Ticker symbol
            - timeframe: Timeframe string
            - start_date: Start date string (ISO format)
            - end_date: End date string (ISO format)

    Returns:
        Result dict with status and message
    """
    dataset_id = payload.get('dataset_id')
    ohlcv_data = payload.get('ohlcv_data', [])
    ticker = payload.get('ticker')

    logger.info(f"[Task {task_id}] Starting dataset regeneration for dataset {dataset_id}")
    update_dataset_progress(dataset_id, "Starting dataset regeneration...", task_id)

    try:
        # Convert OHLCV data to DataFrame
        df = pd.DataFrame(ohlcv_data)
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values('Date').reset_index(drop=True)

        # Add time-based features (day_of_week, hour_of_day)
        df = add_time_features(df)

        logger.info(f"[Task {task_id}] Processing {len(df)} OHLCV data points")
        update_dataset_progress(dataset_id, f"Loaded {len(df)} OHLCV data points", task_id)

        # Get dataset config with a short-lived session
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if not dataset:
                raise ValueError(f"Dataset {dataset_id} not found")

            technical_indicators = dataset.technical_indicators
            sentiment_config = dataset.sentiment_config
            fundamentals_config = dataset.fundamentals_config
            file_path = dataset.file_path
        finally:
            db.close()

        # Update task queue progress
        task_queue = get_task_queue()
        task_queue.update_progress(task_id, 10.0, "Applying technical indicators...")

        # Apply technical indicators
        if technical_indicators:
            num_indicators = len(technical_indicators)
            update_dataset_progress(dataset_id, f"Applying {num_indicators} technical indicators...", task_id)
            logger.info(f"[Task {task_id}] Applying {num_indicators} technical indicators...")

            try:
                indicators_dict = {}
                for i, indicator in enumerate(technical_indicators):
                    indicator_type = indicator.get('type', indicator.get('name', 'unknown'))
                    indicator_name = indicator.get('name', f"{indicator_type}_{indicator.get('period', '')}")
                    indicators_dict[indicator_name] = indicator

                    # Update progress for each indicator
                    if (i + 1) % 5 == 0 or i == num_indicators - 1:
                        update_dataset_progress(
                            dataset_id,
                            f"Applying indicators: {i + 1}/{num_indicators}",
                            task_id
                        )

                df = TechnicalIndicators.add_indicators_to_dataframe(df, indicators_dict)
                update_dataset_progress(
                    dataset_id,
                    f"Applied {num_indicators} indicators ({len(df.columns)} columns)",
                    task_id
                )
                logger.info(f"[Task {task_id}] Added technical indicators. DataFrame now has {len(df.columns)} columns")
            except Exception as e:
                logger.error(f"[Task {task_id}] Error applying technical indicators: {e}")
                update_dataset_progress(dataset_id, f"Warning: Error applying indicators: {str(e)[:100]}", task_id)
        else:
            update_dataset_progress(dataset_id, "No technical indicators to apply", task_id)

        task_queue.update_progress(task_id, 30.0, "Fetching sentiment data...")

        # Fetch and add sentiment features
        if sentiment_config and sentiment_config.get('enabled'):
            update_dataset_progress(dataset_id, "Fetching sentiment/news data...", task_id)
            logger.info(f"[Task {task_id}] Fetching sentiment data...")

            try:
                sentiment_service = SentimentService()

                news_sources = sentiment_config.get('news_sources', [])
                if not news_sources:
                    legacy_provider = sentiment_config.get('provider', 'fmp')
                    news_sources = [legacy_provider]

                logger.info(f"[Task {task_id}] Fetching news from {len(news_sources)} source(s): {news_sources}")
                update_dataset_progress(
                    dataset_id,
                    f"Fetching news from {len(news_sources)} source(s)...",
                    task_id
                )

                all_articles = []
                start_date = df['Date'].min()
                end_date = df['Date'].max()
                use_cached_news = sentiment_config.get('use_cached_news', False)

                if use_cached_news:
                    # Use pre-fetched articles from the news cache DB
                    from app.services.news_cache import NewsCacheService
                    cache_service = NewsCacheService()

                    for idx, source in enumerate(news_sources):
                        provider = source.replace('_news', '').replace('_company', '').replace('_global', '')
                        update_dataset_progress(
                            dataset_id,
                            f"Loading cached news from {provider} ({idx + 1}/{len(news_sources)})...",
                            task_id
                        )

                        try:
                            sd = start_date.to_pydatetime() if hasattr(start_date, 'to_pydatetime') else start_date
                            ed = end_date.to_pydatetime() if hasattr(end_date, 'to_pydatetime') else end_date
                            articles = cache_service.get_cached_articles_for_ticker(
                                ticker=ticker,
                                provider=provider,
                                start_date=sd,
                                end_date=ed
                            )
                            if articles:
                                logger.info(f"[Task {task_id}] Loaded {len(articles)} cached articles from {provider}")
                                update_dataset_progress(
                                    dataset_id,
                                    f"Loaded {len(articles)} cached articles from {provider}",
                                    task_id
                                )
                                all_articles.extend(articles)
                            else:
                                logger.warning(f"[Task {task_id}] No cached articles found for {ticker}/{provider}")
                                update_dataset_progress(
                                    dataset_id,
                                    f"Warning: No cached news for {ticker} from {provider}",
                                    task_id
                                )
                        except Exception as e:
                            logger.warning(f"[Task {task_id}] Error loading cached news from {provider}: {e}")
                            update_dataset_progress(
                                dataset_id,
                                f"Warning: Error loading cached news from {provider}",
                                task_id
                            )
                else:
                    # Fetch live from API
                    # Create progress callback for detailed news fetching progress
                    progress_callback = create_progress_callback(
                        dataset_id, task_id, "Resolving news URLs"
                    )

                    for idx, source in enumerate(news_sources):
                        provider = source.replace('_news', '').replace('_company', '').replace('_global', '')
                        update_dataset_progress(
                            dataset_id,
                            f"Fetching news from {provider} ({idx + 1}/{len(news_sources)})...",
                            task_id
                        )

                        try:
                            articles = sentiment_service.fetch_news_for_ticker(
                                ticker=ticker,
                                start_date=start_date if hasattr(start_date, 'to_pydatetime') else start_date,
                                end_date=end_date if hasattr(end_date, 'to_pydatetime') else end_date,
                                provider=provider,
                                enrich_content=sentiment_config.get('enrich_content', True),
                                progress_callback=progress_callback
                            )
                            if articles:
                                logger.info(f"[Task {task_id}] Fetched {len(articles)} articles from {provider}")
                                update_dataset_progress(
                                    dataset_id,
                                    f"Fetched {len(articles)} articles from {provider}",
                                    task_id
                                )
                                all_articles.extend(articles)
                        except TypeError:
                            # Fallback if progress_callback not supported
                            articles = sentiment_service.fetch_news_for_ticker(
                                ticker=ticker,
                                start_date=start_date if hasattr(start_date, 'to_pydatetime') else start_date,
                                end_date=end_date if hasattr(end_date, 'to_pydatetime') else end_date,
                                provider=provider,
                                enrich_content=sentiment_config.get('enrich_content', True)
                            )
                            if articles:
                                all_articles.extend(articles)
                        except Exception as e:
                            logger.warning(f"[Task {task_id}] Error fetching from {provider}: {e}")
                            update_dataset_progress(
                                dataset_id,
                                f"Warning: Error fetching from {provider}",
                                task_id
                            )

                if all_articles:
                    update_dataset_progress(
                        dataset_id,
                        f"Creating sentiment features from {len(all_articles)} articles...",
                        task_id
                    )
                    df = sentiment_service.create_sentiment_features(df, all_articles)
                    logger.info(f"[Task {task_id}] Added sentiment features from {len(all_articles)} articles")
                    update_dataset_progress(
                        dataset_id,
                        f"Added sentiment features from {len(all_articles)} articles",
                        task_id
                    )

                    # Update sentiment config with articles count
                    db = SessionLocal()
                    try:
                        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
                        if dataset:
                            updated_sentiment_config = dict(dataset.sentiment_config) if dataset.sentiment_config else {}
                            updated_sentiment_config['articles_count'] = len(all_articles)
                            dataset.sentiment_config = updated_sentiment_config
                            db.commit()
                    finally:
                        db.close()
                else:
                    logger.warning(f"[Task {task_id}] No news articles found")
                    update_dataset_progress(dataset_id, "No news articles found", task_id)
                    db = SessionLocal()
                    try:
                        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
                        if dataset:
                            updated_sentiment_config = dict(dataset.sentiment_config) if dataset.sentiment_config else {}
                            updated_sentiment_config['articles_count'] = 0
                            dataset.sentiment_config = updated_sentiment_config
                            db.commit()
                    finally:
                        db.close()

            except Exception as e:
                logger.error(f"[Task {task_id}] Error fetching sentiment: {e}")
                update_dataset_progress(dataset_id, f"Warning: Sentiment error: {str(e)[:100]}", task_id)
        else:
            update_dataset_progress(dataset_id, "Sentiment analysis not enabled", task_id)

        task_queue.update_progress(task_id, 60.0, "Fetching fundamentals data...")

        # Fetch and add fundamentals
        if fundamentals_config and fundamentals_config.get('enabled'):
            update_dataset_progress(dataset_id, "Fetching fundamentals data...", task_id)
            logger.info(f"[Task {task_id}] Fetching fundamentals data...")

            try:
                statement_types = fundamentals_config.get('statement_types')
                if statement_types:
                    lookback_statements = fundamentals_config.get('lookback_statements', 2)
                    providers = fundamentals_config.get('fundamentals_providers', ['yfinance'])

                    update_dataset_progress(
                        dataset_id,
                        f"Creating statement features ({', '.join(statement_types)})...",
                        task_id
                    )
                    logger.info(f"[Task {task_id}] Creating statement features: types={statement_types}")

                    df = FundamentalsService.create_statement_features_v2(
                        df=df,
                        ticker=ticker,
                        statement_types=statement_types,
                        providers=providers,
                        frequency='quarterly'
                    )

                    statement_cols = [c for c in df.columns if any(c.startswith(p + '_q') for p in ['bs', 'is', 'cf', 'earn'])]
                    update_dataset_progress(
                        dataset_id,
                        f"Added {len(statement_cols)} statement feature columns",
                        task_id
                    )
                    logger.info(f"[Task {task_id}] Added {len(statement_cols)} statement feature columns")
                else:
                    # Legacy mode
                    update_dataset_progress(dataset_id, "Fetching current fundamentals...", task_id)
                    fundamentals = FundamentalsService.get_fundamental_data(ticker)
                    if fundamentals and fundamentals.get('current'):
                        current = fundamentals['current']
                        added_count = 0
                        for key, value in current.items():
                            if value is not None:
                                df[f'fundamental_{key}'] = value
                                added_count += 1
                        update_dataset_progress(
                            dataset_id,
                            f"Added {added_count} fundamental columns",
                            task_id
                        )

                # Fetch macro indicators
                macro_indicators = fundamentals_config.get('macro_indicators', [])
                if macro_indicators:
                    update_dataset_progress(
                        dataset_id,
                        f"Fetching macro indicators ({len(macro_indicators)})...",
                        task_id
                    )
                    logger.info(f"[Task {task_id}] Fetching macro indicators: {macro_indicators}")

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
                        update_dataset_progress(
                            dataset_id,
                            f"Added macro indicators: {', '.join(macro_indicators)}",
                            task_id
                        )
                    except Exception as e:
                        logger.warning(f"[Task {task_id}] Error fetching macro data: {e}")
                        update_dataset_progress(dataset_id, f"Warning: Macro error: {str(e)[:50]}", task_id)

            except Exception as e:
                logger.error(f"[Task {task_id}] Error fetching fundamentals: {e}")
                update_dataset_progress(dataset_id, f"Warning: Fundamentals error: {str(e)[:100]}", task_id)
        else:
            update_dataset_progress(dataset_id, "Fundamentals not enabled", task_id)

        task_queue.update_progress(task_id, 85.0, "Saving dataset file...")
        update_dataset_progress(dataset_id, f"Saving dataset ({len(df)} rows, {len(df.columns)} columns)...", task_id)

        # Save to file
        file_path_obj = Path(file_path)
        file_path_obj.parent.mkdir(exist_ok=True)
        df.to_csv(file_path_obj, index=False)
        logger.info(f"[Task {task_id}] Saved dataset to {file_path} with {len(df.columns)} columns")

        task_queue.update_progress(task_id, 95.0, "Finalizing...")
        update_dataset_progress(dataset_id, "Finalizing dataset...", task_id)

        # Update dataset record with short-lived session
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.start_date = df['Date'].min()
                dataset.end_date = df['Date'].max()
                dataset.rows_count = len(df)
                dataset.status = DatasetStatus.READY.value
                dataset.error_message = None
                dataset.progress_message = None  # Clear progress on completion

                # Update generation config
                gen_config = dict(dataset.generation_config) if dataset.generation_config else {}
                gen_config["updated_at"] = datetime.now().isoformat()
                gen_config["background_task_id"] = task_id
                dataset.generation_config = gen_config

                db.commit()
                logger.info(f"[Task {task_id}] Dataset {dataset_id} updated successfully with {len(df)} rows and {len(df.columns)} columns")
        finally:
            db.close()

        return {
            "status": "success",
            "dataset_id": dataset_id,
            "rows": len(df),
            "columns": len(df.columns)
        }

    except Exception as e:
        logger.error(f"[Task {task_id}] Dataset regeneration failed: {e}")

        # Update dataset status to error
        db = SessionLocal()
        try:
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
            if dataset:
                dataset.status = DatasetStatus.ERROR.value
                dataset.error_message = str(e)
                dataset.progress_message = None  # Clear progress on error
                db.commit()
        finally:
            db.close()

        raise
