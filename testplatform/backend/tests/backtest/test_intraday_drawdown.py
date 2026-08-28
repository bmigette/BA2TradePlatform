"""``intraday_drawdown`` -- post-hoc drawdown refinement for options trades whose realised P&L
came from DAILY option-premium bars only. Pure-function tests: all data access is faked via
plain dicts/callables, no real cache files or account objects needed.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_intraday_drawdown.py -v
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest

from app.services.backtest.intraday_drawdown import (
    estimate_worst_intraday_pnl,
    is_flagged_for_intraday_check,
    refine_max_drawdown,
)


# ---------------------------------------------------------------------------
# is_flagged_for_intraday_check
# ---------------------------------------------------------------------------

def test_flagged_when_bars_held_is_one():
    assert is_flagged_for_intraday_check({"bars_held": 1}, prior_bar_low=100.0, exit_bar_low=101.0) is True


def test_flagged_when_bars_held_is_zero():
    assert is_flagged_for_intraday_check({"bars_held": 0}, prior_bar_low=None, exit_bar_low=None) is True


def test_flagged_when_exit_day_makes_a_new_lower_low():
    assert is_flagged_for_intraday_check({"bars_held": 3}, prior_bar_low=100.0, exit_bar_low=99.0) is True


def test_not_flagged_when_multi_bar_and_no_new_low():
    assert is_flagged_for_intraday_check({"bars_held": 3}, prior_bar_low=100.0, exit_bar_low=101.0) is False


def test_not_flagged_when_multi_bar_and_low_data_missing():
    assert is_flagged_for_intraday_check({"bars_held": 3}, prior_bar_low=None, exit_bar_low=None) is False


# ---------------------------------------------------------------------------
# estimate_worst_intraday_pnl
# ---------------------------------------------------------------------------

def test_long_option_worst_case_is_underlyings_low():
    """LONG (direction_sign=+1) option loses when premium drops; delta>0 (call-like) means the
    adverse move is the underlying's LOW within the window."""
    bars = [
        {"Low": 98.0, "High": 101.0},
        {"Low": 95.0, "High": 100.0},  # worst low of the window
    ]
    pnl = estimate_worst_intraday_pnl(
        entry_premium=5.0, entry_underlying_price=100.0, delta=0.5,
        size=1.0, multiplier=100.0, commission=0.0, bars_5m=bars, direction_sign=1.0,
    )
    # worst implied premium = 5.0 + 0.5*(95-100) = 2.5; pnl = (2.5-5.0)*1*100*1 = -250.
    assert pnl == pytest.approx(-250.0)


def test_short_option_worst_case_is_underlyings_high():
    """SHORT (direction_sign=-1) option loses when premium RISES; the adverse move is the
    underlying's HIGH (for a positive-delta/call-like contract)."""
    bars = [
        {"Low": 98.0, "High": 103.0},  # worst high of the window
        {"Low": 95.0, "High": 100.0},
    ]
    pnl = estimate_worst_intraday_pnl(
        entry_premium=5.0, entry_underlying_price=100.0, delta=0.5,
        size=1.0, multiplier=100.0, commission=0.0, bars_5m=bars, direction_sign=-1.0,
    )
    # worst implied premium = 5.0 + 0.5*(103-100) = 6.5; pnl = (6.5-5.0)*1*100*-1 = -150.
    assert pnl == pytest.approx(-150.0)


def test_negative_delta_put_worst_case_is_underlyings_high():
    """A negative delta (put-like) contract's adverse move for a LONG holder is the
    underlying's HIGH, not its low -- confirms no option_type input is needed, delta's sign
    alone determines the adverse side."""
    bars = [{"Low": 95.0, "High": 105.0}]
    pnl = estimate_worst_intraday_pnl(
        entry_premium=5.0, entry_underlying_price=100.0, delta=-0.5,
        size=1.0, multiplier=100.0, commission=0.0, bars_5m=bars, direction_sign=1.0,
    )
    # implied @ low=95:  5.0 + -0.5*(95-100)  = 7.5
    # implied @ high=105: 5.0 + -0.5*(105-100) = 2.5  <- worse (min) for a LONG holder
    assert pnl == pytest.approx((2.5 - 5.0) * 100.0)


def test_premium_floored_at_zero():
    """A huge adverse move can't imply a negative premium."""
    bars = [{"Low": 0.0, "High": 100.0}]
    pnl = estimate_worst_intraday_pnl(
        entry_premium=1.0, entry_underlying_price=100.0, delta=0.5,
        size=1.0, multiplier=100.0, commission=0.0, bars_5m=bars, direction_sign=1.0,
    )
    # implied @ low=0: 1.0 + 0.5*(0-100) = -49 -> floored to 0.
    assert pnl == pytest.approx((0.0 - 1.0) * 100.0)


def test_commission_subtracted():
    bars = [{"Low": 95.0, "High": 100.0}]
    pnl = estimate_worst_intraday_pnl(
        entry_premium=5.0, entry_underlying_price=100.0, delta=0.5,
        size=1.0, multiplier=100.0, commission=2.0, bars_5m=bars, direction_sign=1.0,
    )
    assert pnl == pytest.approx((2.5 - 5.0) * 100.0 - 2.0)


def test_no_bars_returns_none():
    assert estimate_worst_intraday_pnl(
        entry_premium=5.0, entry_underlying_price=100.0, delta=0.5,
        size=1.0, multiplier=100.0, commission=0.0, bars_5m=[], direction_sign=1.0,
    ) is None


# ---------------------------------------------------------------------------
# refine_max_drawdown (full pipeline, faked data access)
# ---------------------------------------------------------------------------

def _make_trade(**overrides):
    base = {
        "contract_symbol": "AAPL240419C00190000",
        "underlying_symbol": "AAPL",
        "entry_time": datetime(2024, 4, 3),
        "exit_time": datetime(2024, 4, 4),
        "direction": "buy",
        "entry_price": 5.0,
        "exit_price": 6.0,
        "size": 1.0,
        "pnl": 100.0,  # a real winner per the daily curve
        "bars_held": 1,
    }
    base.update(overrides)
    return base


def test_refine_worsens_drawdown_for_flagged_trade_with_hidden_dip():
    trade = _make_trade()
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,  # flags via bars_held=1 anyway
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # implied worst premium = 5.0 + 0.5*(90-100) = 0.0 (floored); worst_pnl = (0-5)*1*100 = -500.
    # extra_loss = min(0, -500 - 100) = -600; candidate_dd = -2.0 + (-600/20000*100) = -5.0.
    assert refined == pytest.approx(-5.0)
    assert refined < -2.0


def test_refine_leaves_drawdown_unchanged_when_no_hidden_dip():
    """A flagged trade whose 5m window's worst implied point is still at least as good as what
    was realised must not move max_drawdown at all -- here the underlying only ever trades
    ABOVE entry (102-105), so the worst implied premium (at Low=102) exactly matches the
    trade's actual realised profit (entry 5.0 -> exit 6.0 = +100)."""
    trade = _make_trade(pnl=100.0)
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 99.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 102.0, "High": 105.0}],
    )
    assert refined == pytest.approx(-2.0)


def test_refine_skips_unflagged_multi_bar_trade():
    trade = _make_trade(bars_held=5)
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 101.0,   # exit-day low
        prior_daily_bar_low=lambda sym, dt: 100.0,  # prior-day low; exit low is NOT lower -> unflagged
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 0.0, "High": 0.0}],  # would be catastrophic if used
    )
    assert refined == pytest.approx(-2.0)


def test_refine_skips_trade_missing_contract_symbol():
    """Equity trades (no contract_symbol) must be left alone entirely."""
    trade = _make_trade(contract_symbol=None)
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 0.0, "High": 0.0}],
    )
    assert refined == pytest.approx(-2.0)


def test_refine_is_best_effort_on_lookup_exception():
    """A data-access callable raising must not blow up the whole refinement pass -- just skip
    that trade and keep going."""
    trade = _make_trade()

    def _boom(*args, **kwargs):
        raise RuntimeError("cache unavailable")

    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=_boom,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    assert refined == pytest.approx(-2.0)


def test_build_refine_drawdown_fn_is_none_for_equity_only_account():
    """Real BacktestAccount, no options_provider configured (a plain equity backtest) ->
    _build_refine_drawdown_fn must return None (skip refinement) rather than raise, since
    account._options is None. Confirms the wiring degrades gracefully outside a synthetic
    stub, without needing real 5-minute/option-chain cache data."""
    from tests.backtest.test_round_trip_trades import _acct
    from app.services.backtest.results import _build_refine_drawdown_fn

    acct, ctx, _ps = _acct(account_id=999)
    try:
        assert _build_refine_drawdown_fn(acct, {"initial_capital": 100_000.0}) is None
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# The OPTION seam: _build_refine_drawdown_fn must follow the READER, not an
# incidental attribute only one backend happens to have.
# ---------------------------------------------------------------------------
class _RefinePrice:
    def bar_at(self, symbol, dt):
        return {"low": 99.0}

    def prev_bar(self, symbol, dt):
        return {"low": 100.0}

    def close_at(self, symbol, dt=None):
        return 100.0


def _no_fmp(monkeypatch):
    """The refinement builds an FMP 5-minute provider eagerly; keep it off the network."""
    from types import SimpleNamespace
    import ba2_providers

    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda *a, **k: SimpleNamespace(get_ohlcv_data=lambda *a, **k: None))


_REFINE_CFG = {"initial_capital": 100_000.0,
               "account_settings": {"commission_per_trade": 1.0}}

#: An entry timestamp, in the shape the refinement actually hands the seam.
_ENTRY = datetime(2024, 1, 2, 15, 45)


@contextmanager
def _captured_warnings():
    """``ba2_common``'s logger sets ``propagate = False``, so caplog's root handler never
    sees it; attach a collector directly."""
    import logging
    from ba2_common.logger import logger as ba2_logger

    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _Collect(level=logging.WARNING)
    ba2_logger.addHandler(h)
    try:
        yield records
    finally:
        ba2_logger.removeHandler(h)


def test_refinement_is_wired_for_the_PARQUET_backend_too(monkeypatch, tmp_path):
    """REGRESSION. ``_build_refine_drawdown_fn`` used to bind ``options.cache.db_path`` --
    an attribute only ``HistoricalOptionsProvider`` has -- so on a parquet-backed account it
    returned None and the intraday refinement switched itself OFF silently.

    That is not merely a missing feature: ``strategy_fitness`` divides by ``max_drawdown``
    for ``option_consistent_annual_return``, so the same strategy over the same window
    SCORED DIFFERENTLY depending on which option store served it, with nothing in the result
    to show why.
    """
    from types import SimpleNamespace
    from app.services.backtest.parquet_options_provider import (
        ParquetOptionsProvider, clear_worker_parquet_options_cache)
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    root = tmp_path / "TastyTradeOptionsProvider"
    root.mkdir()
    clear_worker_parquet_options_cache()
    try:
        acct = SimpleNamespace(
            _price=_RefinePrice(),
            _options=ParquetOptionsProvider(str(root), spot_source=lambda s, d: 100.0,
                                            risk_free_rate=0.045, spot_scope="refine"),
            _equity_at=lambda dt: 100_000.0)
        assert _build_refine_drawdown_fn(acct, _REFINE_CFG) is not None
    finally:
        clear_worker_parquet_options_cache()


def test_both_backends_expose_the_named_delta_at_entry_seam():
    """The seam is a METHOD both readers implement, checked by name and arity so a rename on
    one side cannot re-open the silent-skip hole."""
    import inspect
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider

    a = inspect.signature(HistoricalOptionsProvider.delta_at_entry)
    b = inspect.signature(ParquetOptionsProvider.delta_at_entry)
    assert str(a) == str(b), f"sqlite {a} != parquet {b}"
    assert list(a.parameters) == ["self", "underlying", "occ_symbol", "when"]


def test_a_reader_without_delta_at_entry_is_a_WARNING_not_silence(monkeypatch):
    """SILENCE WAS THE DEFECT. Skipping is allowed; skipping without saying so is not --
    ``max_drawdown`` (and every metric divided by it) is then not comparable across runs."""
    from types import SimpleNamespace
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    acct = SimpleNamespace(_price=_RefinePrice(), _options=SimpleNamespace(get_bar=lambda *a: None))
    with _captured_warnings() as msgs:
        assert _build_refine_drawdown_fn(acct, _REFINE_CFG) is None
    assert any("delta_at_entry" in m and "SKIPPED" in m for m in msgs), msgs


def test_an_equity_only_account_skips_QUIETLY(monkeypatch):
    """The other half: an equity run has no options at all, which is normal and must not
    spam a warning on every single backtest."""
    from types import SimpleNamespace
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    acct = SimpleNamespace(_price=_RefinePrice(), _options=None)
    with _captured_warnings() as msgs:
        assert _build_refine_drawdown_fn(acct, _REFINE_CFG) is None
    assert not [m for m in msgs if "delta_at_entry" in m], msgs


def test_the_refinement_calls_the_readers_delta_at_entry(monkeypatch):
    """The closure must route to the READER's method, with (underlying, contract, when)."""
    from types import SimpleNamespace
    from app.services.backtest import intraday_drawdown
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    seen = {}
    calls = []
    acct = SimpleNamespace(
        _price=_RefinePrice(),
        _options=SimpleNamespace(
            delta_at_entry=lambda u, c, w: (calls.append((u, c, w)), 0.5)[1]),
        _equity_at=lambda dt: 100_000.0)

    monkeypatch.setattr(intraday_drawdown, "refine_max_drawdown",
                        lambda trades, md, **kw: (seen.update(kw), md)[1])
    fn = _build_refine_drawdown_fn(acct, _REFINE_CFG)
    assert fn is not None
    fn([], -2.0)
    assert seen["delta_at_entry"]("AAPL", "AAPL240315C00180000", _ENTRY) == 0.5
    assert calls == [("AAPL", "AAPL240315C00180000", _ENTRY)]


def test_a_reader_that_raises_does_not_fail_the_finished_run(monkeypatch):
    """A refinement is a refinement: a broken delta lookup drops that trade from the estimate,
    it does not throw away a completed backtest."""
    from types import SimpleNamespace
    from app.services.backtest import intraday_drawdown
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    seen = {}

    def _boom(*a, **k):
        raise RuntimeError("chain history unreadable")

    acct = SimpleNamespace(_price=_RefinePrice(),
                           _options=SimpleNamespace(delta_at_entry=_boom),
                           _equity_at=lambda dt: 100_000.0)
    monkeypatch.setattr(intraday_drawdown, "refine_max_drawdown",
                        lambda trades, md, **kw: (seen.update(kw), md)[1])
    _build_refine_drawdown_fn(acct, _REFINE_CFG)([], -2.0)
    assert seen["delta_at_entry"]("AAPL", "X", _ENTRY) is None


def test_refine_never_improves_on_the_daily_figure():
    """Even a trade whose estimated worst point is BETTER than the daily figure must not move
    max_drawdown towards 0 -- refinement can only make drawdown worse, never better (the daily
    curve is authoritative for anything it already captured)."""
    trade = _make_trade(pnl=-1000.0)  # daily curve already recorded a big loss
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 99.0, "High": 101.0}],  # mild-only dip
    )
    assert refined == pytest.approx(-2.0)


def test_refine_does_not_accumulate_additively_across_many_flagged_trades():
    """Regression: a 251-trade live run's refined drawdown reached -101.71% (worse than a total
    wipeout) while the raw equity curve's real peak-to-trough was only -41%. Root cause: each
    flagged trade's candidate was computed against the RUNNING (already-adjusted) `refined`
    value instead of the original `max_drawdown`, so N unrelated trades on different dates --
    whose hypothetical worst cases are mutually exclusive, they can't all have hit the SAME
    equity trough at once -- stacked additively without bound. 20 trades each contributing a
    modest -3pp individually must NOT sum to -60pp; the result must be bounded by the worst
    SINGLE trade's contribution."""
    trades = [_make_trade(entry_time=datetime(2024, 4, i + 1), exit_time=datetime(2024, 4, i + 2))
              for i in range(1, 21)]  # 20 separate, non-overlapping flagged trades
    refined = refine_max_drawdown(
        trades,
        max_drawdown=-2.0,
        equity_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # Each trade individually implies candidate_dd = -2.0 + (-600/20000*100) = -5.0 (same math as
    # test_refine_worsens_drawdown_for_flagged_trade_with_hidden_dip). With 20 trades, the OLD
    # (buggy) additive-accumulation behavior would reach roughly -2.0 + 20*-3.0 = -62.0. The
    # fixed behavior must land at exactly the worst SINGLE trade's candidate, -5.0.
    assert refined == pytest.approx(-5.0)


def test_refine_is_hard_floored_at_negative_100_percent():
    """Drawdown relative to total equity cannot mathematically exceed -100% (that would mean
    losing more than the entire equity), even as a hypothetical worst-case estimate. A trade
    whose implied extra loss would push the candidate past that must be clamped."""
    trade = _make_trade()
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 100.0,  # tiny equity relative to the loss -> candidate blows past -100%
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # extra_loss = -600 against equity=100 -> candidate_dd = -2.0 + (-600/100*100) = -602.0,
    # must be floored to exactly -100.0.
    assert refined == pytest.approx(-100.0)
