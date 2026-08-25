"""The optional fixed-notional equity cap. Pure arithmetic, no engine, no clock."""
from __future__ import annotations

import os
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


# ---------------------------------------------------------------------------
# Task 5 -- the scoring conversion inside build_results
# ---------------------------------------------------------------------------
class _SnapshotAccount:
    """The account stub ``build_results`` actually needs.

    Copied from ``test_results_metrics._AccountStub`` (the real harness): ``build_results``
    calls exactly ``get_balance_history()`` and ``get_filled_trades()``, and reads
    ``_wiped_out`` via ``getattr``. It has no ``_price``/``_options``, so
    ``_build_refine_drawdown_fn`` returns None and the intraday refinement is skipped.
    """

    def __init__(self, snapshots, trades=()):
        self._snaps = list(snapshots)
        self._trades = list(trades)

    def get_balance_history(self):
        return self._snaps

    def get_filled_trades(self):
        return self._trades


def _snapshots(points):
    """``[(datetime, net_liquidating_value), ...]`` -> the snapshot shape the account writes."""
    return [{"date": d, "net_liquidating_value": float(nlv),
             "cash_balance": float(nlv), "equity_value": 0.0} for d, nlv in points]


def _results_config(*, initial_capital, equity_cap):
    """The ``build_results`` config shape.

    ``test_results_metrics`` uses the bare ``{"initial_capital": ...}``; the cap lives under
    ``account_settings`` (where ``daily_backtest_handler._build_config`` puts it), so that
    sub-dict is spelled out here rather than guessed.
    """
    return {
        "initial_capital": float(initial_capital),
        "account_settings": {
            "starting_cash": float(initial_capital),
            "commission_per_trade": 0.0,
            "slippage_bps": 0.0,
            "fill_model": "next_bar_open",
            "equity_cap": equity_cap,
        },
    }


_FIVE_K_A_YEAR = [
    (datetime(2020, 1, 1), 20_000.0), (datetime(2021, 1, 1), 25_000.0),
    (datetime(2022, 1, 1), 30_000.0), (datetime(2023, 1, 1), 35_000.0),
    (datetime(2024, 1, 1), 40_000.0),
]


def test_build_results_scores_a_capped_run_on_the_fixed_denominator():
    """$5k a year on a $20k cap: 25% a year, CAGR 25%, total return 144%."""
    from app.services.backtest import results as R

    account = _SnapshotAccount(_snapshots(_FIVE_K_A_YEAR))
    out = R.build_results(account, _results_config(initial_capital=20_000.0,
                                                   equity_cap=20_000.0))
    assert out["equity_curve"][-1]["equity"] == pytest.approx(48_828.125)
    assert out["annualized_return"] == pytest.approx(25.0, abs=0.05)
    assert out["total_return"] == pytest.approx(144.14, abs=0.05)


def test_the_same_run_uncapped_scores_on_the_real_curve():
    from app.services.backtest import results as R

    account = _SnapshotAccount(_snapshots([
        (datetime(2020, 1, 1), 20_000.0), (datetime(2024, 1, 1), 40_000.0),
    ]))
    out = R.build_results(account, _results_config(initial_capital=20_000.0, equity_cap=None))
    assert out["equity_curve"][-1]["equity"] == pytest.approx(40_000.0)
    assert out["total_return"] == pytest.approx(100.0, abs=0.05)


def test_the_uncapped_run_is_the_compounding_one_it_scores_worse_each_year():
    """The defect the feature removes, stated positively: the SAME $5k/yr strategy scored
    without a cap shows a FALLING annual return, so a flat CAGR would read as improvement."""
    from app.services.backtest import results as R

    account = _SnapshotAccount(_snapshots(_FIVE_K_A_YEAR))
    out = R.build_results(account, _results_config(initial_capital=20_000.0, equity_cap=None))
    assert out["equity_curve"][-1]["equity"] == pytest.approx(40_000.0)
    assert out["total_return"] == pytest.approx(100.0, abs=0.05)
    assert out["annualized_return"] == pytest.approx(18.92, abs=0.05)   # NOT 25


def test_a_capped_runs_drawdown_is_denominated_in_the_cap():
    from app.services.backtest import results as R

    account = _SnapshotAccount(_snapshots([
        (datetime(2020, 1, 1), 20_000.0), (datetime(2021, 1, 1), 40_000.0),
        (datetime(2022, 1, 1), 38_000.0),
    ]))
    out = R.build_results(account, _results_config(initial_capital=20_000.0,
                                                   equity_cap=20_000.0))
    assert out["max_drawdown"] == pytest.approx(-10.0, abs=0.01)


def test_the_capped_drawdown_is_measured_on_the_REAL_curve_not_the_synthetic_one():
    """Order guard for results.build_results: ``capped_drawdown_curve`` takes the REAL curve
    and must run BEFORE ``equity_curve`` is reassigned. Run on the synthetic curve the same
    dip reads a plausible, smaller number -- which is exactly what makes the swap invisible.

    20k -> 40k -> 38k. Real cumulative P&L peaks at +20,000 and falls to +18,000: -2,000, i.e.
    -10% of the 20k cap. The SYNTHETIC curve is 20,000 -> 40,000 -> 20,000*2*0.9 = 36,000, a
    -4,000 dip from a 40,000 peak -- and cap-denominated that reads -20%, while the running-peak
    formula reads -10.0% too. So assert the whole curve, where the two differ point by point.
    """
    from app.services.backtest import results as R
    from app.services.backtest.equity_cap import capped_drawdown_curve

    points = [(datetime(2020, 1, 1), 20_000.0), (datetime(2021, 1, 1), 40_000.0),
              (datetime(2022, 1, 1), 38_000.0), (datetime(2023, 1, 1), 39_000.0)]
    out = R.build_results(_SnapshotAccount(_snapshots(points)),
                          _results_config(initial_capital=20_000.0, equity_cap=20_000.0))
    got = [round(p["drawdown"], 6) for p in out["drawdown_curve"]]

    real_curve = [{"date": d, "equity": e} for d, e in points]
    expected = [round(p["drawdown"], 6)
                for p in capped_drawdown_curve(real_curve, cap=20_000.0)]
    synthetic = [round(p["drawdown"], 6)
                 for p in capped_drawdown_curve(
                     [{"date": p["date"], "equity": p["equity"]}
                      for p in out["equity_curve"]], cap=20_000.0)]
    assert expected != synthetic, "the fixture no longer distinguishes the two curves"
    assert got == expected, f"drawdown was measured on the SYNTHETIC curve: {got} vs {expected}"


def test_an_absent_account_settings_block_is_not_a_configured_cap():
    """``test_results_metrics``/``test_zero_coercion_defects`` call build_results with a bare
    ``{"initial_capital": ...}``. That is not a cap of zero; it is no cap."""
    from app.services.backtest import results as R

    out = R.build_results(_SnapshotAccount(_snapshots(_FIVE_K_A_YEAR)),
                          {"initial_capital": 20_000.0})
    assert out["equity_curve"][-1]["equity"] == pytest.approx(40_000.0)


def test_a_bad_cap_in_the_results_config_is_refused_not_ignored():
    from app.services.backtest import results as R

    with pytest.raises(EquityCapError, match="greater than zero"):
        R.build_results(_SnapshotAccount(_snapshots(_FIVE_K_A_YEAR)),
                        _results_config(initial_capital=20_000.0, equity_cap=0.0))


# ---------------------------------------------------------------------------
# Task 6 -- config plumbing (daily_backtest_handler._build_config)
# ---------------------------------------------------------------------------
def _handler_payload(**over):
    """The minimum ``_build_config`` accepts.

    The key set is copied verbatim from ``test_daily_backtest_handler._payload`` (the module
    that already pins ``_build_config``'s contract) rather than guessed from the source.
    """
    p = {
        "backtest_id": 1,
        "name": "equity-cap-test",
        "enabled_instruments": ["AAPL"],
        "experts": ["FMPEarningsDrift"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_capital": 20_000.0,
        "commission": 1.0,
        "slippage": 0.0,
        "fill_model": "next_bar_open",
        "seed": 42,
    }
    p.update(over)
    return p


def test_the_config_carries_the_cap_into_account_settings():
    from app.services.backtest.daily_backtest_handler import _build_config

    cfg = _build_config(_handler_payload(equity_cap=20_000.0))
    assert cfg["account_settings"]["equity_cap"] == 20_000.0


def test_an_absent_cap_is_None_not_zero():
    from app.services.backtest.daily_backtest_handler import _build_config

    cfg = _build_config(_handler_payload())
    assert cfg["account_settings"]["equity_cap"] is None


def test_a_bad_cap_is_refused_at_CONFIG_time_not_mid_run():
    from app.services.backtest.daily_backtest_handler import _build_config

    with pytest.raises(EquityCapError, match="greater than zero"):
        _build_config(_handler_payload(equity_cap=0))


def test_a_cap_above_the_initial_capital_builds_a_config_and_logs():
    """Not an error: the account may grow into it. The INFO line is the whole point of the
    branch, so it is asserted here on the handler's real logger."""
    from app.services.backtest import daily_backtest_handler as H

    seen = []
    original = H.logger.info
    H.logger.info = lambda msg, *a, **k: (seen.append(str(msg)), original(msg, *a, **k))[1]
    try:
        cfg = H._build_config(_handler_payload(equity_cap=50_000.0))
    finally:
        H.logger.info = original
    assert cfg["account_settings"]["equity_cap"] == 50_000.0
    assert any("cannot bind" in m for m in seen), seen


def test_the_cap_is_normalised_to_a_float_by_the_config():
    from app.services.backtest.daily_backtest_handler import _build_config

    cfg = _build_config(_handler_payload(equity_cap=15_000))
    assert isinstance(cfg["account_settings"]["equity_cap"], float)


# ---------------------------------------------------------------------------
# Task 8 -- live safety
# ---------------------------------------------------------------------------
_CAP_ATTRS = ("deployed_equity", "_equity_cap")


def _exposed_cap_attrs(cls):
    """Which equity-cap attributes ``cls`` exposes. Factored out so the guard's DETECTION can
    itself be tested (see the meta-test below) -- an assertion nobody has watched fail is not
    a guard."""
    names = dir(cls)
    return [a for a in _CAP_ATTRS if a in names]


def test_no_LIVE_account_class_can_reach_the_equity_cap():
    """This is a backtest analysis tool. A live account must have no code path to it --
    asserted rather than assumed, because 'we only call it from the backtest' is exactly the
    kind of claim that stops being true."""
    from ba2_common.core.interfaces.AccountInterface import AccountInterface
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
    from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount

    for cls in (AccountInterface, ReadOnlyAccountInterface, AlpacaAccount, IBKRAccount,
                TastyTradeAccount):
        assert _exposed_cap_attrs(cls) == [], \
            f"{cls.__name__} exposes {_exposed_cap_attrs(cls)}"


def test_the_live_safety_guard_actually_detects_an_exposed_cap():
    """Meta-test: prove the guard bites. Without this, ``_exposed_cap_attrs`` could be
    silently broken (wrong attribute names, a typo in ``dir``) and the guard above would pass
    for every class forever."""
    class _LeakyAccount:
        _equity_cap = None

        def deployed_equity(self):
            return 0.0

    assert sorted(_exposed_cap_attrs(_LeakyAccount)) == ["_equity_cap", "deployed_equity"]


def test_the_backtest_account_is_the_ONLY_class_that_has_the_cap():
    """The positive half: BacktestAccount does expose it (or Task 4 would be dead code), and it
    is a BacktestAccount-only addition -- not something inherited down from AccountInterface."""
    from app.services.backtest.backtest_account import BacktestAccount

    assert sorted(_exposed_cap_attrs(BacktestAccount)) == ["deployed_equity"]
    assert "deployed_equity" in vars(BacktestAccount), \
        "deployed_equity is inherited, not defined on BacktestAccount -- the cap has leaked up"


def test_a_live_account_config_key_named_equity_cap_is_never_honoured():
    """The config side of the same rule: the ONLY reader of the key is BacktestAccount."""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))
    out = subprocess.run(
        ["git", "grep", "-l", "equity_cap", "--", "ba2_trade_platform", "packages"],
        cwd=root, capture_output=True, text=True)
    hits = [line for line in out.stdout.splitlines() if line.strip()]
    assert hits == [], f"equity_cap reached live/shared code: {hits}"
