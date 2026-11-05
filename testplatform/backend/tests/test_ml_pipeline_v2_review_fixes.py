"""ML exploration v2, phase 6 review fixes.

1 a dataset build FAILS on a macro refusal (no silent finish without macro columns);
2 the model-predictions endpoint decodes k-class output; 3 (first release wins: see test_ml_pipeline_v2_cleanups);
4 /calculate-macro defaults to the cached series only. Tiny synthetic data, no network.
"""
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def fred_cache(tmp_path, monkeypatch):
    """A temp FRED cache the first-release reader (ba2_providers) is pointed at."""
    from ba2_providers.macro import fred_series as fs

    monkeypatch.setattr(fs, 'CACHE_FOLDER', str(tmp_path / 'cache'))
    fs.reset_cache()

    def write(series_id, observations, fetched_at=None, first_vintage='2000-01-03'):
        os.makedirs(os.path.join(fs.CACHE_FOLDER, 'fred'), exist_ok=True)
        doc = {'series_id': series_id, 'fetched_at': fetched_at or pd.Timestamp.now(tz='UTC').isoformat(),
               'availability': fs.AVAIL_FIRST_RELEASE, 'format': fs.CACHE_FORMAT_FIRST_RELEASE,
               'first_vintage': first_vintage, 'observations': observations}
        with open(fs.cache_path(series_id), 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)

    yield write
    fs.reset_cache()


def _bars(start, periods):
    close = 100.0 + np.arange(periods, dtype=float)
    return pd.DataFrame({'Date': pd.date_range(start, periods=periods, freq='D'), 'Open': close,
                         'High': close + 1, 'Low': close - 1, 'Close': close, 'Volume': 1000.0})


class _FakeDB:
    """One Dataset-like record behind the SQLAlchemy calls the background builders make."""

    def __init__(self, record):
        self.record = record

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.record

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _record(tmp_path, **kw):
    base = dict(id=1, status='building', error_message=None, progress_message=None,
                file_path=str(tmp_path / 'ds.csv'), technical_indicators=None, sentiment_config=None,
                fundamentals_config=None, generation_config={}, start_date=None, end_date=None,
                rows_count=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------------------
# 1 a macro refusal fails the dataset task
# ---------------------------------------------------------------------------------------
class TestMacroRefusalFailsTheDataset:
    def test_add_macro_columns_prefixes_the_columns(self, fred_cache):
        from app.services.macro import MacroService
        fred_cache('UNRATE', [{'date': '2024-01-01', 'value': '3.9', 'realtime_start': '2024-02-02'}])
        out = MacroService().add_macro_columns(_bars('2024-02-05', 3), ['unemployment'])
        assert {'macro_unemployment', 'macro_unemployment_yoy_change', 'macro_unemployment_days_since'} \
            <= set(out.columns)
        assert 'unemployment' not in out.columns
        assert out['macro_unemployment'].tolist() == [3.9, 3.9, 3.9]

    def _regen(self, monkeypatch, tmp_path, macro):
        from app.services import dataset_handler
        record = _record(tmp_path, fundamentals_config={'enabled': True, 'macro_indicators': macro})
        monkeypatch.setattr(dataset_handler, 'SessionLocal', lambda: _FakeDB(record))
        monkeypatch.setattr(dataset_handler, 'update_dataset_progress', lambda *a, **k: None)
        monkeypatch.setattr(dataset_handler, 'get_task_queue',
                            lambda: SimpleNamespace(update_progress=lambda *a, **k: None))
        payload = {'dataset_id': 1, 'ticker': 'AAA', 'timeframe': '1d',
                   'ohlcv_data': _bars('2024-02-05', 5).assign(Date=lambda d: d['Date'].astype(str)).to_dict('records')}
        return dataset_handler, record, payload

    def test_task_handler_fails_on_gdp_and_records_the_error(self, monkeypatch, tmp_path, fred_cache):
        handler, record, payload = self._regen(monkeypatch, tmp_path, ['gdp'])
        with pytest.raises(ValueError, match='GDP'):
            handler.handle_dataset_regeneration('t1', payload)
        assert record.status == 'error' and 'GDP' in record.error_message

    def test_task_handler_fails_on_a_missing_cache_file_naming_it(self, monkeypatch, tmp_path, fred_cache):
        handler, record, payload = self._regen(monkeypatch, tmp_path, ['inflation'])
        with pytest.raises(Exception, match=r'CPIAUCSL\.json'):
            handler.handle_dataset_regeneration('t2', payload)
        assert record.status == 'error' and 'CPIAUCSL.json' in record.error_message

    def test_task_handler_still_finishes_with_a_cached_series(self, monkeypatch, tmp_path, fred_cache):
        fred_cache('UNRATE', [{'date': '2024-01-01', 'value': '3.9', 'realtime_start': '2024-02-02'}])
        handler, record, payload = self._regen(monkeypatch, tmp_path, ['unemployment'])
        assert handler.handle_dataset_regeneration('t3', payload)['status'] == 'success'
        assert 'macro_unemployment' in pd.read_csv(record.file_path).columns

    @pytest.mark.parametrize('macro,needle', [(['gdp'], 'GDP'), (['inflation'], 'CPIAUCSL.json')])
    def test_background_build_ends_in_error_status(self, monkeypatch, tmp_path, fred_cache, macro, needle):
        from app.api import datasets
        record = _record(tmp_path)
        monkeypatch.setattr(datasets, 'SessionLocal', lambda: _FakeDB(record))
        monkeypatch.setattr(datasets, 'fetch_ohlcv_frame', lambda *a, **k: _bars('2024-02-05', 30))
        datasets._build_dataset_in_background(1, {
            'ticker': 'AAA', 'timeframe': '1d', 'start_date': '2024-02-05', 'end_date': '2024-03-05',
            'technical_indicators': [], 'sentiment_config': {},
            'fundamentals_config': {'enabled': True, 'macro_indicators': macro}})
        assert record.status == 'error' and needle in record.error_message
        assert not os.path.exists(record.file_path)

    @pytest.mark.parametrize('macro,needle', [(['gdp'], 'GDP'), (['inflation'], 'CPIAUCSL.json')])
    def test_background_regeneration_ends_in_error_status(self, monkeypatch, tmp_path, fred_cache, macro, needle):
        """A saved config that still holds 'gdp' fails on regeneration; nothing filters it."""
        from app.api import datasets
        record = _record(tmp_path, status='ready')
        _bars('2024-02-05', 10).to_csv(record.file_path, index=False)
        monkeypatch.setattr(datasets, 'SessionLocal', lambda: _FakeDB(record))
        opts = SimpleNamespace(regenerate_ohlcv=False, regenerate_technical=False, regenerate_sentiment=False,
                               regenerate_fundamentals=False, regenerate_macro=True)
        datasets._regenerate_dataset_in_background(1, {
            'regen_options': opts, 'ticker': 'AAA', 'timeframe': '1d', 'file_path': record.file_path,
            'fundamentals_config': {'enabled': True, 'macro_indicators': macro},
            'start_date': datetime_(2024, 2, 5), 'end_date': datetime_(2024, 2, 14)})
        assert record.status == 'error' and needle in record.error_message
        assert 'macro_' not in ''.join(pd.read_csv(record.file_path).columns)


def datetime_(y, m, d):
    from datetime import datetime
    return datetime(y, m, d)


# ---------------------------------------------------------------------------------------
# 2 model predictions decode k-class output
# ---------------------------------------------------------------------------------------
class TestPredictionDecoding:
    def test_binary_keeps_the_threshold_on_p_class_1(self):
        from app.api.models import decode_class_probs
        probs = np.array([[0.7, 0.3], [0.2, 0.8]])
        classes, p, class_probs = decode_class_probs(probs, 0.5, 'shift')
        assert classes.tolist() == [0, 1] and p.tolist() == [0.3, 0.8] and class_probs is None
        assert decode_class_probs(probs, 0.25, 'shift')[0].tolist() == [1, 1]

    def test_three_classes_use_the_argmax_and_return_every_probability(self):
        from app.api.models import decode_class_probs
        probs = np.array([[0.1, 0.2, 0.7], [0.6, 0.3, 0.1], [0.2, 0.5, 0.3]])
        classes, p, class_probs = decode_class_probs(probs, 0.99, 'shift')   # threshold has no meaning
        assert classes.tolist() == [2, 0, 1]
        assert p.tolist() == [0.7, 0.6, 0.5]                                  # probability of the predicted class
        assert class_probs.tolist() == probs.tolist()

    def test_multistep_still_averages_the_horizons(self):
        from app.api.models import decode_class_probs
        classes, p, class_probs = decode_class_probs(np.array([[0.9, 0.5, 0.7], [0.1, 0.2, 0.3]]), 0.5, 'multistep')
        assert classes.tolist() == [1, 0] and p == pytest.approx([0.7, 0.2]) and class_probs is None

    def test_prediction_records_carry_class_probabilities_and_correctness_for_k_classes(self):
        from app.api.models import prediction_record
        probs = np.array([0.1, 0.2, 0.7])
        rec = prediction_record(predicted=2, probability=0.7, actual=2, class_probs=probs)
        assert rec['predictedClass'] == 2 and rec['correct'] is True
        assert rec['classProbabilities'] == [0.1, 0.2, 0.7]
        wrong = prediction_record(predicted=2, probability=0.7, actual=1, class_probs=probs)
        assert wrong['correct'] is False

    def test_binary_prediction_record_is_unchanged(self):
        from app.api.models import prediction_record
        rec = prediction_record(predicted=1, probability=0.8, actual=0, class_probs=None)
        assert rec == {'actual': 0, 'probability': 0.8, 'predictedClass': 1, 'correct': False}


# ---------------------------------------------------------------------------------------
# 4 /calculate-macro asks only for what the cache holds
# ---------------------------------------------------------------------------------------
class TestCalculateMacroEndpoint:
    def _call(self, tmp_path, **kw):
        from app.api import datasets
        csv = tmp_path / 'm.csv'
        _bars('2024-02-05', 6).to_csv(csv, index=False)
        db = _FakeDB(SimpleNamespace(file_path=str(csv)))
        return datasets.calculate_macro_features(1, db=db, **kw), csv

    def test_defaults_are_the_cached_indicators_and_no_yield_curve(self, tmp_path, fred_cache):
        from app.api import datasets
        from app.services.macro import MacroService
        for sid in ('FEDFUNDS', 'CPIAUCSL', 'UNRATE', 'VIXCLS', 'DGS10'):
            fred_cache(sid, [{'date': '2024-01-01', 'value': '1.0', 'realtime_start': '2024-02-02'}])
        res, csv = self._call(tmp_path, indicators=None, include_yield_curve=False)
        assert res['include_yield_curve'] is False
        assert set(res['indicators']) == set(MacroService.cached_indicators())
        assert 'gdp' not in res['indicators'] and 'yield_2y' not in res['indicators']
        assert 'yield_spread' not in pd.read_csv(csv).columns
        import inspect
        assert inspect.signature(datasets.calculate_macro_features).parameters['include_yield_curve'].default is False

    @pytest.mark.parametrize('kw,needle', [
        ({'indicators': ['gdp']}, 'GDP'),
        ({'indicators': ['yield_2y']}, 'DGS2'),
        ({'indicators': ['vix'], 'include_yield_curve': True}, 'DGS2'),
    ])
    def test_asking_for_what_the_cache_lacks_is_a_400_naming_the_series(self, tmp_path, fred_cache, kw, needle):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            self._call(tmp_path, **kw)
        assert e.value.status_code == 400 and needle in e.value.detail

    def test_the_supported_list_is_the_cached_set(self):
        from app.services.macro import MacroService
        assert 'gdp' not in MacroService.get_supported_indicators()
        assert 'yield_2y' not in MacroService.get_supported_indicators()
        assert 'vix' in MacroService.get_supported_indicators()


# ---------------------------------------------------------------------------------------
# code-quality review: coverage is checked up front, one clear error
# ---------------------------------------------------------------------------------------
UNRATE_ROWS = [{'date': '2024-01-01', 'value': '3.9', 'realtime_start': '2024-02-02'}]


class TestMacroCoverageUpFront:
    def _svc(self):
        from app.services.macro import MacroService
        return MacroService()

    def test_a_dataset_past_the_cache_fetch_day_is_refused_with_the_last_coverable_bar(self, fred_cache):
        from app.services.macro import MacroCoverageError
        fred_cache('UNRATE', UNRATE_ROWS, fetched_at='2026-10-28T12:00:00+00:00')
        # bar 10-28 decides on the next session 10-29, a day the cache (fetched 10-28) cannot cover
        with pytest.raises(MacroCoverageError) as e:
            self._svc().add_macro_columns(_bars('2026-10-26', 3), ['unemployment'])
        msg = str(e.value)
        assert 'UNRATE' in msg and '2026-10-28' in msg                       # series and the cache's fetch day
        assert 'end the dataset on or before 2026-10-27' in msg              # last coverable bar
        assert 'refresh the FRED cache' in msg

    def test_a_dataset_inside_the_coverage_still_builds(self, fred_cache):
        fred_cache('UNRATE', UNRATE_ROWS, fetched_at='2026-10-28T12:00:00+00:00')
        out = self._svc().add_macro_columns(_bars('2026-10-26', 2), ['unemployment'])
        assert out['macro_unemployment'].tolist() == [3.9, 3.9]

    def test_rows_before_the_first_vintage_are_refused_with_the_first_coverable_bar(self, fred_cache):
        from app.services.macro import MacroCoverageError
        fred_cache('UNRATE', UNRATE_ROWS, first_vintage='2024-03-01')
        with pytest.raises(MacroCoverageError) as e:
            self._svc().add_macro_columns(_bars('2024-02-26', 10), ['unemployment'])
        msg = str(e.value)
        assert 'UNRATE' in msg and '2024-03-01' in msg
        assert 'start the dataset on or after 2024-03-01' in msg

    def test_the_check_runs_before_any_cache_read(self, fred_cache, monkeypatch):
        from ba2_providers.macro import fred_series as fs
        from app.services.macro import MacroCoverageError
        fred_cache('UNRATE', UNRATE_ROWS, fetched_at='2026-10-28T12:00:00+00:00')
        monkeypatch.setattr(fs, 'get_series_as_of', lambda *a, **k: pytest.fail('read before the coverage check'))
        with pytest.raises(MacroCoverageError):
            self._svc().add_macro_columns(_bars('2026-10-26', 3), ['unemployment'])

    def test_calculate_macro_returns_409_with_that_message(self, tmp_path, fred_cache):
        from fastapi import HTTPException
        from app.api import datasets
        fred_cache('UNRATE', UNRATE_ROWS, fetched_at='2026-10-28T12:00:00+00:00')
        csv = tmp_path / 'm.csv'
        _bars('2026-10-26', 3).to_csv(csv, index=False)
        with pytest.raises(HTTPException) as e:
            datasets.calculate_macro_features(1, indicators=['unemployment'], include_yield_curve=False,
                                              db=_FakeDB(SimpleNamespace(file_path=str(csv))))
        assert e.value.status_code == 409
        assert 'end the dataset on or before 2026-10-27' in e.value.detail

    def test_a_dataset_task_fails_with_the_same_message(self, monkeypatch, tmp_path, fred_cache):
        from app.api import datasets
        fred_cache('UNRATE', UNRATE_ROWS, fetched_at='2026-10-28T12:00:00+00:00')
        record = _record(tmp_path)
        monkeypatch.setattr(datasets, 'SessionLocal', lambda: _FakeDB(record))
        monkeypatch.setattr(datasets, 'fetch_ohlcv_frame', lambda *a, **k: _bars('2026-10-26', 3))
        datasets._build_dataset_in_background(1, {
            'ticker': 'AAA', 'timeframe': '1d', 'start_date': '2026-10-26', 'end_date': '2026-10-28',
            'technical_indicators': [], 'sentiment_config': {},
            'fundamentals_config': {'enabled': True, 'macro_indicators': ['unemployment']}})
        assert record.status == 'error' and 'end the dataset on or before 2026-10-27' in record.error_message


class TestMetricFitnessStrict:
    def test_a_missing_error_metric_raises(self):
        from app.services.job_handler import metric_fitness
        for name in ('mae', 'mse', 'rmse', 'mape'):
            with pytest.raises(KeyError, match=name):
                metric_fitness(name, {})

    def test_a_none_error_metric_is_the_worst_fitness(self):
        from app.services.job_handler import metric_fitness
        for name in ('mae', 'mse', 'rmse'):
            assert metric_fitness(name, {name: None}) == 0.0
