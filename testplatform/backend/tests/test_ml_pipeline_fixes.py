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


# ---------------------------------------------------------------------------------------
# helpers shared by the multi-ticker / split tests
# ---------------------------------------------------------------------------------------
def _ohlcv(close, start='2024-01-01', freq='D', ticker=None):
    close = np.asarray(close, dtype=float)
    df = pd.DataFrame({
        'Date': pd.date_range(start, periods=len(close), freq=freq),
        'Open': close, 'High': close + 0.5, 'Low': close - 0.5, 'Close': close,
        'Volume': np.full(len(close), 1000.0),
        'SMA_5': pd.Series(close).rolling(3, min_periods=1).mean().values,
    })
    if ticker:
        df['ticker'] = ticker
    return df


# ---------------------------------------------------------------------------------------
# #1  targets were built on the date-sorted concatenation of all tickers
# ---------------------------------------------------------------------------------------
class TestPerTickerTargets:
    def _run_dry(self, monkeypatch, frames_by_id):
        from app.services import job_handler
        captured = {}
        monkeypatch.setattr(job_handler, 'ML_AVAILABLE', True)
        monkeypatch.setattr(
            job_handler, 'load_dataset',
            lambda ds_id, start_date=None, end_date=None: frames_by_id[ds_id][0].copy()
        )
        monkeypatch.setattr(
            job_handler, 'get_dataset_info',
            lambda ds_id: {'ticker': frames_by_id[ds_id][1], 'timeframe': 'daily'}
        )
        monkeypatch.setattr(job_handler, 'update_job_progress', lambda *a, **k: None)
        monkeypatch.setattr(
            job_handler, 'save_training_datasets',
            lambda **kw: captured.update(combined=kw['combined_df'], train=kw['train_df'], test=kw['test_df'])
        )
        payload = {
            'dataset_ids': list(frames_by_id), 'job_type': 'classification',
            'selected_models': ['lstm'], 'train_test_split': 70, 'prediction_horizon': 1,
            'prediction_modes': ['shift'], 'genetic_config': {'populationSize': 2},
            'metrics_config': {'classificationMetric': 'f1_score'},
            'prediction_targets': [{'type': 'directional', 'direction': 'up', 'horizon': 1}],
        }
        result = job_handler.handle_training_job('t-per-ticker', payload, dry_run=True)
        return result, captured

    def test_labels_compare_a_ticker_with_itself(self, monkeypatch):
        n = 120
        up = _ohlcv(5000 + np.arange(n))                 # first ticker: rises every bar, far above the second
        down = _ohlcv(300 - np.arange(n))                # second ticker: falls every bar
        result, cap = self._run_dry(monkeypatch, {1: (up, 'AAPL'), 2: (down, 'MSFT')})
        assert result['status'] == 'dry_run_success', result
        combined = cap['combined']
        label = [c for c in combined.columns if c.startswith('direction_up')][0]
        assert (combined.loc[combined['ticker'] == 'AAPL', label] == 1).all()
        assert (combined.loc[combined['ticker'] == 'MSFT', label] == 0).all()

    def test_each_ticker_loses_only_its_own_unobservable_tail(self, monkeypatch):
        n = 120
        result, cap = self._run_dry(
            monkeypatch, {1: (_ohlcv(100 + np.arange(n)), 'A'), 2: (_ohlcv(300 + np.arange(n)), 'B')}
        )
        counts = cap['combined']['ticker'].value_counts()
        assert counts['A'] == n - 1 and counts['B'] == n - 1   # horizon 1 -> 1 NaN tail row EACH

    def test_split_is_per_ticker_and_chronological(self, monkeypatch):
        n = 120
        # B starts months after A: a positional split of the date-sorted concatenation would put
        # all of A in train and none of it in test
        result, cap = self._run_dry(
            monkeypatch,
            {1: (_ohlcv(100 + np.arange(n)), 'A'), 2: (_ohlcv(300 + np.arange(n), start='2024-09-01'), 'B')}
        )
        for tk in ('A', 'B'):
            last_train = cap['train'].loc[cap['train']['ticker'] == tk, 'Date'].max()
            first_test = cap['test'].loc[cap['test']['ticker'] == tk, 'Date'].min()
            assert last_train < first_test
            assert (cap['train']['ticker'] == tk).sum() > 0 and (cap['test']['ticker'] == tk).sum() > 0


# ---------------------------------------------------------------------------------------
# #8  the test block was GA fitness set, threshold source and reported metric
# ---------------------------------------------------------------------------------------
class TestThreeWaySplit:
    def test_bounds_are_ordered_purged_and_disjoint(self):
        from app.services.data_preparation import three_way_bounds
        n, h = 200, 5
        b = three_way_bounds(n, 0.6, 0.5, h)
        assert b['train_end'] + h == b['val_start']            # last h train rows purged
        assert b['val_end'] + h == b['holdout_start']          # last h validation rows purged
        assert b['train_end'] <= b['val_start'] < b['val_end'] <= b['holdout_start'] < n
        assert n - b['holdout_start'] == int((n - b['val_start']) * 0.5)

    def test_too_few_rows_for_three_blocks_is_refused(self):
        from app.services.data_preparation import three_way_bounds
        with pytest.raises(ValueError, match="too few"):
            three_way_bounds(12, 0.8, 0.5, 3)

    def test_split_frames_three_way_cuts_every_ticker(self):
        from app.services.job_handler import split_frames_three_way
        frames = [_ohlcv(np.arange(100.0), ticker='A'), _ohlcv(np.arange(150.0), ticker='B')]
        out = split_frames_three_way(frames, 0.6, 0.5, 4)
        for i in range(2):
            tr, va, ho = out['train'][i], out['val'][i], out['holdout'][i]
            assert tr['Date'].max() < va['Date'].min() <= va['Date'].max() < ho['Date'].min()
            # the label of a validation row looks 4 bars ahead and must not reach the holdout
            assert va['Date'].max() + pd.Timedelta(days=4) < ho['Date'].min()

    def test_tsai_windows_never_cross_blocks_or_tickers(self, monkeypatch):
        from app.services import tsai_training
        from app.services.tsai_training import TSAITrainingService
        from app.services.data_preparation import three_way_bounds
        monkeypatch.setattr(tsai_training, 'TSAI_AVAILABLE', True)
        svc = TSAITrainingService(normalize=False)
        frames = []
        for offset in (0, 1000):                       # feature value encodes (ticker, row)
            f = _ohlcv(np.arange(100.0) + 50)
            f['ROW'] = offset + np.arange(100)
            f['direction'] = 1.0
            frames.append(f)
        out = svc.prepare_split_three_way(
            frames, train_ratio=0.6, holdout_share_of_test=0.5, target_column='direction',
            feature_columns=['ROW'], seq_len=5, prediction_horizon=0, label_horizon=3
        )
        b = three_way_bounds(100, 0.6, 0.5, 3)
        for name, lo, hi in (('train', 0, b['train_end']), ('val', b['val_start'], b['val_end']),
                             ('holdout', b['holdout_start'], 100)):
            rows = out[f'X_{name}'][:, 0, :]
            for ticker_offset in (0, 1000):
                own = rows[(rows >= ticker_offset) & (rows < ticker_offset + 1000)]
                assert own.min() >= ticker_offset + lo and own.max() < ticker_offset + hi
            # a window holds ONE ticker only
            assert ((rows >= 1000).all(axis=1) | (rows < 1000).all(axis=1)).all()

    def test_darts_three_way_series_are_per_ticker_with_own_scalers(self):
        from app.services.darts_training import DartsTrainingService
        svc = DartsTrainingService()
        frames = []
        for base in (10.0, 5000.0):
            f = _ohlcv(base + np.arange(150.0))
            f['y'] = (np.arange(150) % 2).astype(float)
            frames.append(f)
        out = svc.prepare_split_three_way(
            frames, train_ratio=0.6, holdout_share_of_test=0.5, target_column='y',
            feature_columns=['SMA_5'], timeframe='daily', label_horizon=3
        )
        assert len(out['train_series']) == 2 and len(svc.scalers) == 2
        for tr, va, ho in zip(out['train_series'], out['val_series'], out['holdout_series']):
            assert tr.end_time() < va.start_time() and va.end_time() < ho.start_time()
        assert svc.scalers[0] is not svc.scalers[1]

    def test_darts_evaluates_a_list_of_series_with_the_given_scalers(self):
        from app.services.darts_training import DartsTrainingService
        svc = DartsTrainingService()
        s1, s2 = _binary_series(12), _binary_series(12)
        res = svc.evaluate_model(
            _StubForecaster([0.9, 0.1] * 5), [s1, s2], optimize_metric='f1_score', loss_function='mse',
            scalers=[None, None]
        )
        assert res['predictions_made'] == 20          # 10 from each series, pooled


# ---------------------------------------------------------------------------------------
# #11 Darts forecast the label series from its own labels whose outcome was not yet known
# ---------------------------------------------------------------------------------------
class TestLabelHistoryLag:
    NBEATS = {'input_chunk_length': 8, 'output_chunk_length': 1, 'num_stacks': 2, 'num_blocks': 1,
              'num_layers': 2, 'layer_widths': 16, 'batch_size': 8, 'n_epochs': 1}

    def test_model_input_window_ends_label_horizon_before_the_forecast(self):
        from app.services.darts_models import DartsModelService
        model = DartsModelService().create_model('nbeats', {**self.NBEATS, 'output_chunk_shift': 3})
        assert model.output_chunk_shift == 3

    def test_no_shift_for_causal_labels(self):
        from app.services.darts_models import DartsModelService
        model = DartsModelService().create_model('nbeats', dict(self.NBEATS))
        assert model.output_chunk_shift == 0

    def test_rnn_models_are_refused_for_a_lookahead_label(self):
        from app.services.darts_models import DartsModelService
        with pytest.raises(ValueError, match="cannot shift its output"):
            DartsModelService().create_model('lstm', {'output_chunk_shift': 2})

    def test_get_model_params_carries_the_shift(self):
        from app.services.job_handler import get_model_params
        assert get_model_params('nbeats', {}, 5, 1, output_chunk_shift=4)['output_chunk_shift'] == 4
        assert 'output_chunk_shift' not in get_model_params('nbeats', {}, 5, 1)

    def test_evaluation_starts_after_the_shifted_input_window(self):
        from app.services.darts_training import DartsTrainingService

        class Shifted(_StubForecaster):
            output_chunk_shift = 3
            seen = {}

            def historical_forecasts(self, series, **kwargs):
                Shifted.seen = kwargs
                return super().historical_forecasts(series, **kwargs)

        DartsTrainingService().evaluate_model(
            Shifted([0.5] * 6), _binary_series(14), optimize_metric='f1_score', loss_function='mse',
            scalers=[None]
        )
        assert Shifted.seen['start'] == 2 + 3        # input_chunk + output_chunk_shift

    def test_unified_job_refuses_rnn_on_a_lookahead_label(self):
        from app.services.job_handler import train_unified_optimization
        frames = []
        for base in (10.0, 20.0):
            f = _ohlcv(base + np.arange(160.0))
            f['y'] = (np.arange(160) % 2).astype(float)
            frames.append(f)
        res = train_unified_optimization(
            task_id='t-rnn', selected_models=['lstm'], frames=frames, train_ratio=0.6,
            holdout_share_of_test=0.5, target_column='y', feature_columns=['SMA_5'],
            parameter_ranges={}, genetic_config={}, metrics_config={}, prediction_horizon=0,
            label_horizon=3, progress_base=0, progress_range=1, timeframe='daily'
        )
        assert res['status'] == 'failed' and 'cannot be trained on this target' in res['error']


# ---------------------------------------------------------------------------------------
# #2  higher-timeframe features were joined on the higher bar START (leaks the rest of the bar)
# ---------------------------------------------------------------------------------------
class TestMultiTimeframeNoLookahead:
    def _frames(self):
        lower = pd.DataFrame({'Date': pd.date_range('2024-01-02 09:00', periods=12, freq='15min'),
                              'Close': np.arange(12.0)})
        # hourly bars 09:00 and 10:00; their indicator value is 100 and 200
        higher = pd.DataFrame({'Date': pd.to_datetime(['2024-01-02 09:00', '2024-01-02 10:00']),
                               'IND': [100.0, 200.0]})
        return lower, higher

    def test_higher_bar_is_visible_only_after_it_ended(self):
        from app.services.indicators import align_higher_timeframe_features_to_lower
        lower, higher = self._frames()
        out = align_higher_timeframe_features_to_lower(lower, higher, ['IND'], '1h', '15m')
        by_time = dict(zip(out['Date'].dt.strftime('%H:%M'), out['IND']))
        # the 09:00 hourly bar ends 10:00 -> first visible on the 15m row 09:45 (which ends 10:00)
        assert all(np.isnan(by_time[t]) for t in ('09:00', '09:15', '09:30'))
        assert by_time['09:45'] == 100.0 and by_time['10:00'] == 100.0 and by_time['10:30'] == 100.0
        # the 10:00 hourly bar ends 11:00 -> first visible on the row 10:45
        assert by_time['10:45'] == 200.0 and by_time['11:30'] == 200.0

    def test_old_start_keyed_alignment_leaked(self):
        # documents the defect the new function fixes: keyed on the bar START the 09:00 hourly value
        # (known at 10:00) is already on the 09:00 row
        from app.services.indicators import align_higher_timeframe_to_lower
        lower, higher = self._frames()
        out = align_higher_timeframe_to_lower(lower, higher, ['IND'])
        assert out.loc[out['Date'] == '2024-01-02 09:00', 'IND'].iloc[0] == 100.0

    def test_indicator_service_multi_timeframe_does_not_use_the_unfinished_bar(self):
        from app.services.indicators import IndicatorService
        n = 120
        dates = pd.date_range('2024-01-02 09:00', periods=n, freq='15min')
        close = 100 + np.cumsum(np.random.RandomState(3).randn(n))
        df = pd.DataFrame({'Date': dates, 'Open': close, 'High': close + 1, 'Low': close - 1,
                           'Close': close, 'Volume': np.full(n, 1000.0)})
        res = IndicatorService().calculate_indicators_multi_timeframe(
            df, [{'type': 'rsi', 'period': 5}], '1h', source_timeframe='15m'
        )
        series = res['rsi_5_1h']
        # changing the LAST 15m bar of an hour must not change that hour's value on any earlier row
        df2 = df.copy()
        df2.loc[df2['Date'] == '2024-01-02 10:45', 'Close'] += 50
        res2 = IndicatorService().calculate_indicators_multi_timeframe(
            df2, [{'type': 'rsi', 'period': 5}], '1h', source_timeframe='15m'
        )['rsi_5_1h']
        before = df['Date'] < '2024-01-02 10:45'
        pd.testing.assert_series_equal(series[before], res2[before])

    def test_unknown_timeframe_is_refused(self):
        from app.services.indicators import timeframe_delta
        with pytest.raises(ValueError):
            timeframe_delta('3h')


# ---------------------------------------------------------------------------------------
# #3 / #4  fundamentals: back-filled from the future, available at period end + 45 days
# ---------------------------------------------------------------------------------------
class TestFundamentalsPointInTime:
    PERIODS = [   # oldest first; total_assets grows 100, 110, 121, 133.1, 146.41, 161.05
        {'fiscal_date': f'{y}-{m}', 'total_assets': 100.0 * 1.1 ** i}
        for i, (y, m) in enumerate(
            [('2022', '12-31'), ('2023', '03-31'), ('2023', '06-30'), ('2023', '09-30'), ('2023', '12-31'), ('2024', '03-31')]
        )
    ]

    def _run(self, monkeypatch, dates, periods=None):
        from types import SimpleNamespace
        from app.services.fundamentals import FundamentalsService

        class _Provider:
            def get_balance_sheet(self_inner, **kw):
                return SimpleNamespace(periods=list(periods if periods is not None else self.PERIODS))

        monkeypatch.setattr(FundamentalsService, '_build_provider_service', staticmethod(lambda providers: _Provider()))
        df = pd.DataFrame({'Date': pd.to_datetime(dates), 'Close': 1.0})
        return FundamentalsService.create_statement_features_v2(df, 'XYZ', ['balance_sheet'])

    def test_rows_before_the_first_publication_are_nan_not_back_filled(self, monkeypatch):
        out = self._run(monkeypatch, ['2022-06-01', '2023-02-01', '2023-03-15'])
        for col in ('bs_total_assets', 'bs_total_assets_days_old',
                    'bs_total_assets_qoq_change', 'bs_total_assets_yoy_change'):
            assert out[col].isna().all(), col

    def test_change_columns_are_not_borrowed_from_a_later_period(self, monkeypatch):
        # first period published 2022-12-31 + 75d = 2023-03-16: it has no previous quarter, so
        # QoQ/YoY are unknown there -- they used to be filled with the FIRST AVAILABLE (later) value
        out = self._run(monkeypatch, ['2023-04-01'])
        assert out['bs_total_assets'].iloc[0] == 100.0
        assert np.isnan(out['bs_total_assets_qoq_change'].iloc[0])
        assert np.isnan(out['bs_total_assets_yoy_change'].iloc[0])

    def test_statement_is_not_visible_45_days_after_period_end(self, monkeypatch):
        # period end 2023-03-31: 45 days later is 2023-05-15, filings are due later than that
        out = self._run(monkeypatch, ['2023-05-16', '2023-06-13', '2023-06-14'])
        assert list(out['bs_total_assets']) == pytest.approx([100.0, 100.0, 110.0])   # 2023-03-31 + 75d = 2023-06-14

    def test_real_filing_date_beats_the_lag_estimate(self):
        from app.services.fundamentals import FundamentalsService
        p = {'fiscal_date': '2023-03-31', 'fillingDate': '2023-05-04'}
        assert FundamentalsService.statement_available_date(p) == pd.Timestamp('2023-05-04')
        assert FundamentalsService.statement_available_date({'fiscal_date': '2023-03-31'}) == pd.Timestamp('2023-06-14')

    def test_changes_appear_once_enough_history_is_published(self, monkeypatch):
        out = self._run(monkeypatch, ['2024-03-01'])   # 2023-12-31 period (+75d = 2024-03-15) not yet public
        assert out['bs_total_assets'].iloc[0] == pytest.approx(133.1)
        assert out['bs_total_assets_qoq_change'].iloc[0] == pytest.approx(0.1)
        assert out['bs_total_assets_yoy_change'].iloc[0] != out['bs_total_assets_yoy_change'].iloc[0]   # NaN: <4 prior quarters


# ---------------------------------------------------------------------------------------
# #5  today's yf.Ticker.info snapshot was broadcast to every historical row
# ---------------------------------------------------------------------------------------
class TestNoSnapshotBroadcast:
    def test_snapshot_is_off_by_default(self, monkeypatch):
        from app.services.fundamentals import FundamentalsService
        monkeypatch.setattr(FundamentalsService, 'get_fundamental_data',
                            staticmethod(lambda t: {'current': {'trailing_pe': 31.0}}))
        df = pd.DataFrame({'Date': pd.date_range('2019-01-01', periods=3), 'Close': 1.0})
        out = FundamentalsService.add_live_snapshot_columns(df, 'AAPL', {'enabled': True})
        assert list(out.columns) == ['Date', 'Close']

    def test_snapshot_needs_an_explicit_live_only_opt_in(self, monkeypatch):
        from app.services.fundamentals import FundamentalsService
        monkeypatch.setattr(FundamentalsService, 'get_fundamental_data',
                            staticmethod(lambda t: {'current': {'trailing_pe': 31.0}}))
        df = pd.DataFrame({'Date': pd.date_range('2019-01-01', periods=3), 'Close': 1.0})
        out = FundamentalsService.add_live_snapshot_columns(df, 'AAPL', {'live_only_snapshot': True})
        assert 'live_snapshot_trailing_pe' in out.columns and not any(c.startswith('fundamental_') for c in out.columns)

    def test_generation_paths_no_longer_write_fundamental_columns(self):
        import inspect
        from app.api import datasets as datasets_api
        from app.services import dataset_handler
        for module in (datasets_api, dataset_handler):
            src = inspect.getsource(module)
            assert "df[f'fundamental_{key}']" not in src, module.__name__
