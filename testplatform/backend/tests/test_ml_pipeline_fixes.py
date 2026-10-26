"""Regressions for the 2026-10-26 ML dataset / training pipeline review.

One class per finding. Every test builds a tiny synthetic frame or a stub model: no
network, no real training. They run in an environment without tsai (the tsai data
preparation is exercised through the same ``TSAI_AVAILABLE`` monkeypatch as
``test_ml_leakage_regressions``).
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------------------
# #9  undefined `train_series` swallowed -> every non-MSE loss silently trained as MSE
# ---------------------------------------------------------------------------------------
class TestTrainingLoss:
    def test_focal_loss_is_built_not_silently_mse(self):
        from app.services.job_handler import create_training_loss
        from app.services.losses import FocalLoss

        labels = pd.Series([0.0, 1.0, 0.0, 0.0, 1.0, 0.0])
        loss = create_training_loss('focal_loss', labels, 'direction_up_1bar')
        assert isinstance(loss, FocalLoss)

    def test_mse_means_model_default(self):
        from app.services.job_handler import create_training_loss

        assert create_training_loss('mse', pd.Series([0.1, 0.2]), 'volatility_std_5b') is None

    def test_non_binary_target_with_logit_loss_fails_loudly(self):
        from app.services.job_handler import create_training_loss

        with pytest.raises(ValueError, match="binary 0/1"):
            create_training_loss('focal_loss', pd.Series([0.01, 0.02, 0.03]), 'volatility_std_5b')

    def test_unknown_loss_is_not_swallowed(self):
        from app.services.job_handler import create_training_loss

        with pytest.raises(ValueError):
            create_training_loss('nonsense_loss', pd.Series([0.0, 1.0]), 'direction_up_1bar')


# ---------------------------------------------------------------------------------------
# #10 sigmoid applied to MSE outputs -> "everything positive" at threshold 0.5
# ---------------------------------------------------------------------------------------
darts = pytest.importorskip("darts")


class _StubForecaster:
    """Stands in for a Darts model: returns pre-baked 'historical forecasts'."""
    input_chunk_length = 2
    output_chunk_length = 1
    past_covariate_series = None

    def __init__(self, outputs):
        self._outputs = outputs

    def historical_forecasts(self, series, **kwargs):
        from darts import TimeSeries
        start = kwargs['start']
        idx = series.time_index[start:start + len(self._outputs)]
        return TimeSeries.from_times_and_values(idx, np.asarray(self._outputs, dtype='float32'))


def _binary_series(n=12):
    from darts import TimeSeries
    labels = np.array([0, 1] * (n // 2), dtype='float32')
    return TimeSeries.from_times_and_values(pd.date_range('2024-01-01', periods=n, freq='D'), labels)


class TestDartsOutputActivation:
    def _eval(self, outputs, loss_function):
        from app.services.darts_training import DartsTrainingService
        svc = DartsTrainingService()
        series = _binary_series()
        return svc.evaluate_model(
            _StubForecaster(outputs), series, optimize_metric='f1_score',
            threshold=0.5, loss_function=loss_function
        )

    def test_mse_scores_below_threshold_are_negative(self):
        # 10 forecasts, all MSE scores of 0.2..0.4 (< 0.5): a correct reading predicts NO positives.
        # With a sigmoid on top every one of them became >= 0.55 and was called positive.
        res = self._eval([0.2, 0.3, 0.4, 0.3, 0.2, 0.4, 0.3, 0.2, 0.4, 0.3], 'mse')
        assert res['true_positives'] + res['false_positives'] == 0

    def test_logit_loss_still_gets_a_sigmoid(self):
        # logits of -3 (p=0.05) must be negative and +3 (p=0.95) positive
        res = self._eval([-3, 3, -3, 3, -3, 3, -3, 3, -3, 3], 'focal_loss')
        assert res['true_positives'] + res['false_positives'] == 5

    def test_classification_without_declared_loss_is_refused(self):
        res = self._eval([0.2] * 10, None)
        assert 'error' in res
