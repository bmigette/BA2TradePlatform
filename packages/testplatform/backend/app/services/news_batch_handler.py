"""
News Batch Fetch Handler

Background task handler for bulk-fetching, enriching, and caching news articles
for multiple symbols over a date range.

Each article's webpage is fetched (or retrieved from Wayback Machine for articles
older than 1 year) and sentiment is analyzed via FinBERT before caching.
"""

import logging
from datetime import datetime
from typing import Dict, Any

from app.services.task_queue import get_task_queue
from app.services.sentiment import SentimentService

logger = logging.getLogger(__name__)


def handle_news_batch_fetch(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Background task handler for batch news fetching with content enrichment
    and sentiment analysis.

    For each symbol:
      1. Fetches articles from the provider (monthly chunks, deduped against cache).
      2. Enriches articles with full webpage content via trafilatura.
         Articles older than 1 year are tried via Wayback Machine first.
      3. Runs FinBERT sentiment analysis.
      4. Persists articles + sentiment to the news DB cache.

    Args:
        task_id: Task ID for progress tracking
        payload: Dict with keys:
            - provider: str (e.g., 'fmp', 'alphavantage', 'finnhub', 'alpaca') (required)
            - symbols: list[str] (required)
            - start_date: str 'YYYY-MM-DD' (required)
            - end_date: str 'YYYY-MM-DD' (required)

    Returns:
        Summary dict with status and per-symbol article counts.
    """
    task_queue = get_task_queue()

    provider = payload.get('provider')
    symbols = payload.get('symbols', [])
    raw_start = payload.get('start_date')
    raw_end = payload.get('end_date')

    if not provider:
        return {'status': 'failed', 'error': 'provider is required'}
    if not symbols:
        return {'status': 'failed', 'error': 'symbols list is required'}
    if not raw_start or not raw_end:
        return {'status': 'failed', 'error': 'start_date and end_date are required'}

    try:
        start_date = datetime.strptime(raw_start, '%Y-%m-%d')
        end_date = datetime.strptime(raw_end, '%Y-%m-%d')
    except ValueError as e:
        return {'status': 'failed', 'error': f'Invalid date format (expected YYYY-MM-DD): {e}'}

    if start_date >= end_date:
        return {'status': 'failed', 'error': 'start_date must be before end_date'}

    sentiment_service = SentimentService()
    results = {}
    total = len(symbols)

    for i, symbol in enumerate(symbols):
        symbol = symbol.strip().upper()
        if not symbol:
            results[symbol] = {'status': 'skipped', 'reason': 'empty symbol'}
            continue

        # Each symbol gets a progress slice: [base_progress, base_progress + per_symbol]
        per_symbol = 100.0 / total
        base_progress = i * per_symbol

        def on_progress(phase, pct, message):
            # Map fetch (0-70%), enrich (70-90%), cache (90-100%) into the symbol's slice
            # Fetch+enrich+cache = 85% of symbol slice, sentiment = remaining 15%
            scaled = base_progress + (pct / 100.0) * per_symbol * 0.85
            task_queue.update_progress(task_id, scaled, f"[{i+1}/{total}] {symbol}: {message}")

        task_queue.update_progress(
            task_id, base_progress,
            f"[{i+1}/{total}] Fetching news for {symbol}..."
        )

        try:
            articles = sentiment_service.fetch_news_for_ticker(
                ticker=symbol,
                start_date=start_date,
                end_date=end_date,
                provider=provider,
                enrich_content=True,
                use_cache=False,
                progress_callback=on_progress
            )

            task_queue.update_progress(
                task_id, base_progress + per_symbol * 0.85,
                f"[{i+1}/{total}] Analyzing sentiment for {symbol} "
                f"({len(articles)} articles)..."
            )

            analyzed_count = 0
            if articles:
                def on_sentiment_progress(done, article_total):
                    pct = base_progress + per_symbol * (0.85 + 0.12 * done / max(article_total, 1))
                    task_queue.update_progress(
                        task_id, pct,
                        f"[{i+1}/{total}] {symbol}: Sentiment {done}/{article_total}"
                    )

                analyzed = sentiment_service.analyze_news_articles(
                    articles, progress_callback=on_sentiment_progress
                )
                analyzed_count = len(analyzed)

                # Save/update all articles in cache (upserts by URL)
                task_queue.update_progress(
                    task_id, base_progress + per_symbol * 0.97,
                    f"[{i+1}/{total}] {symbol}: Caching {analyzed_count} articles..."
                )
                if sentiment_service._cache_service:
                    sentiment_service._cache_service.cache_articles_batch(
                        analyzed, provider, symbol
                    )

            results[symbol] = {
                'status': 'success',
                'total_articles': len(articles),
                'analyzed': analyzed_count,
            }
            logger.info(f"News batch {symbol}: {len(articles)} total, "
                        f"{analyzed_count} analyzed")

        except Exception as e:
            results[symbol] = {'status': 'error', 'error': str(e)}
            logger.error(f"Error in news batch fetch for {symbol}: {e}", exc_info=True)

    task_queue.update_progress(task_id, 100, "Completed")

    return {
        'status': 'completed',
        'provider': provider,
        'start_date': raw_start,
        'end_date': raw_end,
        'results': results,
    }
