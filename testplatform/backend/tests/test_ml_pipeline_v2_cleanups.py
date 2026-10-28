"""ML exploration v2, phase 6: pipeline cleanups found by the inventory.

6.1 the tsai classification path handles 3-class targets; 6.2 no silent-zero targets;
6.3 no dead options (balanced_accuracy / mse / r2 are computed); 6.4 the wizard macro reads
first-release FRED values. Tiny synthetic frames, no network.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import tsai_training  # noqa: E402



@pytest.fixture
def tsai_stack(monkeypatch):
    """The names tsai_training needs from tsai/fastai/torch.

    ``tsai.all`` pulls in optional extras (pyts, ...) that a lean ML env may lack, which turns the
    whole module off; the pieces the training service uses import fine on their own, so bind those.
    """
    pytest.importorskip('torch')
    pytest.importorskip('tsai')
    if tsai_training.TSAI_AVAILABLE:
        return
    import torch
    import torch.nn as nn
    from fastai.learner import Learner
    from fastai.metrics import accuracy, F1Score, Precision, Recall
    from fastai.losses import CrossEntropyLossFlat, FocalLossFlat
    from tsai.data.core import get_ts_dls
    for name, obj in dict(torch=torch, nn=nn, Learner=Learner, accuracy=accuracy, F1Score=F1Score,
                          Precision=Precision, Recall=Recall, CrossEntropyLossFlat=CrossEntropyLossFlat,
                          FocalLossFlat=FocalLossFlat, get_ts_dls=get_ts_dls, TSAI_AVAILABLE=True).items():
        monkeypatch.setattr(tsai_training, name, obj, raising=False)
    # the module bound a plain `object` base for the callback when its import failed; the progress
    # callback is not under test, so a do-nothing fastai callback stands in
    from fastai.callback.core import Callback

    class NoopProgress(Callback):
        def __init__(self, on_epoch_end):
            self.on_epoch_end = on_epoch_end

    monkeypatch.setattr(tsai_training, 'EpochProgressCallback', NoopProgress)


def _frame(n, labels, start='2024-01-01'):
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(size=n))
    return pd.DataFrame({
        'Date': pd.date_range(start, periods=n, freq='D'),
        'Close': close,
        'f1': rng.normal(size=n),
        'f2': rng.normal(size=n),
        'y': labels,
    })


# ---------------------------------------------------------------------------------------
# 6.1 3-class tsai targets
# ---------------------------------------------------------------------------------------
class TestClassCount:
    def test_binary_labels_are_two_classes(self):
        assert tsai_training.class_count(np.array([0, 1, 1, 0, np.nan])) == 2

    def test_three_class_labels(self):
        assert tsai_training.class_count(pd.Series([0.0, 1.0, 2.0, 2.0, np.nan])) == 3

    def test_single_observed_class_is_still_at_least_binary(self):
        assert tsai_training.class_count(np.array([0, 0, 0])) == 2

    def test_a_class_missing_from_the_middle_still_sizes_the_head_for_the_highest_label(self):
        assert tsai_training.class_count(np.array([0, 2, 2])) == 3

    def test_non_integer_labels_are_refused(self):
        with pytest.raises(ValueError, match='integer class labels'):
            tsai_training.class_count(np.array([0.0, 0.5, 1.0]))

    def test_negative_labels_are_refused(self):
        with pytest.raises(ValueError, match='integer class labels'):
            tsai_training.class_count(np.array([-1, 0, 1]))

    def test_no_labels_is_refused(self):
        with pytest.raises(ValueError, match='no labels'):
            tsai_training.class_count(np.array([np.nan]))


class TestClassPosWeightMulticlass:
    def test_binary_weight_is_unchanged(self):
        assert tsai_training.class_pos_weight(np.array([0, 0, 0, 1]), 'shift') == pytest.approx(3.0)

    def test_k_classes_get_one_inverse_frequency_weight_each(self):
        y = np.array([0, 0, 0, 0, 1, 1, 2, 2, 2, 2, 2, 2])
        w = tsai_training.class_pos_weight(y, 'shift', n_classes=3)
        assert w == pytest.approx([12 / (3 * 4), 12 / (3 * 2), 12 / (3 * 6)])

    def test_a_class_with_no_training_examples_is_an_error(self):
        with pytest.raises(ValueError, match='every class'):
            tsai_training.class_pos_weight(np.array([0, 0, 2, 2]), 'shift', n_classes=3)


@pytest.mark.usefixtures('tsai_stack')
class TestThreeClassTsai:
    def _split(self, svc, labels, seq_len=4):
        df = _frame(len(labels), labels)
        return svc.prepare_split_three_way(
            [df], train_ratio=0.6, holdout_share_of_test=0.5, target_column='y',
            feature_columns=['f1', 'f2'], seq_len=seq_len, prediction_horizon=0,
            prediction_mode='shift', label_horizon=1)

    def _net(self, c_in, seq_len, c_out):
        import torch.nn as nn
        return nn.Sequential(nn.Flatten(), nn.Linear(c_in * seq_len, c_out))

    def test_three_class_trains_assesses_and_predicts(self):
        svc = tsai_training.TSAITrainingService(normalize=False)
        labels = np.arange(150) % 3
        split = self._split(svc, labels)
        net = self._net(2, 4, 3)
        res = svc.train_model(net, (split['X_train'], split['y_train']),
                              val_data=(split['X_val'], split['y_val']), epochs=1, batch_size=16,
                              loss_fn=svc.get_loss_function('ce'))
        assert res['status'] == 'success', res
        assert all(np.isfinite(v) for v in res['metrics'].values())

        m = svc.assess_model(res['model'], (split['X_val'], split['y_val']), learner=res['learner'])
        assert 'error' not in m, m
        for key in ('f1_score', 'accuracy', 'precision', 'recall', 'mcc', 'balanced_accuracy'):
            assert np.isfinite(m[key]), key
        cm = np.array(m['confusion_matrix'])
        assert cm.shape == (3, 3) and cm.sum() == len(split['y_val'])

        probs = svc.predict(res['model'], split['X_val'], learner=res['learner'])
        assert probs.shape == (len(split['X_val']), 3)
        assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-4)

    def test_assessment_predicts_the_softmax_argmax_not_a_threshold(self):
        import torch
        import torch.nn as nn

        class Fixed(nn.Module):
            """Always puts the mass on class 2, whatever the threshold says."""
            def forward(self, x):
                out = torch.zeros(x.shape[0], 3)
                out[:, 2] = 5.0
                return out

        svc = tsai_training.TSAITrainingService(normalize=False)
        X = np.zeros((9, 2, 4), dtype=np.float32)
        y = np.array([2, 2, 2, 0, 0, 0, 1, 1, 1])
        m = svc.assess_model(Fixed(), (X, y), threshold=0.99)
        assert m['accuracy'] == pytest.approx(3 / 9)
        assert m['confusion_matrix'] == [[0, 0, 3], [0, 0, 3], [0, 0, 3]]
        # macro F1: only class 2 has any predictions (p=1/3, r=1) -> F1 0.5, the others 0
        assert m['f1_score'] == pytest.approx(0.5 / 3)
        assert m['balanced_accuracy'] == pytest.approx(1 / 3)

    def test_binary_assessment_is_unchanged(self):
        import torch
        import torch.nn as nn

        class Fixed(nn.Module):
            def forward(self, x):
                out = torch.zeros(x.shape[0], 2)
                out[:, 1] = 1.0
                return out

        svc = tsai_training.TSAITrainingService(normalize=False)
        X = np.zeros((4, 2, 4), dtype=np.float32)
        y = np.array([1, 1, 0, 0])
        m = svc.assess_model(Fixed(), (X, y), threshold=0.5)
        assert m['accuracy'] == pytest.approx(0.5)
        assert m['f1_score'] == pytest.approx(2 / 3)
        assert m['confusion_matrix'] == [[0, 2], [0, 2]]
        assert m['true_positives'] == 2 and m['false_positives'] == 2
        assert 'auc_roc' in m

    def test_three_class_job_sizes_the_head_from_the_labels(self, monkeypatch, tmp_path):
        import torch.nn as nn
        from types import SimpleNamespace
        from app.services import job_handler, tsai_models, task_queue, genetic

        created = []

        class Models:
            def create_model(self, model_type, params, c_in, c_out, seq_len):
                created.append(c_out)
                return nn.Sequential(nn.Flatten(), nn.Linear(c_in * seq_len, c_out))

        class Optimizer:
            def __init__(self, **kwargs):
                pass

            def optimize(self, fitness_function, callback=None, on_generation_start=None, **kwargs):
                on_generation_start(0)
                best = fitness_function({'model_type_idx': 0, 'hidden_size': 8, 'n_layers': 1,
                                         'dropout': 0.1, 'learning_rate': 0.01, 'threshold_idx': 0})
                return {'best_fitness': best, 'generations_run': 1, 'history': []}

        monkeypatch.setattr(tsai_models, 'TSAI_AVAILABLE', True)
        monkeypatch.setattr(tsai_models, 'TSAIModelService', Models)
        monkeypatch.setattr(task_queue, 'get_task_queue', lambda: SimpleNamespace(get_task_status=lambda tid: None))
        monkeypatch.setattr(genetic, 'GeneticOptimizer', Optimizer)
        monkeypatch.setattr(job_handler, 'get_job_models_dir', lambda tid: tmp_path)
        for name in ('update_job_training_state', 'update_job_progress', 'add_individual_to_job',
                     'cleanup_non_elite_models'):
            monkeypatch.setattr(job_handler, name, lambda *a, **k: None)
        monkeypatch.setattr(job_handler, 'get_epoch_history', lambda tid: [])
        monkeypatch.setattr(job_handler, 'save_elite_models', lambda **k: [])

        frames = [_frame(200, np.arange(200) % 3)]
        res = job_handler.train_classification_optimization(
            task_id='t-3c', selected_models=['inception'], frames=frames, train_ratio=0.6,
            holdout_share_of_test=0.5, seed=3, target_column='y', feature_columns=['f1', 'f2'],
            parameter_ranges={'seqLen': 5, 'layerSizeMin': 8, 'layerSizeMax': 16, 'layersMin': 1, 'layersMax': 2,
                              'dropoutMin': 0.0, 'dropoutMax': 0.2, 'learningRateMin': 0.001,
                              'learningRateMax': 0.01},
            genetic_config={'populationSize': 1, 'generations': 1, 'trainingEpochs': 1, 'crossoverProb': 0.7,
                            'mutationProb': 0.2, 'elitismPercent': 10.0, 'earlyStoppingGenerations': 1},
            metrics_config={'classificationMetric': 'f1_score', 'lossFunction': 'weighted_cross_entropy'},
            prediction_horizon=0, prediction_modes=['shift'], label_horizon=2,
            progress_base=0, progress_range=1, timeframe='daily')
        assert res['status'] == 'completed', res
        assert created == [3]
        assert np.isfinite(res['best_fitness']) and res['best_fitness'] > 0
        assert np.isfinite(res['metrics']['accuracy'])
        assert np.array(res['metrics']['confusion_matrix']).shape == (3, 3)
        assert np.isfinite(res['holdout_metrics']['f1_score'])

    def test_multistep_with_three_classes_is_refused(self, monkeypatch, tmp_path):
        from app.services import job_handler, tsai_models

        monkeypatch.setattr(tsai_models, 'TSAI_AVAILABLE', True)
        frames = [_frame(120, np.arange(120) % 3)]
        res = job_handler.train_classification_optimization(
            task_id='t-3c-ms', selected_models=['inception'], frames=frames, train_ratio=0.6,
            holdout_share_of_test=0.5, seed=3, target_column='y', feature_columns=['f1', 'f2'],
            parameter_ranges={'seqLen': 5}, genetic_config={'populationSize': 1, 'generations': 1,
                                                            'trainingEpochs': 1},
            metrics_config={'classificationMetric': 'f1_score', 'lossFunction': 'focal_loss'},
            prediction_horizon=2, prediction_modes=['multistep'], label_horizon=2,
            progress_base=0, progress_range=1, timeframe='daily')
        assert res['status'] == 'failed'
        assert 'multistep' in res['error'] and '3 classes' in res['error']


# ---------------------------------------------------------------------------------------
# 6.2 no silent-zero targets
# ---------------------------------------------------------------------------------------
class TestUnsupportedTrendReversalIndicators:
    @pytest.mark.parametrize('indicator', ['donchian', 'adx', 'stochastic'])
    def test_indicator_without_a_reversal_rule_is_refused_not_all_zeros(self, indicator):
        from app.services.darts_models import PredictionTargetService
        df = _frame(60, np.zeros(60))
        df['Open'] = df['High'] = df['Low'] = df['Close']
        with pytest.raises(ValueError, match=indicator):
            PredictionTargetService().calculate_trend_reversal(df, indicator, {'period': 14}, 30, 'bullish')

    def test_supported_indicator_still_builds(self):
        from app.services.darts_models import PredictionTargetService
        n = 120
        df = _frame(n, np.zeros(n))
        close = 100 + 10 * np.sin(np.arange(n) / 4.0)
        df['Open'] = close
        df['High'] = close + 1
        df['Low'] = close - 1
        df['Close'] = close
        out = PredictionTargetService().calculate_trend_reversal(df, 'rsi', {'period': 5}, 30, 'bullish')
        assert len(out) == n and out.sum() > 0
