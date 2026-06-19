"""Tests for news batch fetch handler."""
import pytest
from unittest.mock import MagicMock, patch

from app.services.news_batch_handler import handle_news_batch_fetch


@pytest.fixture
def mock_task_queue():
    return MagicMock()


class TestNewsBatchHandler:
    """Tests for handle_news_batch_fetch background task handler."""

    def test_missing_provider_returns_failed(self, mock_task_queue):
        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue):
            result = handle_news_batch_fetch('t1', {
                'symbols': ['AAPL'],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })
        assert result['status'] == 'failed'
        assert 'provider' in result['error']

    def test_missing_symbol_returns_failed(self, mock_task_queue):
        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': [],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })
        assert result['status'] == 'failed'

    def test_missing_dates_returns_failed(self, mock_task_queue):
        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
            })
        assert result['status'] == 'failed'

    def test_invalid_date_format_returns_failed(self, mock_task_queue):
        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
                'start_date': 'Jan 1 2024',
                'end_date': '2024-03-01',
            })
        assert result['status'] == 'failed'
        assert 'date' in result['error'].lower()

    def test_start_after_end_returns_failed(self, mock_task_queue):
        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
                'start_date': '2024-03-01',
                'end_date': '2024-01-01',
            })
        assert result['status'] == 'failed'

    def test_successful_fetch_calls_sentiment_service(self, mock_task_queue):
        mock_articles = [
            {'title': 'Test', 'url': 'http://x.com/1', 'date': '2024-01-15',
             'content': 'positive earnings', 'sentiment': None}
        ]
        mock_sentiment_svc = MagicMock()
        mock_sentiment_svc.fetch_news_for_ticker.return_value = mock_articles
        mock_sentiment_svc.analyze_news_articles.return_value = mock_articles

        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.services.news_batch_handler.SentimentService',
                   return_value=mock_sentiment_svc):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })

        assert result['status'] == 'completed'
        mock_sentiment_svc.fetch_news_for_ticker.assert_called_once()
        mock_sentiment_svc.analyze_news_articles.assert_called_once()

    def test_already_analyzed_articles_skip_sentiment(self, mock_task_queue):
        """Articles that already have sentiment should not be re-analyzed."""
        mock_articles = [
            {'title': 'Test', 'url': 'http://x.com/1', 'sentiment': 'positive',
             'sentiment_score': 0.9}
        ]
        mock_sentiment_svc = MagicMock()
        mock_sentiment_svc.fetch_news_for_ticker.return_value = mock_articles

        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.services.news_batch_handler.SentimentService',
                   return_value=mock_sentiment_svc):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['AAPL'],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })

        assert result['status'] == 'completed'
        mock_sentiment_svc.analyze_news_articles.assert_not_called()

    def test_symbol_error_does_not_abort_batch(self, mock_task_queue):
        """A failing symbol should not prevent other symbols from being processed."""
        mock_sentiment_svc = MagicMock()
        mock_sentiment_svc.fetch_news_for_ticker.side_effect = [
            Exception("API error"),
            [{'title': 'OK', 'url': 'http://x.com/2', 'sentiment': 'neutral'}],
        ]
        mock_sentiment_svc.analyze_news_articles.return_value = []

        with patch('app.services.news_batch_handler.get_task_queue', return_value=mock_task_queue), \
             patch('app.services.news_batch_handler.SentimentService',
                   return_value=mock_sentiment_svc):
            result = handle_news_batch_fetch('t1', {
                'provider': 'fmp',
                'symbols': ['FAIL', 'GOOD'],
                'start_date': '2024-01-01',
                'end_date': '2024-03-01',
            })

        assert result['status'] == 'completed'
        assert result['results']['FAIL']['status'] == 'error'
        assert result['results']['GOOD']['status'] == 'success'
