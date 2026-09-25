"""``intraday_drawdown`` -- post-hoc drawdown refinement for options trades whose realised P&L
came from DAILY option-premium bars only. Pure-function tests: all data access is faked via
plain dicts/callables, no real cache files or account objects needed.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_intraday_drawdown.py -v
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

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
        peak_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,  # flags via bars_held=1 anyway
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # implied worst premium = 5.0 + 0.5*(90-100) = 0.0 (floored); worst_pnl = (0-5)*1*100 = -500.
    # dip = (20000 - 500) / 20000 - 1 = -2.5% (Task 3: measured from the peak; the realised
    # +100 plays no part -- the old formula subtracted it and reported -5.0).
    assert refined == pytest.approx(-2.5)
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
        peak_at=lambda dt: 20_000.0,
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
        peak_at=lambda dt: 20_000.0,
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
        peak_at=lambda dt: 20_000.0,
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
        peak_at=lambda dt: 20_000.0,
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
        peak_at=lambda dt: 20_000.0,
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
        peak_at=lambda dt: 20_000.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # Each trade individually implies a -2.5% dip (same math as
    # test_refine_worsens_drawdown_for_flagged_trade_with_hidden_dip). Stacking 20 of them would
    # reach -50%; the result must land at exactly the worst SINGLE trade's dip, -2.5.
    assert refined == pytest.approx(-2.5)


def test_refine_is_hard_floored_at_negative_100_percent():
    """Drawdown relative to total equity cannot mathematically exceed -100% (that would mean
    losing more than the entire equity), even as a hypothetical worst-case estimate. A trade
    whose implied extra loss would push the candidate past that must be clamped."""
    trade = _make_trade()
    refined = refine_max_drawdown(
        [trade],
        max_drawdown=-2.0,
        equity_at=lambda dt: 100.0,  # tiny equity relative to the loss -> candidate blows past -100%
        peak_at=lambda dt: 100.0,
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    # worst_pnl = -500 against equity = peak = 100 -> dip = (100 - 500) / 100 - 1 = -500%,
    # must be floored to exactly -100.0.
    assert refined == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# Task 3 (plan 2026-09-24-option-bt-engine-bug-fixes): the refinement measures a DIP from the
# running equity peak, not "worst P&L minus realised P&L". The old formula counted a winning
# trade's realised GAIN as drawdown: +$51,048 realised against a -$500 intraday worst read as a
# -$51,548 "extra loss" -> -462% -> floored to -100%, the bust sentinel on a profitable run.
# ---------------------------------------------------------------------------

def _refine_kw(**overrides):
    """The injected data access every Task 3 test shares: one flagged (bars_held=1) trade whose
    5m window floors the premium to 0 (worst_pnl = -entry_premium * size * 100)."""
    kw = dict(
        daily_bar_low=lambda sym, dt: 100.0,
        prior_daily_bar_low=lambda sym, dt: 105.0,
        delta_at_entry=lambda underlying, contract, dt: 0.5,
        underlying_price_at=lambda sym, dt: 100.0,
        bars_5m_between=lambda sym, entry, exit_: [{"Low": 90.0, "High": 101.0}],
    )
    kw.update(overrides)
    return kw


def test_a_WINNING_trade_refines_to_its_dip_not_to_minus_100():
    """Realised +$51,048, worst intraday -$500, entry equity $11,156, running peak $12,000.
    The dip is (11156 - 500) / 12000 - 1 = -11.2%; the old formula said -100%."""
    trade = _make_trade(pnl=51_048.0)  # entry 5.0 x 1 lot -> worst_pnl = -500
    refined = refine_max_drawdown(
        [trade], max_drawdown=-5.0,
        equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0, **_refine_kw())
    assert refined == pytest.approx((11_156.0 - 500.0) / 12_000.0 * 100.0 - 100.0)  # -11.2
    assert refined == pytest.approx(-11.2)


def test_a_winning_trade_whose_dip_is_shallower_than_the_daily_max_changes_nothing():
    """Same trade against the O_LC TOP1's daily figure: -11.2% is inside -25.49%, so the
    daily curve already holds the worst point and the refinement must leave it alone."""
    trade = _make_trade(pnl=51_048.0)
    refined = refine_max_drawdown(
        [trade], max_drawdown=-25.49,
        equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0, **_refine_kw())
    assert refined == pytest.approx(-25.49)


def test_a_LOSING_trade_with_a_worse_intraday_low_deepens_the_drawdown_by_the_dip():
    """Realised -$1,000, intraday worst -$2,000 (4 lots floored to 0 from 5.0), entry equity
    $18,000 under a $20,000 peak: dip = (18000 - 2000) / 20000 - 1 = -20%. The realised loss
    plays no part -- the dip is measured from the peak, not relative to what was realised
    (the old formula gave -2 + (-1000/18000*100) = -7.56%)."""
    trade = _make_trade(pnl=-1_000.0, size=4.0)
    refined = refine_max_drawdown(
        [trade], max_drawdown=-2.0,
        equity_at=lambda dt: 18_000.0, peak_at=lambda dt: 20_000.0, **_refine_kw())
    assert refined == pytest.approx(-20.0)


def test_no_flagged_trades_returns_the_input_unchanged():
    unflagged = _make_trade(bars_held=5)
    kw = _refine_kw(daily_bar_low=lambda sym, dt: 101.0,        # exit low NOT below prior
                    prior_daily_bar_low=lambda sym, dt: 100.0)
    for trades in ([], [unflagged]):
        refined = refine_max_drawdown(
            trades, max_drawdown=-7.25,
            equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0, **kw)
        assert refined == -7.25


def test_a_window_that_never_goes_below_entry_changes_nothing():
    """worst_pnl >= 0: the trade never dipped, so equity_at(entry) is itself a point on the
    daily curve and cannot be worse than that curve's max drawdown."""
    trade = _make_trade(pnl=51_048.0)
    refined = refine_max_drawdown(
        [trade], max_drawdown=-1.0,
        equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0,
        **_refine_kw(bars_5m_between=lambda sym, entry, exit_: [{"Low": 102.0, "High": 105.0}]))
    assert refined == pytest.approx(-1.0)


def test_a_capped_run_measures_the_dip_on_the_cap_like_its_daily_curve():
    """With an equity cap the daily drawdown is (P&L - peak P&L) / cap
    (``equity_cap.capped_drawdown_curve``), so the dip must use the same fixed denominator or
    the refinement would compare a peak-relative figure against a cap-relative one:
    (11156 - 500 - 12000) / 20000 = -6.72%, not the uncapped -11.2%."""
    trade = _make_trade(pnl=51_048.0)
    refined = refine_max_drawdown(
        [trade], max_drawdown=-5.0,
        equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0,
        drawdown_base=20_000.0, **_refine_kw())
    assert refined == pytest.approx(-6.72)


def test_the_peak_is_never_below_the_equity_it_is_read_with():
    """A running peak includes the point it is read at. A peak callable answering below the
    entry equity (an inconsistent source) must not turn a dip into a positive 'drawdown' or a
    shallower one: the dip is measured from max(peak, equity)."""
    trade = _make_trade(pnl=0.0)
    refined = refine_max_drawdown(
        [trade], max_drawdown=0.0,
        equity_at=lambda dt: 20_000.0, peak_at=lambda dt: 10_000.0, **_refine_kw())
    assert refined == pytest.approx(-2.5)  # (20000 - 500) / 20000 - 1


def test_a_non_positive_drawdown_base_is_refused_loudly():
    with pytest.raises(ValueError, match="drawdown_base"):
        refine_max_drawdown(
            [_make_trade()], max_drawdown=-1.0,
            equity_at=lambda dt: 1.0, peak_at=lambda dt: 1.0, drawdown_base=0.0,
            **_refine_kw())


def test_the_wiring_builds_peak_at_as_the_running_peak_of_the_same_curve(monkeypatch):
    """``results._build_refine_drawdown_fn`` must hand the refinement a ``peak_at`` read from
    the account's recorded equity curve with ``_equity_at``'s own at/just-before lookup, so the
    peak and the equity are read at the same point of the same series -- and it must forward a
    configured cap as the dip's denominator."""
    from types import SimpleNamespace
    from app.services.backtest import intraday_drawdown
    from app.services.backtest.results import _build_refine_drawdown_fn

    _no_fmp(monkeypatch)
    # UTC-AWARE, like the real account: snapshot dates and the entry times ``_refine`` parses
    # back from ISO strings both carry +00:00, and the bisect compares them directly.
    utc = timezone.utc
    snaps = [{"date": datetime(2024, 1, 2, tzinfo=utc), "net_liquidating_value": 10_000.0},
             {"date": datetime(2024, 1, 3, tzinfo=utc), "net_liquidating_value": 12_000.0},
             {"date": datetime(2024, 1, 4, tzinfo=utc), "net_liquidating_value": 11_000.0},
             {"date": datetime(2024, 1, 5, tzinfo=utc), "net_liquidating_value": 12_500.0}]
    acct = SimpleNamespace(
        _price=_RefinePrice(),
        _options=SimpleNamespace(delta_at_entry=lambda u, c, w: 0.5),
        _equity_at=lambda dt: 100_000.0,
        get_balance_history=lambda: list(snaps))
    seen = {}
    monkeypatch.setattr(intraday_drawdown, "refine_max_drawdown",
                        lambda trades, md, **kw: (seen.update(kw), md)[1])

    _build_refine_drawdown_fn(acct, _REFINE_CFG)([], -2.0)
    peak_at = seen["peak_at"]
    assert peak_at(datetime(2024, 1, 1, tzinfo=utc)) == 10_000.0          # pre-curve: first point
    assert peak_at(datetime(2024, 1, 2, 15, 45, tzinfo=utc)) == 10_000.0
    assert peak_at(datetime(2024, 1, 4, 15, 45, tzinfo=utc)) == 12_000.0  # held through the dip
    assert peak_at(datetime(2024, 1, 5, tzinfo=utc)) == 12_500.0          # at the snapshot: in
    assert seen["drawdown_base"] is None

    _build_refine_drawdown_fn(acct, _REFINE_CFG, equity_cap=20_000.0)([], -2.0)
    assert seen["drawdown_base"] == 20_000.0


# ---------------------------------------------------------------------------
# Task 3 review follow-ups: only the DIPS are floored, skips are counted out loud, and the
# result records what the refinement did.
# ---------------------------------------------------------------------------

def test_a_capped_daily_drawdown_below_minus_100_comes_back_UNCHANGED():
    """A $30k loss on a $20k cap is a -150% daily drawdown. The old final ``max(refined, -100)``
    clamped the INPUT, so this came back -100 with no flagged trade at all -- improving a
    figure the refinement may only worsen."""
    unflagged = _make_trade(bars_held=5)
    kw = _refine_kw(daily_bar_low=lambda sym, dt: 101.0, prior_daily_bar_low=lambda sym, dt: 100.0)
    for trades in ([], [unflagged]):
        assert refine_max_drawdown(
            trades, max_drawdown=-150.0, equity_at=lambda dt: 11_156.0,
            peak_at=lambda dt: 12_000.0, drawdown_base=20_000.0, **kw) == -150.0


def test_a_capped_daily_drawdown_below_minus_100_survives_a_flagged_dip():
    """A flagged trade whose dip (-6.72% on the cap) is shallower than the -150% daily figure
    leaves it alone."""
    refined = refine_max_drawdown(
        [_make_trade(pnl=51_048.0)], max_drawdown=-150.0,
        equity_at=lambda dt: 11_156.0, peak_at=lambda dt: 12_000.0,
        drawdown_base=20_000.0, **_refine_kw())
    assert refined == -150.0


def test_a_dip_past_minus_100_is_floored_on_a_capped_run_too():
    """The floor applies to the dip: -$500 worst on $100 of equity under a $100 cap is -500%,
    reported as -100 because the daily figure (-50) is shallower."""
    refined = refine_max_drawdown(
        [_make_trade()], max_drawdown=-50.0,
        equity_at=lambda dt: 100.0, peak_at=lambda dt: 100.0,
        drawdown_base=100.0, **_refine_kw())
    assert refined == pytest.approx(-100.0)


def test_a_trade_dropped_by_an_exception_is_a_WARNING_with_its_count():
    def _boom(*a, **k):
        raise RuntimeError("cache unavailable")

    with _captured_warnings() as msgs:
        refined = refine_max_drawdown(
            [_make_trade()], max_drawdown=-2.0,
            equity_at=lambda dt: 20_000.0, peak_at=lambda dt: 20_000.0,
            **_refine_kw(daily_bar_low=_boom))
    assert refined == pytest.approx(-2.0)
    assert any("1 trade(s) skipped on an exception" in m for m in msgs), msgs


def test_a_dip_dropped_for_a_non_positive_base_is_a_WARNING_with_its_count():
    """Negative equity makes the peak (the base) non-positive: the dip has no meaningful
    denominator and is dropped -- counted, not silently."""
    with _captured_warnings() as msgs:
        refined = refine_max_drawdown(
            [_make_trade()], max_drawdown=-2.0,
            equity_at=lambda dt: -100.0, peak_at=lambda dt: -200.0, **_refine_kw())
    assert refined == pytest.approx(-2.0)
    assert any("1 on a non-positive drawdown base" in m for m in msgs), msgs


def test_a_clean_refinement_logs_no_warning():
    with _captured_warnings() as msgs:
        refine_max_drawdown(
            [_make_trade()], max_drawdown=-2.0,
            equity_at=lambda dt: 20_000.0, peak_at=lambda dt: 20_000.0, **_refine_kw())
    assert not [m for m in msgs if "intraday drawdown refinement" in m], msgs


# --- build_results level: the cap goes in, the refinement status comes out ------------------

class _CurveAccount:
    """``build_results`` reads only these two methods on a non-option account."""

    def __init__(self, snaps):
        self._snaps = snaps

    def get_balance_history(self):
        return self._snaps

    def get_filled_trades(self):
        return []


_CURVE = [{"date": datetime(2024, 1, d, tzinfo=timezone.utc), "net_liquidating_value": v,
           "cash_balance": v, "equity_value": 0.0}
          for d, v in ((2, 20_000.0), (3, 22_000.0), (4, 19_800.0))]


def _cfg(cap):
    return {"initial_capital": 20_000.0,
            "account_settings": {"starting_cash": 20_000.0, "commission_per_trade": 0.0,
                                 "slippage_bps": 0.0, "fill_model": "next_bar_open",
                                 "equity_cap": cap}}


@pytest.mark.parametrize("cap", [20_000.0, None])
def test_build_results_passes_the_equity_cap_to_the_refinement(monkeypatch, cap):
    from app.services.backtest import results as R

    seen = {}

    def _spy(account, config, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(R, "_build_refine_drawdown_fn", _spy)
    R.build_results(_CurveAccount(_CURVE), _cfg(cap))
    assert seen == {"equity_cap": cap}


def test_an_equity_run_carries_no_refinement_status_key():
    """No refinement (equity-only run) -> the key is ABSENT, so a stored stock backtest re-runs
    with exactly its old key set (user acceptance gate 2026-09-25: byte-identical re-runs)."""
    from app.services.backtest.results import build_results

    assert "max_drawdown_refinement" not in build_results(_CurveAccount(_CURVE), _cfg(None))


def test_refinement_status_is_applied_when_it_ran(monkeypatch):
    from app.services.backtest import results as R

    monkeypatch.setattr(R, "_build_refine_drawdown_fn",
                        lambda account, config, **kw: (lambda trades, md: md - 1.0))
    out = R.build_results(_CurveAccount(_CURVE), _cfg(None))
    assert out["max_drawdown_refinement"] == "applied"
    assert out["max_drawdown"] == pytest.approx(out["max_drawdown_daily"] - 1.0)


def test_a_refinement_that_RAISES_is_a_warning_and_is_recorded(monkeypatch):
    """No silent failure: the daily figure stands, but the log says so at WARNING and the
    result records that its max_drawdown is the unrefined one, and why."""
    from app.services.backtest import results as R

    def _raises(trades, md):
        raise RuntimeError("5m cache unreadable")

    monkeypatch.setattr(R, "_build_refine_drawdown_fn", lambda account, config, **kw: _raises)
    with _captured_warnings() as msgs:
        out = R.build_results(_CurveAccount(_CURVE), _cfg(None))
    assert out["max_drawdown_refinement"] == "failed:RuntimeError"
    assert out["max_drawdown"] == out["max_drawdown_daily"] == pytest.approx(-10.0)
    assert any("refinement FAILED" in m and "RuntimeError" in m for m in msgs), msgs
