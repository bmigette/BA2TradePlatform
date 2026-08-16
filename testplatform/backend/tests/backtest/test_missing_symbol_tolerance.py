"""Missing-symbol tolerance in preload: survive an isolated cache gap, still fail on a broken cache.

WHY: opt 324 (scr-mid-FMPRating-S5-goal2020-riskatr) aborted after 25 minutes because ONE symbol
of 586 (NBIX) had no cached 5min parquet -- 0.17% of the universe. daily_engine already tolerates
a per-symbol miss at six sites during the run; only this preload gate was all-or-nothing.

The bound is deliberately tight and LOUD. Dropping symbols changes the universe and therefore the
results, so a tolerated run is NOT comparable with a full-coverage one -- the whole risk here is a
job quietly backtesting a different universe than its name implies.
"""
import logging

import pytest

from app.services.backtest import price_source as ps


class _PartialOHLCV:
    """Provider where a named set of symbols has no cached bars."""

    def __init__(self, missing):
        self.missing = set(missing)

    def read_window(self, symbol, start, end, interval):
        import pandas as pd
        if symbol in self.missing:
            raise ps.BacktestCacheMiss(f"no cached bars for {symbol}")
        return pd.DataFrame({
            "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
            "Close": [1.0, 2.0], "Volume": [10, 20],
        })


def _preload(missing, n_symbols):
    from datetime import datetime
    syms = [f"S{i}" for i in range(n_symbols)]
    for m in missing:
        syms[int(m[1:])] = m
    src = ps.AsOfPriceSource(ohlcv_provider=_PartialOHLCV(missing), interval="1d")
    src.preload(syms, datetime(2024, 1, 2), datetime(2024, 1, 3), warmup_days=0)
    return src


@pytest.fixture(autouse=True)
def _clean():
    ps.clear_worker_bar_cache()
    yield
    ps.clear_worker_bar_cache()


def test_one_missing_of_many_is_tolerated_not_fatal(monkeypatch, caplog):
    """The opt-324 case: 1 of 586. A 25-minute run must not die for 0.17% of the universe."""
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_ABS", 5)
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_FRAC", 0.01)
    with caplog.at_level(logging.DEBUG):
        src = _preload(["S7"], 586)
    assert src._dropped_symbols == ["S7"]
    assert not src.has_symbol("S7")
    assert src.has_symbol("S8")          # the rest of the universe loaded fine


def test_the_drop_is_logged_at_WARNING_and_names_the_symbols(caplog):
    """Must survive the optimize run's global logging.disable(INFO) -- a silent universe change
    is exactly the failure mode this must not become."""
    prior = logging.root.manager.disable
    logging.disable(logging.INFO)        # what a real optimize run installs
    try:
        with caplog.at_level(logging.DEBUG):
            _preload(["S3"], 200)
        assert "TOLERATED" in caplog.text
        assert "S3" in caplog.text
        assert "not" in caplog.text and "comparable" in caplog.text
    finally:
        logging.disable(prior)


def test_too_many_missing_still_fails_hard(monkeypatch):
    """Beyond the bound it is a broken cache, not a gap. Backtesting a fraction of the intended
    universe would produce confident, meaningless results -- fail instead."""
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_ABS", 5)
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_FRAC", 0.01)
    with pytest.raises(ps.BacktestCacheMiss) as ei:
        _preload([f"S{i}" for i in range(20)], 100)
    assert "20 of 100" in str(ei.value)
    assert "fetch-cache" in str(ei.value)     # keeps the actionable remedy


def test_fraction_bound_protects_a_LARGE_universe(monkeypatch):
    """5 absolute would pass here, but on a small universe 5 is a big share -- the fraction bound
    is what stops a 20-symbol fixture tolerating 25% loss."""
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_ABS", 5)
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_FRAC", 0.01)
    with pytest.raises(ps.BacktestCacheMiss):
        _preload(["S1", "S2", "S3"], 20)      # 3 <= 5 abs, but 15% >> 1%


def test_absolute_bound_protects_a_HUGE_universe(monkeypatch):
    """Conversely 1% of 1368 is 13 symbols -- the absolute cap stops that being waved through."""
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_ABS", 5)
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_FRAC", 0.01)
    with pytest.raises(ps.BacktestCacheMiss):
        _preload([f"S{i}" for i in range(10)], 1368)   # 0.7% frac, but 10 > 5 abs


def test_zero_restores_the_old_fail_on_any_behaviour(monkeypatch):
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_ABS", 0)
    with pytest.raises(ps.BacktestCacheMiss):
        _preload(["S1"], 586)


def test_no_missing_symbols_records_nothing(monkeypatch):
    src = _preload([], 50)
    assert src._dropped_symbols == []
