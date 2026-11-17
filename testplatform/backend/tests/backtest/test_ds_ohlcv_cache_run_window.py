"""DeterministicScorer's process-wide OHLCV frame must never answer a decision it does not cover.

THE DEFECT (measured 2026-11-17 on a distributed GA worker). ``data.fetch_ohlcv`` keeps one frame per
symbol for the life of the PROCESS and reused it whenever the decision's lookback START was covered.
In a backtest that frame comes from ``MemoizedOHLCVProvider``, which serves the series clamped to the
RUN's window ``[start - warmup, end]``. GA worker processes outlive a job, so a worker that evaluated a
job ending 2021-12-31 answered the 2022-2023 decisions of its NEXT job from a frame that stops in 2021:
every indicator froze on the last 2021 bar. Same genome, fresh process: 107 trades / +9.49%; process
holding 2021-clamped frames: 707 trades / +15.36%.

The fix keys reuse on END coverage as well (the end the provider DECLARED it serves, never the frame's
last bar -- a symbol whose history genuinely stops early must still be fetched once per run) and on the
history SOURCE the frame came from.

Run from the backend dir:
    python -m pytest tests/backtest/test_ds_ohlcv_cache_run_window.py -v
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from ba2_experts.DeterministicScorer import data as ds_data
from ba2_experts.DeterministicScorer.technical import technical_score

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import MemoizedOHLCVProvider

SYMBOL = "AAA"
#: The provider's whole history (what a parquet cache would hold).
HISTORY = pd.bdate_range("2018-01-01", "2023-12-29")


def _frame(dates) -> pd.DataFrame:
    """A deterministic random walk with real High/Low spread, so every indicator moves."""
    rng = np.random.default_rng(7)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(HISTORY))))
    full = pd.DataFrame({
        "Date": HISTORY, "Open": closes * 0.998, "High": closes * 1.01,
        "Low": closes * 0.99, "Close": closes, "Volume": 1_000_000})
    return full[full["Date"].isin(dates)].reset_index(drop=True)


class _Inner:
    """An in-memory OHLCV source (no ``get_provider_name``: MemoizedOHLCVProvider serves it directly,
    clamped to its run bounds). ``last`` cuts the history short, like a delisted symbol."""

    def __init__(self, last=None):
        self.calls = 0
        self._dates = HISTORY if last is None else HISTORY[HISTORY <= pd.Timestamp(last)]

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **_kw):
        self.calls += 1
        df = _frame(self._dates)
        lo = pd.Timestamp(start_date) if start_date is not None else df["Date"].min()
        hi = pd.Timestamp(end_date) if end_date is not None else df["Date"].max()
        return df[(df["Date"] >= lo) & (df["Date"] <= hi)].reset_index(drop=True)


class _OtherSourceInner(_Inner):
    """A second history source (another class = another native-cache folder): same dates, other prices."""

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **_kw):
        df = super().get_ohlcv_data(symbol, start_date, end_date, interval)
        for col in ("Open", "High", "Low", "Close"):
            df[col] = df[col] * 2.0
        return df


class _Bundle:
    def __init__(self, ohlcv):
        self._o = ohlcv

    def ohlcv(self):
        return self._o


def _run_provider(inner, start, end):
    """The provider ``run_daily_backtest`` builds for a run over ``[start, end]`` (warmup folded into
    ``start``); not hermetic so the in-memory source is read directly."""
    return _Bundle(MemoizedOHLCVProvider(inner, start, end, interval="1d", cached_only=False))


@pytest.fixture(autouse=True)
def _fresh_process_state():
    ds_data.reset_caches()
    ps_mod.clear_ohlcv_memo()
    yield
    ds_data.reset_caches()
    ps_mod.clear_ohlcv_memo()


def _score(df):
    return technical_score(df, {}, view=ds_data.frame_view(SYMBOL))


def test_a_later_run_is_not_answered_from_a_frame_clamped_to_an_earlier_run_end():
    inner = _Inner()
    # Job 1: window ends 2021-12-31. Two decisions, as a GA trial makes them.
    job1 = _run_provider(inner, datetime(2019, 1, 1), datetime(2021, 12, 31))
    ds_data.fetch_ohlcv(job1, SYMBOL, datetime(2021, 6, 1))
    last_job1 = ds_data.fetch_ohlcv(job1, SYMBOL, datetime(2021, 12, 30))
    assert pd.Timestamp(last_job1["Date"].max()) == pd.Timestamp("2021-12-30")

    # Job 2, SAME process: window ends 2023-12-31, its first decision is in 2022.
    job2 = _run_provider(inner, datetime(2020, 6, 1), datetime(2023, 12, 31))
    decision = datetime(2022, 6, 1)
    stale = ds_data.fetch_ohlcv(job2, SYMBOL, decision)
    assert pd.Timestamp(stale["Date"].max()) == pd.Timestamp("2022-06-01"), (
        "the decision was answered from the previous job's frame, which ends at its window end")
    warmed = _score(stale)

    # The same decision from a FRESH process state.
    ds_data.reset_caches()
    ps_mod.clear_ohlcv_memo()
    fresh_df = ds_data.fetch_ohlcv(job2, SYMBOL, decision)
    fresh = _score(fresh_df)
    pd.testing.assert_frame_equal(stale, fresh_df)
    assert warmed == fresh, "an indicator computed after E1 differs from a fresh process's"


def test_later_decisions_of_the_later_run_keep_advancing():
    inner = _Inner()
    ds_data.fetch_ohlcv(_run_provider(inner, datetime(2019, 1, 1), datetime(2021, 12, 31)),
                       SYMBOL, datetime(2021, 6, 1))
    job2 = _run_provider(inner, datetime(2020, 6, 1), datetime(2023, 12, 31))
    for day in ("2022-03-01", "2023-03-01", "2023-12-28"):
        df = ds_data.fetch_ohlcv(job2, SYMBOL, datetime.fromisoformat(day))
        assert pd.Timestamp(df["Date"].max()) == pd.Timestamp(day)


def test_one_fetch_per_symbol_per_run():
    inner = _Inner()
    job = _run_provider(inner, datetime(2019, 1, 1), datetime(2023, 12, 31))
    ds_data.fetch_ohlcv(job, SYMBOL, datetime(2021, 1, 4))
    fingerprint = ds_data.frame_view(SYMBOL).fingerprint
    for day in pd.bdate_range("2021-01-05", "2023-12-29")[::7]:
        ds_data.fetch_ohlcv(job, SYMBOL, day.to_pydatetime())
    # Including the last day of the run, decided late in the session (past the end's midnight stamp).
    ds_data.fetch_ohlcv(job, SYMBOL, datetime(2023, 12, 31, 16, 0))
    assert ds_data.frame_view(SYMBOL).fingerprint == fingerprint, "the frame was refetched mid-run"
    assert inner.calls == 1


def test_a_history_that_genuinely_ends_early_is_fetched_once_not_on_every_decision():
    """A delisted symbol: the run window reaches 2023, the provider's bars stop in mid-2022. The frame
    covers the run (the provider has nothing later to give), so it must not be refetched per decision."""
    inner = _Inner(last="2022-06-30")
    job = _run_provider(inner, datetime(2019, 1, 1), datetime(2023, 12, 31))
    ds_data.fetch_ohlcv(job, SYMBOL, datetime(2021, 1, 4))
    fingerprint = ds_data.frame_view(SYMBOL).fingerprint
    for day in pd.bdate_range("2022-07-01", "2023-12-29")[::5]:
        df = ds_data.fetch_ohlcv(job, SYMBOL, day.to_pydatetime())
        assert pd.Timestamp(df["Date"].max()) == pd.Timestamp("2022-06-30")
    assert ds_data.frame_view(SYMBOL).fingerprint == fingerprint, "a short history was refetched"
    assert inner.calls == 1


def test_a_new_working_set_drops_the_expert_frame_caches():
    """Belt and braces (memory, not correctness): the per-run eviction chokepoint and the worker's
    memory release also drop DeterministicScorer's frames."""
    job = _run_provider(_Inner(), datetime(2019, 1, 1), datetime(2023, 12, 31))
    ds_data.fetch_ohlcv(job, SYMBOL, datetime(2022, 6, 1))
    assert ds_data.frame_view(SYMBOL) is not None
    ps_mod.evict_memo_if_working_set_changed(("job-a",))
    assert ds_data.frame_view(SYMBOL) is None and not ds_data._OHLCV_COVERAGE
    ds_data.fetch_ohlcv(job, SYMBOL, datetime(2022, 6, 1))
    assert ps_mod.evict_memo_if_working_set_changed(("job-a",)) is False
    assert ds_data.frame_view(SYMBOL) is not None, "the same working set must keep its frames"
    ps_mod.clear_expert_frame_caches()
    assert ds_data.frame_view(SYMBOL) is None


def test_a_frame_from_another_history_source_is_never_served():
    """fmp vs alpaca history: one worker process can run jobs on both sources, same window."""
    window = (datetime(2019, 1, 1), datetime(2023, 12, 31))
    first = ds_data.fetch_ohlcv(_run_provider(_Inner(), *window), SYMBOL, datetime(2022, 6, 1))
    other = ds_data.fetch_ohlcv(_run_provider(_OtherSourceInner(), *window), SYMBOL,
                                datetime(2022, 6, 1))
    assert other["Close"].iloc[-1] == pytest.approx(2.0 * first["Close"].iloc[-1]), (
        "the other source's decision was answered from the first source's frame")


def test_the_index_closes_memo_is_not_served_across_sources():
    """``fetch_index_closes`` memoises on (index, as_of): a hit must still be a frame of THIS source."""
    window = (datetime(2019, 1, 1), datetime(2023, 12, 31))
    as_of = datetime(2022, 6, 1)
    first = ds_data.fetch_index_closes(_run_provider(_Inner(), *window), as_of, index_symbol=SYMBOL)
    other = ds_data.fetch_index_closes(_run_provider(_OtherSourceInner(), *window), as_of,
                                       index_symbol=SYMBOL)
    assert other.iloc[-1] == pytest.approx(2.0 * first.iloc[-1])
