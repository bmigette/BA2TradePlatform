"""An assigned short option's trade ROW must not count the assignment loss twice.

The defect (measured on a real wheel run: rows summed to -$13,119 while equity rose $37,317)
------------------------------------------------------------------------------------------
``settle_single_leg_expiry`` used to close a physically ASSIGNED short option at its intrinsic
value, so its row read ``premium - intrinsic``. The share leg moves cash AT THE STRIKE and the
stock lot starts at the strike -- already ``intrinsic`` above (put) / below (call) the market --
so the stock row carried the same loss again. Cash and equity were right; every row-based
metric (win rate, profit factor, the profit-cap ``adjusted_*`` metrics, the fitness
concentration / Monte-Carlo screens) was wrong for any strategy that gets assigned.

The convention (the one live uses): on physical assignment the option leg closes at 0 -- the
premium is fully kept -- and the shares carry the strike. Live:
``AlpacaAccount._apply_option_activity`` opens the stock at ``strike`` (``open_price=strike``)
and ``_record_option_settlement_order`` writes the option settlement at ``open_price=0.0``
("Assignment, exercise and expiry all extinguish the contract without a premium changing
hands"). Then sum(row P&L) == the change in equity.

All numbers are hand-computed from the wheel fixture (zero commission, 100k start): put sold at
21.00 (+2,100), assigned at 180 with the underlying at 164 (intrinsic 16), shares sold next bar
open 165.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.backtest.test_wheel_assignment import (
    _BASE_CFG, _CALL170, _PUT180, _assign_the_put, _build, _engine, _sell_the_put,
    _write_the_call, _shares,
)

_START = 100_000.0


def _rows(acct):
    return acct.get_round_trip_trades()


def _row_sum(acct):
    return sum(float(r["pnl"]) for r in _rows(acct))


def _option_rows(acct):
    return [r for r in _rows(acct) if r["contract_symbol"]]


def test_assigned_short_put_rows_tie_to_equity(tmp_path):
    cfg = dict(_BASE_CFG)
    acct, ps, ctx = _build(tmp_path, "rowput", 81, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()

        # cash: +2,100 premium, -18,000 shares at the strike, +16,500 sold at 165.
        assert _shares(acct) == pytest.approx(0.0)
        assert acct._cash == pytest.approx(100_600.0)
        assert acct.equity() == pytest.approx(100_600.0)

        (opt,) = _option_rows(acct)
        assert opt["pnl"] == pytest.approx(2_100.0), "premium fully kept: closed at 0"
        assert float(opt["exit_price"]) == pytest.approx(0.0)
        (stock,) = [r for r in _rows(acct) if not r["contract_symbol"]]
        assert stock["pnl"] == pytest.approx(-1_500.0)            # 180 -> 165
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)   # 600 == 600
    finally:
        ctx.__exit__(None, None, None)


def test_assigned_put_equity_and_cash_are_exactly_the_hand_computed_numbers(tmp_path):
    """The cash ledger must not move because of the row fix (hold variant, stock marked)."""
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "rowcash", 82, cfg)
    try:
        _sell_the_put(acct)
        assert acct._cash == pytest.approx(102_100.0)
        _assign_the_put(acct, ps, cfg)
        assert acct._cash == pytest.approx(84_100.0)
        # equity = cash + 100 shares marked at the 2024-03-08 close of 164.
        assert acct.equity() == pytest.approx(84_100.0 + 16_400.0)
        # open_at_end stock row is marked at 164: -1,600; option row +2,100 -> +500 == equity change.
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_covered_call_called_away_rows_tie_to_equity(tmp_path):
    cfg = {**_BASE_CFG, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "rowcc", 83, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()
        _write_the_call(acct, occ=_CALL170, strike=170.0)         # +600
        ps.set_clock(datetime(2024, 3, 28))
        _engine(acct, ps, cfg)._apply_option_expiry(datetime(2024, 3, 28))
        acct.refresh_transactions()

        assert _shares(acct) == pytest.approx(0.0)
        assert acct._cash == pytest.approx(101_700.0)             # 84,100 + 600 + 17,000
        assert acct.equity() == pytest.approx(101_700.0)
        pnls = sorted(float(r["pnl"]) for r in _rows(acct))
        # call +600 (kept in full), put +2,100, stock 180 -> 170 called away = -1,000.
        assert pnls == pytest.approx([-1_000.0, 600.0, 2_100.0])
        assert _row_sum(acct) == pytest.approx(1_700.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_worthless_short_put_is_unchanged(tmp_path):
    cfg = dict(_BASE_CFG)
    acct, ps, ctx = _build(tmp_path, "rowotm", 84, cfg)
    try:
        _sell_the_put(acct)
        ps.set_clock(datetime(2024, 3, 8))
        lot = [p for p in acct.get_option_positions() if p.contract_symbol == _PUT180][0]
        assert acct.settle_single_leg_expiry(lot, 190.0) is True
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(102_100.0)
        (opt,) = _rows(acct)
        assert opt["pnl"] == pytest.approx(2_100.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_an_itm_long_call_is_still_sold_to_close_at_intrinsic(tmp_path):
    """Long ITM options are never exercised: the row closes at the intrinsic that was CREDITED."""
    cfg = dict(_BASE_CFG)
    acct, ps, ctx = _build(tmp_path, "rowlong", 85, cfg)
    try:
        from ba2_common.core.option_types import OptionLeg
        from ba2_common.core.types import OptionRight, OrderDirection
        from tests.backtest.test_wheel_assignment import _EXP_CALL

        ps.set_clock(datetime(2024, 3, 11))
        acct.submit_option_order(
            legs=[OptionLeg(contract_symbol=_CALL170, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.CALL,
                            strike=170.0, expiry=_EXP_CALL, underlying="AAPL")],
            quantity=1, order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        cash_after_entry = acct._cash
        assert cash_after_entry < _START                           # a debit was paid
        ps.set_clock(datetime(2024, 3, 28))
        lot = [p for p in acct.get_option_positions() if p.contract_symbol == _CALL170][0]
        assert acct.settle_single_leg_expiry(lot, 190.0) is True
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(cash_after_entry + 2_000.0)   # intrinsic 20 x 100
        (opt,) = _rows(acct)
        assert float(opt["exit_price"]) == pytest.approx(20.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_a_defined_risk_combo_settlement_is_unchanged(tmp_path):
    """The combo's intrinsic fills DO move cash, so its rows already tie to equity."""
    from tests.backtest import test_butterfly_true_value_bounds as fly

    ctx, acct, ps = fly._build(tmp_path, (100, 110, 120), spot_late=150)
    try:
        assert acct._cash == pytest.approx(50_000.0 - 750.0)       # 11 - 2x2 + 0.5 debit
        ps.set_clock(fly._EXPIRY_BAR)
        held = list(acct.get_option_positions())
        assert acct.settle_defined_risk_combo_expiry(held, 150.0) is True
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(49_250.0)               # balanced fly pays 0
        assert acct.equity() == pytest.approx(49_250.0)
        assert _row_sum(acct) == pytest.approx(-750.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - 50_000.0)
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# The end-of-run invariant: reported, never raised.
# ---------------------------------------------------------------------------
def test_the_tie_out_check_names_the_dollar_gap_and_does_not_raise(caplog):
    import logging
    from app.services.backtest.results import warn_if_rows_do_not_tie_to_equity

    with caplog.at_level(logging.WARNING):
        gap = warn_if_rows_do_not_tie_to_equity(
            [{"pnl": -13_119.0}], initial=100_000.0, final=137_317.0)
    assert gap == pytest.approx(-50_436.0)
    assert any("DO NOT TIE" in r.getMessage() and "-50436.00" in r.getMessage()
               for r in caplog.records)


def test_the_tie_out_check_is_silent_when_rows_tie(caplog):
    import logging
    from app.services.backtest.results import warn_if_rows_do_not_tie_to_equity

    with caplog.at_level(logging.WARNING):
        gap = warn_if_rows_do_not_tie_to_equity(
            [{"pnl": 600.0}], initial=100_000.0, final=100_600.0)
    assert gap == 0.0
    assert not [r for r in caplog.records if "DO NOT TIE" in r.getMessage()]


def test_the_tolerance_scales_with_a_growing_run(caplog):
    """$20k -> $1.7M: 0.5% of the STARTING capital is a fixed $100; the base is max(start, end)."""
    import logging
    from app.services.backtest.results import warn_if_rows_do_not_tie_to_equity

    with caplog.at_level(logging.WARNING):
        # gap $500 on a $1.7M run is 0.03%: silent (a start-based base would shout).
        warn_if_rows_do_not_tie_to_equity([{"pnl": 1_680_500.0}], initial=20_000.0,
                                          final=1_700_000.0)
    assert not [r for r in caplog.records if "DO NOT TIE" in r.getMessage()]
    with caplog.at_level(logging.WARNING):
        gap = warn_if_rows_do_not_tie_to_equity([{"pnl": 1_660_000.0}], initial=20_000.0,
                                                final=1_700_000.0)
    assert gap == pytest.approx(-20_000.0)
    (msg,) = [r.getMessage() for r in caplog.records if "DO NOT TIE" in r.getMessage()]
    assert "1.18%" in msg                                     # 20,000 / 1,700,000


def test_a_malformed_row_surfaces_and_a_non_finite_gap_logs_an_error(caplog):
    import logging
    from app.services.backtest.results import warn_if_rows_do_not_tie_to_equity

    with pytest.raises(KeyError):
        warn_if_rows_do_not_tie_to_equity([{"size": 1}], initial=1.0, final=1.0)
    with caplog.at_level(logging.ERROR):
        assert warn_if_rows_do_not_tie_to_equity(
            [{"pnl": float("nan")}], initial=100.0, final=100.0) is None
    assert [r for r in caplog.records
            if r.levelno == logging.ERROR and "NON-FINITE" in r.getMessage()]


def test_assigned_put_cycle_ties_to_the_cent_with_commission(tmp_path):
    """Settlement and the assignment liquidation charge no commission, so the rows must not."""
    cfg = {**_BASE_CFG, "commission_per_trade": 1.0}
    acct, ps, ctx = _build(tmp_path, "rowcomm1", 86, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(100_599.0)         # the put sale paid $1; nothing else
        assert _row_sum(acct) == pytest.approx(599.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_called_away_cycle_ties_to_the_cent_with_commission(tmp_path):
    cfg = {**_BASE_CFG, "commission_per_trade": 1.0, "hold_assigned_stock": True}
    acct, ps, ctx = _build(tmp_path, "rowcomm2", 87, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        ps.set_clock(datetime(2024, 3, 11))
        acct.process_pending_assignment_liquidations()
        _write_the_call(acct, occ=_CALL170, strike=170.0)
        ps.set_clock(datetime(2024, 3, 28))
        _engine(acct, ps, cfg)._apply_option_expiry(datetime(2024, 3, 28))
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(101_698.0)         # two option sales, $1 each
        assert _row_sum(acct) == pytest.approx(1_698.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_short_strangle_one_leg_assigned_other_worthless_ties_to_equity(tmp_path):
    """Call 180 sold at 4.00 is assigned (spot 200: short 100 @180); put 160 sold at 2.50 expires
    worthless; the short shares are bought back at the next open (200).
    cash: +400 +250 (premia) +18,000 (shares sold at the strike) -20,000 (buy back) = -1,350."""
    from ba2_common.core.option_types import OptionLeg
    from ba2_common.core.types import OptionRight, OrderDirection
    from datetime import date
    from tests.backtest import test_option_assignment as ta

    legs = [
        OptionLeg(contract_symbol=ta._CALL_OCC, side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.CALL,
                  strike=180.0, expiry=date(2024, 3, 15), underlying="AAPL"),
        OptionLeg(contract_symbol=ta._PUT_OCC, side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.PUT,
                  strike=160.0, expiry=date(2024, 3, 15), underlying="AAPL"),
    ]
    engine, acct, ps, ctx = ta._make_engine(tmp_path, legs, "short_strangle")
    try:
        ps.set_clock(datetime(2024, 3, 15))
        engine._apply_option_expiry(datetime(2024, 3, 15))
        ps.set_clock(datetime(2024, 3, 18))
        assert acct.process_pending_assignment_liquidations() is True
        acct.refresh_transactions()
        assert acct._cash == pytest.approx(98_650.0)
        assert acct.equity() == pytest.approx(98_650.0)
        pnls = sorted(float(r["pnl"]) for r in _rows(acct))
        assert pnls == pytest.approx([-2_000.0, 250.0, 400.0])   # stock 180 -> 200 short
        assert _row_sum(acct) == pytest.approx(-1_350.0)
        assert _row_sum(acct) == pytest.approx(acct.equity() - _START)
    finally:
        ctx.__exit__(None, None, None)


def test_an_assigned_option_transaction_has_no_close_price_like_live(tmp_path):
    """Live closes the OPASN option transaction with no close price (``_close_txn``)."""
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import AssetClass, TransactionStatus

    cfg = dict(_BASE_CFG)
    acct, ps, ctx = _build(tmp_path, "rowclosepx", 88, cfg)
    try:
        _sell_the_put(acct)
        _assign_the_put(acct, ps, cfg)
        (t,) = [t for t in transactions_where(status=TransactionStatus.CLOSED)
                if t.asset_class == AssetClass.OPTION]
        assert t.close_price is None
    finally:
        ctx.__exit__(None, None, None)
