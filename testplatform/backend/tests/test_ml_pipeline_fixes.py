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


# ---------------------------------------------------------------------------------------
# #6  FRED observation date was used as the release date
# ---------------------------------------------------------------------------------------
class TestMacroReleaseLag:
    def _service(self, monkeypatch, frames):
        from app.services.macro import MacroService
        svc = MacroService(api_key='test-key')
        monkeypatch.setattr(svc, 'get_macro_data', lambda indicators, start, end: {
            k: v.copy() for k, v in frames.items() if k in indicators
        })
        return svc

    @staticmethod
    def _rows(dates):
        return pd.DataFrame({'Date': pd.to_datetime(dates), 'Close': 1.0})

    def test_monthly_value_is_not_visible_on_its_observation_date(self, monkeypatch):
        # FRED stamps March CPI 2024-03-01; it is published mid-April
        cpi = pd.DataFrame({'Date': pd.to_datetime(['2024-02-01', '2024-03-01']), 'inflation': [310.0, 312.0]})
        svc = self._service(monkeypatch, {'inflation': cpi})
        out = svc.integrate_macro_with_ohlc(
            self._rows(['2024-03-05', '2024-03-20', '2024-04-10', '2024-04-20']), ['inflation']
        )
        # Feb CPI (obs 02-01) is public from 02-01 + 45d = 03-17; Mar CPI from 04-15
        assert np.isnan(out['inflation'].iloc[0])
        assert list(out['inflation'].iloc[1:]) == [310.0, 310.0, 312.0]

    def test_quarterly_gdp_waits_for_the_advance_estimate(self, monkeypatch):
        gdp = pd.DataFrame({'Date': pd.to_datetime(['2023-10-01', '2024-01-01']), 'gdp': [27000.0, 27500.0]})
        svc = self._service(monkeypatch, {'gdp': gdp})
        out = svc.integrate_macro_with_ohlc(self._rows(['2024-01-15', '2024-03-15', '2024-05-05']), ['gdp'])
        # Q1-2024 (obs 01-01) is public from 01-01 + 120d = 04-30; Q4-2023 from 2023-10-01 + 120d = 01-29
        assert np.isnan(out['gdp'].iloc[0])
        assert list(out['gdp'].iloc[1:]) == [27000.0, 27500.0]

    def test_daily_series_is_shifted_by_one_session(self, monkeypatch):
        vix = pd.DataFrame({'Date': pd.to_datetime(['2024-03-04', '2024-03-05']), 'vix': [14.0, 25.0]})
        svc = self._service(monkeypatch, {'vix': vix})
        out = svc.integrate_macro_with_ohlc(self._rows(['2024-03-04', '2024-03-05', '2024-03-06']), ['vix'])
        # the 03-05 close (25.0) cannot be on the 03-05 row; it appears on 03-06
        assert np.isnan(out['vix'].iloc[0])
        assert list(out['vix'].iloc[1:]) == [14.0, 25.0]

    def test_days_since_counts_from_the_release(self, monkeypatch):
        cpi = pd.DataFrame({'Date': pd.to_datetime(['2024-02-01']), 'inflation': [310.0]})
        svc = self._service(monkeypatch, {'inflation': cpi})
        out = svc.integrate_macro_with_ohlc(self._rows(['2024-03-27']), ['inflation'])
        assert out['inflation_days_since'].iloc[0] == 10     # released 2024-03-17

    def test_every_indicator_declares_a_release_lag(self):
        from app.services.macro import MacroService
        for name, cfg in MacroService.MACRO_INDICATORS.items():
            assert cfg['release_lag_days'] >= 1, name

    def test_yield_curve_uses_released_values(self, monkeypatch):
        y2 = pd.DataFrame({'Date': pd.to_datetime(['2024-03-04', '2024-03-05']), 'yield_2y': [4.0, 4.5]})
        y10 = pd.DataFrame({'Date': pd.to_datetime(['2024-03-04', '2024-03-05']), 'yield_10y': [4.2, 4.1]})
        svc = self._service(monkeypatch, {'yield_2y': y2, 'yield_10y': y10})
        out = svc.create_yield_curve_features(self._rows(['2024-03-05', '2024-03-06']))
        assert out['yield_2y'].tolist() == [4.0, 4.5]       # 03-05 row only knows the 03-04 close


# ---------------------------------------------------------------------------------------
# #7  zigzag: pivot placed on the pivot bar (confirmed later), ffill().bfill() in the feature path
# ---------------------------------------------------------------------------------------
class TestZigzagHindsight:
    @staticmethod
    def _swing_frame():
        # 100 -> 110 over 10 bars, then a slow slide to 98 over 30 bars (the 110 pivot is only
        # confirmed once price is 5% lower, ~12 bars after the top), then back up
        up = np.linspace(100, 110, 11)
        down = np.linspace(110, 98, 31)[1:]
        up2 = np.linspace(98, 112, 15)[1:]
        close = np.concatenate([up, down, up2])
        return pd.DataFrame({
            'Date': pd.date_range('2024-01-01', periods=len(close), freq='D'),
            'Open': close, 'High': close + 0.2, 'Low': close - 0.2, 'Close': close, 'Volume': 1000.0,
        })

    def test_zigzag_columns_are_never_model_features(self):
        from app.services.job_handler import select_feature_columns
        cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'RSI_14', 'zigzag_5.0',
                'zigzag_direction', 'ZigZag_3', 'direction_up_5bar']
        assert select_feature_columns(cols, target_columns=['direction_up_5bar']) == ['RSI_14']

    def test_sparse_fill_does_not_write_the_first_pivot_into_leading_rows(self):
        from app.services.job_handler import ffill_sparse_indicators
        df = pd.DataFrame({'Close': np.arange(8.0),
                           'zigzag_5.0': [np.nan, np.nan, np.nan, 7.0, np.nan, np.nan, 9.0, np.nan]})
        out = ffill_sparse_indicators(df)
        assert out['zigzag_5.0'].iloc[:3].isna().all()               # was back-filled with 7.0
        assert out['zigzag_5.0'].iloc[3:].tolist() == [7.0, 7.0, 7.0, 9.0, 9.0]

    def test_confirmation_lag_reflects_the_swing_length_not_one_bar(self):
        from app.services.indicators import IndicatorService
        lag = IndicatorService().zigzag_confirmation_lag(self._swing_frame(), 5.0)
        assert lag >= 10

    def test_zigzag_target_purge_horizon_is_the_confirmation_lag(self):
        from app.services.job_handler import build_prediction_targets
        from app.services.indicators import IndicatorService
        frame = self._swing_frame()
        _, col, _, horizons = build_prediction_targets(
            frame.copy(),
            [{'type': 'trend_reversal', 'indicator': 'zigzag', 'direction': 'bullish',
              'indicatorParams': {'deviation_pct': 5.0}}],
            'daily'
        )
        assert horizons[col] == IndicatorService().zigzag_confirmation_lag(frame, 5.0)
        assert horizons[col] > 1


# ---------------------------------------------------------------------------------------
# #12 ML backtest: predictions stamped at bar START, no OOS enforcement, slippage ignored
# ---------------------------------------------------------------------------------------
class TestMlBacktest:
    @staticmethod
    def _exec_1m(n=180):
        idx = pd.date_range('2024-01-02 09:30', periods=n, freq='min')
        price = 100 + np.arange(n) * 0.01
        return pd.DataFrame({'Date': idx, 'Open': price, 'High': price + 0.05, 'Low': price - 0.05,
                             'Close': price, 'Volume': 1000.0})

    @staticmethod
    def _hourly_predictions():
        starts = pd.to_datetime(['2024-01-02 09:00', '2024-01-02 10:00', '2024-01-02 11:00'])
        probs = {ts: np.array([0.1, 0.9]) for ts in starts}
        return probs, list(starts)

    def _run(self, monkeypatch, **overrides):
        from app.services import backtest_handler as bh
        seen = []
        original = bh.MLStrategy._get_prediction_for_time

        def spy(self_, current_time):
            probs, is_new = original(self_, current_time)
            seen.append((pd.Timestamp(current_time), probs is not None))
            return probs, is_new

        monkeypatch.setattr(bh.MLStrategy, '_get_prediction_for_time', spy)
        lookup, stamps = self._hourly_predictions()
        kwargs = dict(
            pred_lookup=lookup, pred_timestamps=stamps, exec_df=self._exec_1m(), strategy_params={},
            initial_capital=10000.0, position_sizing_type='percent', position_sizing_value=10.0,
            commission=0.0, slippage=0.0, buy_entry_conditions=None, sell_entry_conditions=None,
            exit_conditions=None,
        )
        kwargs.update(overrides)
        return bh, bh._run_strategy_backtest(**kwargs), seen

    def test_a_prediction_is_not_usable_before_its_bar_has_ended(self, monkeypatch):
        bh, _, seen = self._run(monkeypatch)
        usable = [t for t, ok in seen if ok]
        # the 09:00 hourly prediction covers 09:00-10:00; the first 1m bar that ENDS at 10:00 is
        # the one starting 09:59. Execution data starts at 09:30, so nothing before 09:59 is usable.
        assert usable and usable[0] == pd.Timestamp('2024-01-02 09:59')
        assert all(t >= pd.Timestamp('2024-01-02 09:59') for t in usable)

    def test_slippage_is_charged_like_a_cost(self, monkeypatch):
        from app.services import backtest_handler as bh
        captured = {}

        class _FakeBacktest:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            def run(self):
                raise RuntimeError("stop after capturing the constructor arguments")

        def fake_backtest(*args, **kwargs):
            return _FakeBacktest(*args, **kwargs)

        monkeypatch.setattr(bh, 'Backtest', fake_backtest)
        lookup, stamps = self._hourly_predictions()
        bh._run_strategy_backtest(
            pred_lookup=lookup, pred_timestamps=stamps, exec_df=self._exec_1m(), strategy_params={},
            initial_capital=10000.0, position_sizing_type='percent', position_sizing_value=10.0,
            commission=0.10, slippage=0.05, buy_entry_conditions=None, sell_entry_conditions=None,
            exit_conditions=None,
        )
        assert captured['commission'] == pytest.approx(0.0015)

    def _model(self, window):
        from types import SimpleNamespace
        return SimpleNamespace(model_id='mdl-1', training_date_range=window)

    @staticmethod
    def _pred_df(start):
        return pd.DataFrame({'Date': pd.date_range(start, periods=30, freq='D')})

    def test_window_overlapping_the_training_data_is_refused(self):
        from app.services.backtest_handler import ensure_out_of_sample
        with pytest.raises(ValueError, match="in-sample"):
            ensure_out_of_sample(self._model({'startDate': '2020-01-01', 'endDate': '2024-01-20'}),
                                 self._pred_df('2024-01-01'), 5, {})

    def test_window_after_the_training_end_is_accepted(self):
        from app.services.backtest_handler import ensure_out_of_sample
        # first PREDICTION is row seq_len-1 = 2024-01-05, after the 2024-01-04 training end
        ensure_out_of_sample(self._model({'startDate': '2020-01-01', 'endDate': '2024-01-04'}),
                             self._pred_df('2024-01-01'), 5, {})

    def test_training_end_day_itself_is_in_sample(self):
        from app.services.backtest_handler import ensure_out_of_sample
        with pytest.raises(ValueError, match="in-sample"):
            ensure_out_of_sample(self._model({'endDate': '2024-01-05'}), self._pred_df('2024-01-01'), 5, {})

    def test_unknown_training_window_is_refused_unless_explicitly_allowed(self):
        from app.services.backtest_handler import ensure_out_of_sample
        with pytest.raises(ValueError, match="no recorded training end date"):
            ensure_out_of_sample(self._model(None), self._pred_df('2024-01-01'), 5, {})
        ensure_out_of_sample(self._model(None), self._pred_df('2024-01-01'), 5, {'allow_in_sample': True})


# ---------------------------------------------------------------------------------------
# #14 / #15  truncated tail windows written as 0 / timeout; two triple-barrier implementations
# ---------------------------------------------------------------------------------------
class TestBarrierTargets:
    @staticmethod
    def _flat(n=20, price=100.0):
        return pd.DataFrame({'Date': pd.date_range('2024-01-01', periods=n, freq='D'),
                             'Open': price, 'High': price + 0.1, 'Low': price - 0.1, 'Close': price,
                             'Volume': 1.0})

    def test_price_based_truncated_window_is_nan_not_zero(self):
        from app.services.darts_models import PredictionTargetService
        df = self._flat(20)
        s = PredictionTargetService()._calculate_single_target(df, 5.0, 2.0, 5, 'up')
        # flat prices: no barrier is ever hit. Rows with a full 5-bar window are real negatives (0);
        # the last 5 rows cannot be judged and must be NaN
        assert (s.iloc[:15] == 0).all()
        assert s.iloc[15:].isna().all()

    def test_price_based_hit_inside_a_truncated_window_is_still_a_label(self):
        from app.services.darts_models import PredictionTargetService
        df = self._flat(20)
        df.loc[18, 'High'] = 110.0          # profit touched 1 bar after row 17 (window truncated)
        s = PredictionTargetService()._calculate_single_target(df, 5.0, 2.0, 5, 'up')
        assert s.iloc[17] == 1.0 and s.iloc[16] == 1.0

    def test_triple_barrier_classes_and_truncated_tail(self):
        from app.services.darts_models import PredictionTargetService
        df = self._flat(20)
        df.loc[6, 'High'] = 110.0           # profit barrier (row 5 sees it 1 bar ahead)
        df.loc[12, 'Low'] = 90.0            # stop barrier (row 11 sees it 1 bar ahead)
        s = PredictionTargetService().calculate_triple_barrier(df, 5.0, 5.0, 3)
        assert s.iloc[5] == 2               # profit
        assert s.iloc[11] == 0              # stop
        assert s.iloc[0] == 1               # full window, nothing hit: timeout
        assert s.iloc[17:].isna().all()     # window of 3 runs past the data (and nothing hit): unknown
        assert s.iloc[16] == 1              # last row with a complete window

    def test_training_job_uses_the_shared_triple_barrier(self):
        from app.services.job_handler import build_prediction_targets
        from app.services.darts_models import PredictionTargetService
        df = self._flat(30)
        df.loc[10, 'High'] = 111.0          # a High-only touch: the old Close-only loop never saw it
        frame, col, cols, horizons = build_prediction_targets(
            df.copy(), [{'type': 'triple_barrier', 'profitPct': 5.0, 'stopPct': 5.0, 'maxBars': 4}], 'daily'
        )
        expected = PredictionTargetService().calculate_triple_barrier(df, 5.0, 5.0, 4)
        pd.testing.assert_series_equal(frame[col].reset_index(drop=True), expected.reset_index(drop=True),
                                       check_names=False)
        assert frame[col].iloc[9] == 2 and horizons[col] == 4


# ---------------------------------------------------------------------------------------
# #13 tsai trained on TSStandardize-d batches but scored / reloaded WITHOUT it
# ---------------------------------------------------------------------------------------
class TestTsaiNoHiddenBatchTransform:
    def test_train_model_applies_no_batch_standardize(self, monkeypatch):
        from types import SimpleNamespace
        from app.services import tsai_training
        from app.services.tsai_training import TSAITrainingService
        import torch

        seen = {}

        class FakeLearner:
            recorder = SimpleNamespace(values=[])

            def __init__(self, dls, model, **kwargs):
                self.model = model

            def fit_one_cycle(self, *a, **k):
                pass

        def fake_dls(X, y, splits=None, bs=None, batch_tfms=None, device=None, **kwargs):
            seen['batch_tfms'] = batch_tfms
            return object()

        class Standardize:                      # stand-in marker for tsai's TSStandardize
            pass

        for name, value in (('TSAI_AVAILABLE', True), ('get_ts_dls', fake_dls), ('Learner', FakeLearner),
                            ('TSStandardize', Standardize), ('accuracy', object()), ('F1Score', lambda: 1),
                            ('Precision', lambda: 1), ('Recall', lambda: 1), ('torch', torch)):
            monkeypatch.setattr(tsai_training, name, value, raising=False)

        svc = TSAITrainingService(normalize=True)
        X = np.random.RandomState(0).rand(40, 3, 8).astype('float32')
        y = (np.arange(40) % 2).astype('int64')
        res = svc.train_model(torch.nn.Linear(1, 1), (X[:30], y[:30]), val_data=(X[30:], y[30:]),
                              epochs=1, loss_fn=object())
        assert res['status'] == 'success', res
        # the scaling that is persisted with the model (DataPreparationService) is the ONLY scaling:
        # a batch transform estimated on one training batch is not saved and not applied by
        # assess_model / predict / the reload path, so it silently changed what the net saw
        assert not seen['batch_tfms']
