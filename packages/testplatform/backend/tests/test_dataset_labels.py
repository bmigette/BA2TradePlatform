"""
Tests for dataset labels feature.

Tests label creation, retrieval, update, and migration.
"""

import pytest
from unittest.mock import patch, MagicMock
import json


class TestDatasetLabelModel:
    """Tests for Dataset model labels field."""

    def test_dataset_model_has_labels_field(self):
        """Test that Dataset model has a labels column."""
        from app.models.dataset import Dataset
        assert hasattr(Dataset, 'labels')

    def test_dataset_create_with_labels(self):
        """Test creating a Dataset object with labels."""
        from app.models.dataset import Dataset, DatasetStatus

        dataset = Dataset(
            name='test_dataset',
            ticker='AAPL',
            timeframe='1d',
            rows_count=0,
            status=DatasetStatus.BUILDING.value,
            labels=['batch-test', 'daily'],
            file_path='datasets/test.csv'
        )

        assert dataset.labels == ['batch-test', 'daily']
        assert dataset.name == 'test_dataset'

    def test_dataset_create_without_labels(self):
        """Test creating a Dataset object without labels defaults to None."""
        from app.models.dataset import Dataset, DatasetStatus

        dataset = Dataset(
            name='test_dataset',
            ticker='AAPL',
            timeframe='1d',
            rows_count=0,
            status=DatasetStatus.BUILDING.value,
            file_path='datasets/test.csv'
        )

        assert dataset.labels is None


class TestDatasetLabelSchema:
    """Tests for dataset schema labels field."""

    def test_dataset_create_schema_accepts_labels(self):
        """Test DatasetCreate schema accepts labels."""
        from app.schemas.dataset import DatasetCreate

        schema = DatasetCreate(
            ticker='AAPL',
            timeframe='1d',
            labels=['batch-SP500', 'test']
        )

        assert schema.labels == ['batch-SP500', 'test']

    def test_dataset_create_schema_labels_optional(self):
        """Test DatasetCreate schema labels are optional."""
        from app.schemas.dataset import DatasetCreate

        schema = DatasetCreate(
            ticker='AAPL',
            timeframe='1d'
        )

        assert schema.labels is None

    def test_dataset_update_schema_accepts_labels(self):
        """Test DatasetUpdate schema accepts labels."""
        from app.schemas.dataset import DatasetUpdate

        schema = DatasetUpdate(labels=['new-label'])

        assert schema.labels == ['new-label']

    def test_dataset_response_includes_labels(self):
        """Test DatasetResponse schema has labels field."""
        from app.schemas.dataset import DatasetResponse

        # Check the model_fields for labels
        assert 'labels' in DatasetResponse.model_fields


class TestDatasetLabelMigration:
    """Tests for the labels migration script."""

    def test_migration_upgrade(self):
        """Test that migration adds labels column."""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'db_migrate', '014_add_dataset_labels.py'
        )

        spec = importlib.util.spec_from_file_location("migration_014", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        # Mock cursor and connection
        mock_cursor = MagicMock()
        mock_conn = MagicMock()

        # Simulate column NOT existing
        mock_cursor.execute = MagicMock()
        mock_cursor.fetchall.return_value = [
            (0, 'id', 'INTEGER', 0, None, 1),
            (1, 'name', 'TEXT', 0, None, 0),
        ]

        result = migration.upgrade(mock_cursor, mock_conn)

        assert result is True
        # Should have called ALTER TABLE
        calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert any('ALTER TABLE' in str(c) and 'labels' in str(c) for c in calls)

    def test_migration_upgrade_column_exists(self):
        """Test migration skips if labels column already exists."""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'db_migrate', '014_add_dataset_labels.py'
        )

        spec = importlib.util.spec_from_file_location("migration_014", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        mock_cursor = MagicMock()
        mock_conn = MagicMock()

        # Simulate column already existing
        mock_cursor.fetchall.return_value = [
            (0, 'id', 'INTEGER', 0, None, 1),
            (1, 'labels', 'TEXT', 0, None, 0),
        ]

        result = migration.upgrade(mock_cursor, mock_conn)

        assert result is False
