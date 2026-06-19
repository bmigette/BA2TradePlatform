"""
Tests for epoch metrics handling in job_handler.

Ensures that empty epoch metrics (empty dict) don't create invalid
training history entries.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestEpochMetricsHandling:
    """Tests for update_job_training_state epoch_metrics handling."""

    @pytest.fixture
    def mock_jobs_store(self):
        """Create a mock jobs_store for testing."""
        return {
            "test-job-123": {
                "status": "running",
                "epochHistory": []
            }
        }

    def test_empty_epoch_metrics_not_added(self, mock_jobs_store):
        """Empty dict {} should not create an epoch entry."""
        # Patch at the source module where jobs_store is defined
        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            # Call with empty dict - should NOT add entry
            update_job_training_state(
                "test-job-123",
                current_epoch=1,
                epoch_metrics={}
            )

            assert len(mock_jobs_store["test-job-123"]["epochHistory"]) == 0

    def test_none_epoch_metrics_not_added(self, mock_jobs_store):
        """None should not create an epoch entry."""
        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            # Call with None - should NOT add entry
            update_job_training_state(
                "test-job-123",
                current_epoch=1,
                epoch_metrics=None
            )

            assert len(mock_jobs_store["test-job-123"]["epochHistory"]) == 0

    def test_valid_epoch_metrics_added(self, mock_jobs_store):
        """Valid metrics dict should create an epoch entry."""
        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            # Call with valid metrics - should add entry
            update_job_training_state(
                "test-job-123",
                current_epoch=5,
                epoch_metrics={
                    "train_loss": 0.5,
                    "val_loss": 0.6
                }
            )

            history = mock_jobs_store["test-job-123"]["epochHistory"]
            assert len(history) == 1
            assert history[0]["epoch"] == 5
            assert history[0]["train_loss"] == 0.5
            assert history[0]["val_loss"] == 0.6

    def test_partial_epoch_metrics_added(self, mock_jobs_store):
        """Partial metrics (only train_loss) should still be added."""
        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            update_job_training_state(
                "test-job-123",
                current_epoch=3,
                epoch_metrics={"train_loss": 0.25}
            )

            history = mock_jobs_store["test-job-123"]["epochHistory"]
            assert len(history) == 1
            assert history[0]["epoch"] == 3
            assert history[0]["train_loss"] == 0.25

    def test_reset_epoch_history_clears_list(self, mock_jobs_store):
        """reset_epoch_history=True should clear the history."""
        # Pre-populate with some history
        mock_jobs_store["test-job-123"]["epochHistory"] = [
            {"epoch": 1, "train_loss": 0.9},
            {"epoch": 2, "train_loss": 0.8}
        ]

        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            update_job_training_state(
                "test-job-123",
                reset_epoch_history=True
            )

            assert mock_jobs_store["test-job-123"]["epochHistory"] == []

    def test_epoch_history_limit(self, mock_jobs_store):
        """Epoch history should be limited to 100 entries."""
        with patch.dict('app.api.jobs.jobs_store', mock_jobs_store, clear=True):
            from app.services.job_handler import update_job_training_state

            # Add 105 entries
            for i in range(105):
                update_job_training_state(
                    "test-job-123",
                    current_epoch=i + 1,
                    epoch_metrics={"train_loss": 0.5}
                )

            history = mock_jobs_store["test-job-123"]["epochHistory"]
            assert len(history) == 100
            # Should keep the last 100 (epochs 6-105)
            assert history[0]["epoch"] == 6
            assert history[-1]["epoch"] == 105


class TestTrainingHistorySchema:
    """Tests for TrainingHistory Pydantic model flexibility."""

    def test_old_format_accepted(self):
        """Old format with loss/accuracy/valLoss/valAccuracy should work."""
        from app.api.models import TrainingHistory

        entry = TrainingHistory(
            epoch=1,
            loss=0.5,
            accuracy=0.8,
            valLoss=0.6,
            valAccuracy=0.75
        )

        assert entry.epoch == 1
        assert entry.loss == 0.5

    def test_tsai_format_accepted(self):
        """TSAI format with train_loss/val_loss should work."""
        from app.api.models import TrainingHistory

        entry = TrainingHistory(
            epoch=1,
            train_loss=0.5,
            val_loss=0.6
        )

        assert entry.epoch == 1
        assert entry.train_loss == 0.5
        assert entry.val_loss == 0.6

    def test_minimal_format_accepted(self):
        """Just epoch should be valid (though not useful)."""
        from app.api.models import TrainingHistory

        entry = TrainingHistory(epoch=1)
        assert entry.epoch == 1

    def test_extra_fields_allowed(self):
        """Extra fields should be allowed for future compatibility."""
        from app.api.models import TrainingHistory

        entry = TrainingHistory(
            epoch=1,
            train_loss=0.5,
            custom_metric=0.9  # Extra field
        )

        assert entry.epoch == 1
        assert entry.train_loss == 0.5
