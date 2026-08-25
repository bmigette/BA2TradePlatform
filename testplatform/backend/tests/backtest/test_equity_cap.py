"""The optional fixed-notional equity cap. Pure arithmetic, no engine, no clock."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.backtest.equity_cap import (
    EquityCapError, capped_drawdown_curve, deployed_equity, scoring_curve,
    validate_equity_cap,
)


@pytest.fixture
def caplog_free_logger():
    """Named only to make the no-caplog rule visible at the call site."""
    return None


# ---------------------------------------------------------------------------
# Task 1 -- validation
# ---------------------------------------------------------------------------
def test_none_means_the_feature_is_off():
    assert validate_equity_cap(None) is None


def test_a_positive_cap_is_returned_as_a_float():
    assert validate_equity_cap(20_000) == 20_000.0
    assert isinstance(validate_equity_cap(20_000), float)


@pytest.mark.parametrize("bad", [0, 0.0, -1, -20_000.0])
def test_a_non_positive_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be greater than zero"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", ["20000", "", [], {}, object()])
def test_a_non_numeric_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_cap_is_refused(bad):
    with pytest.raises(EquityCapError, match="must be finite"):
        validate_equity_cap(bad)


def test_a_bool_is_not_a_number():
    """True == 1 in Python. A boolean reaching a money field is a caller bug, not a $1 cap."""
    with pytest.raises(EquityCapError, match="must be a number"):
        validate_equity_cap(True)


def test_a_cap_above_the_initial_capital_is_allowed_and_says_so(caplog_free_logger):
    """It cannot bind YET, but the account may grow into it. Not an error."""
    msgs = []
    assert validate_equity_cap(50_000, initial_capital=20_000, log=msgs.append) == 50_000.0
    assert any("cannot bind" in m and "50,000" in m and "20,000" in m for m in msgs), msgs


def test_a_cap_at_or_below_the_initial_capital_logs_nothing():
    msgs = []
    validate_equity_cap(20_000, initial_capital=20_000, log=msgs.append)
    validate_equity_cap(5_000, initial_capital=20_000, log=msgs.append)
    assert msgs == []


# ---------------------------------------------------------------------------
# Task 2 -- deployed equity
# ---------------------------------------------------------------------------
def test_with_no_cap_the_real_equity_passes_through():
    assert deployed_equity(37_412.55, cap=None) == 37_412.55


def test_above_the_cap_only_the_cap_is_deployed():
    assert deployed_equity(40_000.0, cap=20_000.0) == 20_000.0


def test_below_the_cap_the_real_equity_is_deployed():
    """'except if account value goes below' -- a drawdown genuinely shrinks what can be deployed."""
    assert deployed_equity(15_000.0, cap=20_000.0) == 15_000.0


def test_exactly_at_the_cap():
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0


def test_recovery_climbs_back_to_the_cap_and_stops():
    assert deployed_equity(18_000.0, cap=20_000.0) == 18_000.0
    assert deployed_equity(20_000.0, cap=20_000.0) == 20_000.0
    assert deployed_equity(25_000.0, cap=20_000.0) == 20_000.0


def test_a_wiped_out_account_deploys_nothing_not_the_cap():
    assert deployed_equity(0.0, cap=20_000.0) == 0.0


def test_negative_equity_is_not_raised_to_zero_here():
    """The caller decides what a negative account means; this function does not invent a floor."""
    assert deployed_equity(-500.0, cap=20_000.0) == -500.0


def test_unmeasurable_equity_is_unmeasurable_not_zero():
    """None in means None out. A broker/engine that cannot state equity has not stated zero."""
    assert deployed_equity(None, cap=20_000.0) is None
    assert deployed_equity(None, cap=None) is None


# ---------------------------------------------------------------------------
# Task 3 -- the scoring conversion
# ---------------------------------------------------------------------------
def _pt(y, equity):
    return {"date": datetime(y, 1, 1), "equity": float(equity)}


def test_five_k_a_year_on_twenty_k_reads_twenty_five_percent_every_year():
    """THE headline case. The naive `cap + cumulative P&L` curve would read
    25 / 20 / 16.7 / 14.3 for this identical strategy; that decline is the compounding
    effect this feature exists to remove."""
    real = [_pt(2020, 20_000), _pt(2021, 25_000), _pt(2022, 30_000),
            _pt(2023, 35_000), _pt(2024, 40_000)]
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 25_000.0, 31_250.0, 39_062.5, 48_828.125])


def test_the_curve_keeps_its_dates():
    real = [_pt(2020, 20_000), _pt(2021, 25_000)]
    assert [p["date"] for p in scoring_curve(real, cap=20_000.0)] == \
           [datetime(2020, 1, 1), datetime(2021, 1, 1)]


def test_with_no_cap_the_curve_is_returned_untouched():
    real = [_pt(2020, 20_000), _pt(2021, 25_000)]
    assert scoring_curve(real, cap=None) == real


def test_a_flat_period_is_a_measured_zero_not_a_missing_one():
    real = [_pt(2020, 20_000), _pt(2021, 20_000), _pt(2022, 25_000)]
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 20_000.0, 25_000.0])


def test_a_loss_period_compounds_downward_on_the_fixed_denominator():
    real = [_pt(2020, 20_000), _pt(2021, 18_000)]     # -2,000 = -10% of the 20k cap
    got = [p["equity"] for p in scoring_curve(real, cap=20_000.0)]
    assert got == pytest.approx([20_000.0, 18_000.0])


def test_the_denominator_is_the_cap_not_the_running_equity():
    """A +2,000 period reads +10% whether it happens first or last."""
    early = scoring_curve([_pt(2020, 20_000), _pt(2021, 22_000)], cap=20_000.0)
    late = scoring_curve([_pt(2020, 20_000), _pt(2021, 20_000), _pt(2022, 22_000)],
                         cap=20_000.0)
    first_step = early[1]["equity"] / early[0]["equity"] - 1.0
    last_step = late[2]["equity"] / late[1]["equity"] - 1.0
    assert first_step == pytest.approx(0.10)
    assert last_step == pytest.approx(0.10)


def test_a_single_point_curve_has_no_return_to_compute():
    assert [p["equity"] for p in scoring_curve([_pt(2020, 20_000)], cap=20_000.0)] == [20_000.0]


def test_an_empty_curve_stays_empty():
    assert scoring_curve([], cap=20_000.0) == []


def test_a_two_thousand_drawdown_is_ten_percent_whenever_it_happens():
    """Risk is denominated in the cap too, or dd_guard rewards a late-run strategy by
    arithmetic alone (dd_guard = min(20/max(dd,1), 2.0))."""
    early = [_pt(2020, 20_000), _pt(2021, 18_000), _pt(2022, 20_000)]
    late = [_pt(2020, 20_000), _pt(2021, 40_000), _pt(2022, 38_000)]
    assert min(p["drawdown"] for p in capped_drawdown_curve(early, cap=20_000.0)) \
        == pytest.approx(-10.0)
    assert min(p["drawdown"] for p in capped_drawdown_curve(late, cap=20_000.0)) \
        == pytest.approx(-10.0)


def test_the_capped_drawdown_curve_keeps_its_dates_and_starts_flat():
    pts = capped_drawdown_curve([_pt(2020, 20_000), _pt(2021, 18_000)], cap=20_000.0)
    assert [p["date"] for p in pts] == [datetime(2020, 1, 1), datetime(2021, 1, 1)]
    assert pts[0]["drawdown"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Task 4 -- the account's money surface
# ---------------------------------------------------------------------------
#: A frozen simulated clock. Never "today" — a wall-clock-dependent bar would make the
#: mark-to-market (and therefore every assertion below) drift with the calendar.
_BAR_DAY = datetime(2024, 3, 15)

_ACCT_SEQ = [900]


@pytest.fixture
def capped_account():
    """A REAL ``BacktestAccount`` over a throwaway backtest DB, with a known cash/MTM and an
    optional cap.

    Deliberately the genuine constructor (mirroring ``test_round_trip_trades._acct``) rather
    than a ``__new__`` bypass: the cap is read out of the account config in ``__init__``, so a
    fixture that skips ``__init__`` would leave that read untested.

    ``mtm`` is produced by a REAL ledger position marked against a REAL price bar, not by
    stubbing ``_open_positions_mtm`` — the whole point of ``deployed_equity`` is that it sees
    unrealised marks.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    made = []

    def _make(*, cash, cap, mtm=0.0, symbol="AAPL", mark=100.0):
        _ACCT_SEQ[0] += 1
        account_id = _ACCT_SEQ[0]
        cfg = {
            "starting_cash": float(cash),
            "commission_per_trade": 0.0,
            "slippage_bps": 0.0,
            "fill_model": "next_bar_open",
        }
        if cap is not None:
            cfg["equity_cap"] = float(cap)
        wire_backtest_seams()
        ctx = backtest_trading_db(f"equity-cap-{account_id}")
        ctx.__enter__()
        made.append(ctx)
        seed_account_definition(account_id, cfg)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(symbol, [{"Date": _BAR_DAY, "Open": mark, "High": mark, "Low": mark,
                               "Close": mark, "Volume": 1_000}])
        ps.set_clock(_BAR_DAY)
        acct = BacktestAccount(account_id, ps, cfg)
        wire_backtest_seams().register_account(account_id, acct)
        if mtm:
            acct._update_position(symbol, float(mtm) / mark, mark)
        return acct

    try:
        yield _make
    finally:
        for ctx in reversed(made):
            ctx.__exit__(None, None, None)


def test_with_no_cap_the_account_reports_its_real_money(capped_account):
    acct = capped_account(cash=40_000.0, cap=None)
    assert acct.get_balance() == 40_000.0
    assert acct.get_account_info()["equity"] == 40_000.0
    assert acct.get_account_info()["buying_power"] == 40_000.0


def test_above_the_cap_equity_and_buying_power_are_capped(capped_account):
    acct = capped_account(cash=40_000.0, cap=20_000.0)
    assert acct.get_account_info()["equity"] == 20_000.0
    assert acct.get_account_info()["buying_power"] == 20_000.0
    assert acct.get_balance() == 20_000.0


def test_below_the_cap_the_real_figures_are_reported(capped_account):
    acct = capped_account(cash=15_000.0, cap=20_000.0)
    assert acct.get_account_info()["equity"] == 15_000.0
    assert acct.get_balance() == 15_000.0


def test_cash_is_never_reported_above_what_is_actually_held(capped_account):
    """Cap 20k, equity 40k, but only 5k in cash because the rest is invested. You cannot spend
    money you do not have, so the cap must not RAISE the cash figure."""
    acct = capped_account(cash=5_000.0, cap=20_000.0, mtm=35_000.0)
    assert acct.equity() == pytest.approx(40_000.0)
    assert acct.get_balance() == 5_000.0


def test_buying_power_never_goes_negative(capped_account):
    acct = capped_account(cash=-500.0, cap=20_000.0)
    assert acct.get_account_info()["buying_power"] == 0.0


def test_the_recorded_equity_curve_is_NEVER_capped(capped_account):
    """The cap must not reach snapshot_equity or the run's own history becomes
    unreconstructable -- and the scoring curve would then report zero P&L for every period
    spent above the cap."""
    acct = capped_account(cash=40_000.0, cap=20_000.0)
    snap = acct.snapshot_equity(datetime(2024, 3, 15, 16, 0))
    assert snap["net_liquidating_value"] == 40_000.0
    assert snap["cash_balance"] == 40_000.0


def test_the_account_snapshot_seam_inherits_the_cap(capped_account):
    """``_validate_position_size_limits`` reads equity through ``get_account_snapshot()``, not
    ``get_account_info()``. The base implementation derives it from ``get_account_info``, so
    capping the one seam must be enough -- asserted rather than assumed."""
    acct = capped_account(cash=40_000.0, cap=20_000.0)
    snapshot = acct.get_account_snapshot()
    assert snapshot.equity == 20_000.0
    assert snapshot.buying_power == 20_000.0


def test_an_uncapped_account_has_the_cap_attribute_set_to_None(capped_account):
    """Off means None, never 0.0 -- a 0.0 cap would make every position unaffordable."""
    acct = capped_account(cash=40_000.0, cap=None)
    assert acct._equity_cap is None
    assert acct.deployed_equity() == 40_000.0
