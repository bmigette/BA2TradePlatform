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


# ---------------------------------------------------------------------------------------
# 6.3 metrics the UI offers are computed
# ---------------------------------------------------------------------------------------
class _Vals:
    """Stands in for a darts TimeSeries: only ``.values()`` is read."""
    def __init__(self, v):
        self._v = np.asarray(v, dtype=float).reshape(-1, 1)

    def values(self):
        return self._v


class TestRegressionMetricsOffered:
    def _metrics(self, pred, actual):
        from app.services.darts_training import DartsTrainingService
        return DartsTrainingService()._regression_metrics([_Vals(pred)], [_Vals(actual)], len(actual))

    def test_mse_and_r2_are_computed(self):
        m = self._metrics([2.0, 4.0, 6.0, 9.0], [1.0, 4.0, 7.0, 8.0])
        assert m['mse'] == pytest.approx((1 + 0 + 1 + 1) / 4)
        assert m['rmse'] == pytest.approx(m['mse'] ** 0.5)
        ss_res = 3.0
        ss_tot = ((np.array([1.0, 4.0, 7.0, 8.0]) - 5.0) ** 2).sum()
        assert m['r2'] == pytest.approx(1 - ss_res / ss_tot)

    def test_perfect_forecast_has_r2_one(self):
        assert self._metrics([1.0, 2.0, 5.0], [1.0, 2.0, 5.0])['r2'] == pytest.approx(1.0)

    def test_constant_actuals_have_no_r2(self):
        assert self._metrics([1.0, 2.0, 3.0], [2.0, 2.0, 2.0])['r2'] is None


class TestRegressionFitness:
    def test_lower_is_better_metrics_become_a_decreasing_fitness(self):
        from app.services.job_handler import metric_fitness
        for name in ('mae', 'rmse', 'mse'):
            assert metric_fitness(name, {name: 0.5}) > metric_fitness(name, {name: 2.0}) > 0

    def test_r2_is_higher_is_better_and_stays_positive_when_the_model_is_worse_than_the_mean(self):
        from app.services.job_handler import metric_fitness
        good, mean_model, bad = (metric_fitness('r2', {'r2': r}) for r in (0.9, 0.0, -3.0))
        assert good > mean_model > bad > 0

    def test_r2_without_a_value_is_the_worst_fitness(self):
        from app.services.job_handler import metric_fitness
        assert metric_fitness('r2', {'r2': None}) == 0.0

    def test_classification_metrics_pass_through(self):
        from app.services.job_handler import metric_fitness
        assert metric_fitness('f1_score', {'f1_score': 0.7}) == 0.7
        assert metric_fitness('balanced_accuracy', {}) == 0.0


@pytest.mark.usefixtures('tsai_stack')
class TestBalancedAccuracy:
    def _net(self, probs):
        import torch
        import torch.nn as nn
        logits = torch.log(torch.tensor(probs, dtype=torch.float32))

        class Fixed(nn.Module):
            def forward(self, x):
                return logits[:x.shape[0]]
        return Fixed()

    def test_binary_balanced_accuracy_is_the_mean_recall(self):
        svc = tsai_training.TSAITrainingService(normalize=False)
        # 3 negatives (2 right), 1 positive (right) -> recalls 2/3 and 1 -> 5/6; plain accuracy is 3/4
        net = self._net([[0.9, 0.1], [0.8, 0.2], [0.2, 0.8], [0.1, 0.9]])
        m = svc.assess_model(net, (np.zeros((4, 1, 3), dtype=np.float32), np.array([0, 0, 0, 1])), threshold=0.5)
        assert m['accuracy'] == pytest.approx(0.75)
        assert m['balanced_accuracy'] == pytest.approx(5 / 6)

    def test_multistep_balanced_accuracy_averages_the_horizons(self):
        import torch
        import torch.nn as nn

        class Fixed(nn.Module):
            def forward(self, x):
                return torch.tensor([[5.0, -5.0], [5.0, -5.0], [5.0, -5.0], [-5.0, 5.0]])

        svc = tsai_training.TSAITrainingService(normalize=False)
        y = np.array([[1, 0], [0, 0], [0, 0], [1, 1]])
        m = svc.assess_model(Fixed(), (np.zeros((4, 1, 3), dtype=np.float32), y), prediction_mode='multistep')
        # horizon 1: predicts 1,1,1,0 vs 1,0,0,1 -> recall(1)=1/2, recall(0)=0 ; horizon 2: 0,0,0,1 vs 0,0,0,1 -> 1
        assert m['balanced_accuracy'] == pytest.approx((0.25 + 1.0) / 2)


# ---------------------------------------------------------------------------------------
# 6.4 wizard macro reads first-release FRED values
# ---------------------------------------------------------------------------------------
@pytest.fixture
def fred_cache(tmp_path, monkeypatch):
    """A temp FRED cache the first-release reader (ba2_providers) is pointed at."""
    import json
    from ba2_providers.macro import fred_series as fs

    monkeypatch.setattr(fs, 'CACHE_FOLDER', str(tmp_path))
    fs.reset_cache()

    def write(series_id, observations):
        os.makedirs(os.path.join(fs.CACHE_FOLDER, 'fred'), exist_ok=True)
        doc = {'series_id': series_id, 'fetched_at': pd.Timestamp.now(tz='UTC').isoformat(),
               'availability': fs.AVAIL_FIRST_RELEASE, 'format': fs.CACHE_FORMAT_FIRST_RELEASE,
               'first_vintage': '2000-01-03', 'observations': observations}
        with open(fs.cache_path(series_id), 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)

    yield write
    fs.reset_cache()


def _rows(dates):
    return pd.DataFrame({'Date': pd.to_datetime(dates), 'Close': 1.0})


class TestMacroFirstRelease:
    def _svc(self):
        from app.services.macro import MacroService
        return MacroService(api_key='unused')

    def test_a_later_vintage_of_an_observation_never_changes_its_value(self, fred_cache):
        fred_cache('UNRATE', [
            {'date': '2023-12-01', 'value': '3.7', 'realtime_start': '2024-01-05'},
            {'date': '2024-01-01', 'value': '3.9', 'realtime_start': '2024-02-02'},   # first release
            {'date': '2024-01-01', 'value': '4.0', 'realtime_start': '2024-03-08'},   # a revision
        ])
        out = self._svc().integrate_macro_with_ohlc(
            _rows(['2024-02-01', '2024-02-02', '2024-03-07', '2024-03-08']), ['unemployment'])
        # 02-01 decides before January's release (3.7 still in force); from 02-02 the first release
        # (3.9) is the value, also after the revision's vintage (03-08): revisions are never applied
        assert out['unemployment'].tolist() == [3.7, 3.9, 3.9, 3.9]

    def test_value_is_not_visible_on_its_observation_date(self, fred_cache):
        fred_cache('CPIAUCSL', [
            {'date': '2024-02-01', 'value': '310.0', 'realtime_start': '2024-03-12'},
            {'date': '2024-03-01', 'value': '312.0', 'realtime_start': '2024-04-10'},
        ])
        out = self._svc().integrate_macro_with_ohlc(
            _rows(['2024-03-05', '2024-03-20', '2024-04-09', '2024-04-10', '2024-04-20']), ['inflation'])
        # March's 312 was published 04-10: bar 04-09 decides on 04-10 (not yet visible), bar 04-10 on 04-11
        assert np.isnan(out['inflation'].iloc[0])
        assert out['inflation'].iloc[1:].tolist() == [310.0, 310.0, 312.0, 312.0]

    def test_column_names_yoy_and_days_since(self, fred_cache):
        obs = [{'date': f'2023-{m:02d}-01', 'value': str(100.0 + m), 'realtime_start': f'2023-{m:02d}-20'}
               for m in range(1, 13)]
        obs += [{'date': '2024-01-01', 'value': '120.0', 'realtime_start': '2024-01-20'},
                {'date': '2024-02-01', 'value': '121.0', 'realtime_start': '2024-02-20'}]
        fred_cache('CPIAUCSL', obs)
        out = self._svc().integrate_macro_with_ohlc(_rows(['2024-02-27']), ['inflation'])
        assert {'inflation', 'inflation_yoy_change', 'inflation_days_since'} <= set(out.columns)
        assert out['inflation'].iloc[0] == 121.0
        assert out['inflation_yoy_change'].iloc[0] == pytest.approx((121.0 / 102.0 - 1) * 100)
        assert out['inflation_days_since'].iloc[0] == 26      # 2024-02-01 -> 2024-02-27

    def test_intraday_bars_of_one_day_share_one_value(self, fred_cache):
        fred_cache('VIXCLS', [
            {'date': '2024-03-04', 'value': '14.0', 'realtime_start': '2024-03-05'},
            {'date': '2024-03-05', 'value': '25.0', 'realtime_start': '2024-03-06'},
        ])
        bars = ['2024-03-06 09:30', '2024-03-06 15:30', '2024-03-07 09:30']
        out = self._svc().integrate_macro_with_ohlc(_rows(bars), ['vix'])
        assert out['vix'].tolist() == [14.0, 14.0, 25.0]

    def test_a_series_without_a_cache_file_is_refused_with_the_expected_file(self, fred_cache):
        from ba2_providers.macro import fred_series as fs
        with pytest.raises(fs.MacroAvailabilityUnknown, match=r'CPIAUCSL\.json'):
            self._svc().integrate_macro_with_ohlc(_rows(['2024-03-05']), ['inflation'])

    @pytest.mark.parametrize('indicator,series', [('gdp', 'GDP'), ('yield_2y', 'DGS2')])
    def test_indicators_with_no_cache_series_are_refused(self, fred_cache, indicator, series):
        with pytest.raises(ValueError, match=f'{series}.*no first-release cache file'):
            self._svc().integrate_macro_with_ohlc(_rows(['2024-03-05']), [indicator])

    def test_yield_curve_needs_the_cached_2y_and_is_refused_without_it(self, fred_cache):
        fred_cache('DGS10', [{'date': '2024-03-04', 'value': '4.2', 'realtime_start': '2024-03-05'}])
        with pytest.raises(ValueError, match='DGS2'):
            self._svc().create_yield_curve_features(_rows(['2024-03-06']))

    def test_no_live_fred_fetch_and_no_api_key_for_dataset_features(self, fred_cache, monkeypatch):
        import requests
        from app.services.macro import MacroService
        fred_cache('UNRATE', [{'date': '2024-01-01', 'value': '3.9', 'realtime_start': '2024-02-02'}])
        monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('network used'))
        monkeypatch.delenv('FRED_API_KEY', raising=False)
        out = MacroService().integrate_macro_with_ohlc(_rows(['2024-02-05']), ['unemployment'])
        assert out['unemployment'].iloc[0] == 3.9

    def test_every_indicator_names_its_fred_series(self):
        from app.services.macro import MacroService
        for name, cfg in MacroService.MACRO_INDICATORS.items():
            assert cfg['series'] and cfg['yoy_periods'] >= 1, name
