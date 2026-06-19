"""
Tests for batch dataset creation.

Tests the batch creation endpoint, label generation, and edge cases.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


class TestBatchDatasetCreation:
    """Tests for the batch dataset creation endpoint."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = MagicMock()
        # Make add() store the dataset so we can inspect it
        db._added = []

        def track_add(obj):
            obj.id = len(db._added) + 1
            db._added.append(obj)

        db.add.side_effect = track_add
        db.commit = MagicMock()
        db.refresh = MagicMock()
        return db

    @pytest.fixture
    def mock_executor(self):
        """Create a mock thread pool executor."""
        executor = MagicMock()
        executor.submit = MagicMock()
        return executor

    def test_batch_creates_multiple_datasets(self, mock_db, mock_executor):
        """Test batch creation creates one dataset per symbol."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL', 'MSFT', 'GOOGL'],
                    'timeframe': '1d',
                    'data_provider': 'yfinance'
                }, mock_db)
            )

            assert result['count'] == 3
            assert len(result['created_ids']) == 3
            assert mock_executor.submit.call_count == 3

    def test_batch_auto_generates_batch_label(self, mock_db, mock_executor):
        """Test batch creation auto-generates batch label."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL'],
                    'timeframe': '1d',
                    'name': 'SP500'
                }, mock_db)
            )

            assert result['batch_label'] == 'batch-SP500'
            # Check that the dataset has the batch label
            added_dataset = mock_db._added[0]
            assert 'batch-SP500' in added_dataset.labels

    def test_batch_with_user_labels(self, mock_db, mock_executor):
        """Test batch creation merges user labels with batch label."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL'],
                    'timeframe': '1d',
                    'name': 'test',
                    'labels': ['daily', 'large-cap']
                }, mock_db)
            )

            added_dataset = mock_db._added[0]
            assert 'batch-test' in added_dataset.labels
            assert 'daily' in added_dataset.labels
            assert 'large-cap' in added_dataset.labels

    def test_batch_empty_symbols_raises_error(self, mock_db):
        """Test batch creation with empty symbols list raises HTTP 400."""
        import asyncio
        from fastapi import HTTPException
        from app.api.datasets import create_batch_datasets

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': [],
                    'timeframe': '1d'
                }, mock_db)
            )

        assert exc_info.value.status_code == 400
        assert 'symbols' in str(exc_info.value.detail).lower()

    def test_batch_skips_empty_symbols(self, mock_db, mock_executor):
        """Test batch creation skips empty/whitespace symbols."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL', '', '  ', 'MSFT'],
                    'timeframe': '1d'
                }, mock_db)
            )

            assert result['count'] == 2  # Only AAPL and MSFT

    def test_batch_timestamp_label_when_no_name(self, mock_db, mock_executor):
        """Test batch creates timestamp-based label when no name provided."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL'],
                    'timeframe': '1d'
                }, mock_db)
            )

            # batch_label should start with 'batch-' followed by timestamp
            assert result['batch_label'].startswith('batch-')
            # Should contain date-like pattern
            label_suffix = result['batch_label'].replace('batch-', '')
            assert len(label_suffix) >= 8  # At least YYYYMMDD

    def test_batch_symbols_uppercased(self, mock_db, mock_executor):
        """Test batch creation uppercases symbols."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            result = asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['aapl', 'msft'],
                    'timeframe': '1d'
                }, mock_db)
            )

            assert result['count'] == 2
            # Check tickers are uppercased
            tickers = [d.ticker for d in mock_db._added]
            assert 'AAPL' in tickers
            assert 'MSFT' in tickers

    def test_batch_dataset_name_format(self, mock_db, mock_executor):
        """Test batch dataset names follow expected format."""
        import asyncio
        from app.api.datasets import create_batch_datasets

        with patch('app.api.datasets._dataset_executor', mock_executor):
            asyncio.get_event_loop().run_until_complete(
                create_batch_datasets({
                    'symbols': ['AAPL'],
                    'timeframe': '4h'
                }, mock_db)
            )

            name = mock_db._added[0].name
            assert name.startswith('AAPL_4h_')
