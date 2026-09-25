"""Task 1b (plan 2026-09-24): an option lot held across an INTEGER FORWARD split is re-keyed onto
the ADJUSTED contract on the ex-date, the way OCC and the broker do it.

WHAT LIVE DOES. OCC adjusts every listed contract on the ex-date of a k:1 split: strike / k,
contracts x k, deliverable still 100 shares, and the position is carried under the NEW OCC
string (Task 0: AAPL200918P00400000 has no bar after 2020-08-28; the adjusted contract trades as
AAPL200918P00100000 from 2020-08-31). Task 1a stopped the backtest from valuing the old string
against a post-split spot, but left the lot stranded in its own basis: no bars, no quotes, no
exit by an order, settled only at expiry. Task 1b moves the lot AND its order linkage to the
adjusted contract, so the position keeps real marks and an exit rule closes the adjusted contract
at its real bar -- which is what live does.

Integer forward splits only. A 3:2 or a reverse split is not re-keyed (Task 1a's own-basis
valuation stays) and says so. A combo is re-keyed whole or not at all.

The fixture is Task 1a's: AAPL, 4:1 on 2020-08-31, adjusted closes (pre-split raw / 4), an
AS-TRADED option store.
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import logging

import pytest

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.split_basis import CalendarSplit, SymbolSplitBasis
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus, TransactionStatus

from tests.backtest.test_option_split_crossing import (
    EXPIRY, LAST_PRE, OPEN_DAY, SPLIT, _Chain, _bar, _dt, _expire, _expiry_close, _sessions,
)

PUT400 = "AAPL200918P00400000"
ADJ_PUT100 = "AAPL200918P00100000"
PUT410 = "AAPL200918P00410000"
ADJ_PUT1025 = "AAPL200918P00102500"
CALL500 = "AAPL200918C00500000"
ADJ_CALL125 = "AAPL200918C00125000"

CFG = {**_LEGACY_ZERO_SPREAD, "starting_cash": 1_000_000.0, "commission_per_trade": 0.0,
       "slippage_bps": 0.0, "fill_model": "same_bar_close"}
NBO = {**CFG, "fill_model": "next_bar_open"}


def _basis(ratio=4.0):
    from app.services.backtest.option_split_basis import RunSplitBasis
    return RunSplitBasis({"AAPL": SymbolSplitBasis(
        "AAPL", (CalendarSplit(SPLIT, ratio),), basis_date=date(2026, 1, 1))})


def _closes(ratio=4.0, pre_raw=400.0, post_raw=400.0, overrides=None):
    """ADJUSTED closes. Flat as traded by default (400 before, 400/ratio after, i.e. an adjusted
    series that does not move) so any equity move on the ex-date is the re-key's, not the
    market's."""
    overrides = overrides or {}
    return [(d, overrides.get(d, (pre_raw / ratio) if d < SPLIT else (post_raw / ratio)))
            for d in _sessions()]


@contextmanager
def _harness(opt_bars, closes, cfg=CFG, basis=None):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db("optsplitrekey")
    ctx.__enter__()
    try:
        seed_account_definition(1, cfg)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": _dt(d), "Open": c, "High": c, "Low": c, "Close": c,
                               "Volume": 1e6} for d, c in closes])
        ps.set_clock(_dt(OPEN_DAY))
        acct = BacktestAccount(1, ps, cfg, options_provider=_Chain(opt_bars),
                               split_basis=basis if basis is not None else _basis())
        wire_backtest_seams().register_account(1, acct)
        engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
        engine.account = acct
        engine.price = ps
        engine.config = cfg
        yield engine, acct, ps
    finally:
        ctx.__exit__(None, None, None)


def _leg(contract, right, strike, side):
    return OptionLeg(contract_symbol=contract, side=side,
                     position_intent="buy_to_open" if side == OrderDirection.BUY else "sell_to_open",
                     option_type=right, strike=strike, expiry=EXPIRY, underlying="AAPL")


def _open(acct, contract, right, strike, side, strategy, qty=1):
    acct.submit_option_order(legs=[_leg(contract, right, strike, side)], quantity=qty,
                             order_type="market", option_strategy=strategy)
    acct.refresh_orders()
    acct.refresh_transactions()
    lot = acct._option_positions[contract]
    assert lot.qty == (qty if side == OrderDirection.BUY else -qty)
    return lot


def _step(acct, ps, day):
    """The engine's bar head: clock, then the re-key pass (daily_engine step 1c)."""
    ps.set_clock(_dt(day))
    return acct.apply_split_rekeys()


def _series(contract, days_closes):
    return {(contract, d): _bar(c) for d, c in days_closes.items()}


def _rows(acct, contract):
    return [o for o in acct.get_orders() if o.contract_symbol == contract]


D0901, D0902, D0903 = date(2020, 9, 1), date(2020, 9, 2), date(2020, 9, 3)


# =============================================================================================
# the OCC adjusted-strike field
# =============================================================================================
@pytest.mark.parametrize("contract,ratio,expected", [
    (PUT400, 4, (ADJ_PUT100, 100.0)),
    (PUT410, 4, (ADJ_PUT1025, 102.5)),
    # ThetaData ANET 2024-12-04 (4:1): 362.5 / 4 = 90.625 trades as P00090630 (strike 90.63)
    ("ANET241220P00362500", 4, ("ANET241220P00090630", 90.63)),
    ("ANET241220C00352500", 4, ("ANET241220C00088130", 88.13)),
    # ThetaData TSLA 2022-08-25 (3:1): 815 / 3 = 271.666.. trades as 271.67
    ("TSLA220916P00815000", 3, ("TSLA220916P00271670", 271.67)),
])
def test_the_adjusted_contract_is_the_occ_string_at_strike_over_k_rounded_to_the_cent(
        contract, ratio, expected):
    from app.services.backtest.backtest_account import BacktestAccount
    sym, strike = BacktestAccount._adjusted_occ(contract, ratio)
    assert (sym, strike) == (expected[0], pytest.approx(expected[1]))


# =============================================================================================
# 1. long put across a 4:1 split: re-keyed, marked from the adjusted contract, exited by an order
# =============================================================================================
def _long_put_bars():
    bars = _series(PUT400, {date(2020, 8, 21): 15.0, OPEN_DAY: 15.0, LAST_PRE: 12.0})
    bars.update(_series(ADJ_PUT100, {SPLIT: 3.0, D0901: 4.0, D0902: 5.0, D0903: 5.5}))
    return bars


def test_a_long_put_is_rekeyed_onto_the_adjusted_contract_on_the_ex_date(caplog):
    with _harness(_long_put_bars(), _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(date(2020, 8, 21)))
        old = _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put", qty=2)
        assert (old.avg_price, old.basis_factor, old.basis_date) == (15.0, 4.0, OPEN_DAY)
        assert _step(acct, ps, LAST_PRE) == 0
        cash = acct._cash
        eq_pre = acct.equity()
        assert eq_pre == pytest.approx(cash + 12.0 * 2 * 100)

        with caplog.at_level(logging.INFO):
            assert _step(acct, ps, SPLIT) == 1

        # The lot: 2 x P400 @ 15 -> 8 x P100 @ 3.75, today's basis, same multiplier.
        assert acct._option_positions[PUT400].qty == 0
        new = acct._option_positions[ADJ_PUT100]
        assert (new.qty, new.avg_price, new.multiplier) == (8.0, pytest.approx(3.75), 100.0)
        assert (new.underlying, new.basis_factor, new.basis_date) == ("AAPL", 1.0, SPLIT)
        assert new.last_iv == old.last_iv or old.last_iv is None
        # An identity transformation: no cash moves, and the adjusted contract at the same
        # spot (flat as traded) is worth what the old one was: 3.00 x 8 == 12.00 x 2.
        assert acct._cash == pytest.approx(cash)
        assert acct.equity() == pytest.approx(eq_pre)
        assert not acct._crossed_held_lot(ADJ_PUT100)
        assert acct.get_option_quote(ADJ_PUT100).bid == pytest.approx(3.0)

        # The linkage: the entry order now names the adjusted contract, with an audit note.
        (entry,) = _rows(acct, ADJ_PUT100)
        assert (entry.strike, entry.quantity, entry.filled_qty) == (100.0, 8.0, 8.0)
        assert entry.open_price == pytest.approx(3.75)
        note = entry.data["split_rekey"]
        assert note["from_contract"] == PUT400 and note["to_contract"] == ADJ_PUT100
        assert note["ratio"] == 4 and note["from_strike"] == 400.0
        assert note["from_quantity"] == 2.0 and note["from_open_price"] == pytest.approx(15.0)
        assert _rows(acct, PUT400) == []
        (pos,) = acct.get_option_positions()
        assert (pos.contract_symbol, pos.strike, pos.quantity) == (ADJ_PUT100, 100.0, 8.0)
        assert pos.avg_entry_price == pytest.approx(3.75)
        assert acct._option_transaction_for_contract(ADJ_PUT100) is not None

        msgs = [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and "RE-KEYED" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert PUT400 in msgs[0] and ADJ_PUT100 in msgs[0] and "4:1" in msgs[0]
        assert not [r for r in caplog.records if "ACROSS A SPLIT" in r.getMessage()]


def test_an_exit_rule_closes_the_adjusted_contract_at_its_real_bar_with_the_adjusted_qty():
    """close_option on the transaction (the shared TradeActions path an exit rule runs) submits a
    sell-to-close of 8 x P100, which fills at the adjusted contract's own next-bar open (5.00).
    P&L: sold 8 x 5.00 x 100 = 4000 against 2 x 15.00 x 100 = 3000 paid -> +1000."""
    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_common.core.types import OrderRecommendation

    with _harness(_long_put_bars(), _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(date(2020, 8, 21)))
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put", qty=2)
        start_cash = CFG["starting_cash"]
        _step(acct, ps, LAST_PRE)
        _step(acct, ps, SPLIT)
        assert _step(acct, ps, D0901) == 0
        (entry,) = _rows(acct, ADJ_PUT100)

        action = CloseOptionAction(instrument_name="AAPL", account=acct,
                                   order_recommendation=OrderRecommendation.SELL,
                                   existing_order=entry)
        # The fixture has no ExpertRecommendation row for the action-result audit record; the
        # close decision and submission are what is under test, so the audit write is stubbed.
        from types import SimpleNamespace
        action.create_and_save_action_result = lambda **kw: SimpleNamespace(**kw)
        result = action.execute()
        assert result.success, result.message
        (close,) = [o for o in _rows(acct, ADJ_PUT100) if o.side == OrderDirection.SELL]
        assert close.quantity == 8

        acct.refresh_orders()          # decided 09-01, fills on 09-02's open (5.00)
        acct.refresh_transactions()
        assert close.status == OrderStatus.FILLED and close.open_price == pytest.approx(5.0)
        assert acct._option_positions[ADJ_PUT100].qty == 0
        assert acct._cash == pytest.approx(start_cash - 3000.0 + 4000.0)

        (row,) = [t for t in acct.get_round_trip_trades()]
        # ONE trade, stated in the ORIGINAL contract's units: 2 x P400, entry 15.00, exit
        # 5.00 x 4 = 20.00 per original share; the adjusted exit is in the split_rekey note.
        assert row["contract_symbol"] == PUT400 and row["strike"] == 400.0
        assert row["entry_price"] == pytest.approx(15.0)
        assert row["size"] == pytest.approx(2.0)
        assert row["exit_price"] == pytest.approx(20.0)
        assert row["pnl"] == pytest.approx(1000.0)
        assert row["option_basis_factor"] == 4.0
        rk = row["split_rekey"]
        assert (rk["from_contract"], rk["to_contract"], rk["ratio"]) == (PUT400, ADJ_PUT100, 4)
        assert rk["exit_price"] == pytest.approx(5.0) and rk["size"] == pytest.approx(8.0)
        # (the fixture's entry was submitted directly, so it carries no entry_record; the
        # close ran through CloseOptionAction and its exit record names the adjusted leg)
        assert row["exit_record"]["leg"]["contract_symbol"] == ADJ_PUT100

        from app.services.backtest.results import _trade_row
        assert _trade_row(row)["split_rekey"]["to_contract"] == ADJ_PUT100


def test_the_rekeyed_lot_settles_at_expiry_on_the_adjusted_strike():
    """Adjusted close at expiry 95: P100 intrinsic 5.00 on 8 contracts (= P400's 20.00 on 2)."""
    bars = _long_put_bars()
    closes = _closes(overrides={EXPIRY: 95.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put", qty=2)
        cash = acct._cash
        _step(acct, ps, SPLIT)
        ps.set_clock(_dt(EXPIRY))
        _expire(engine, acct, ps)
        assert _expiry_close(acct, ADJ_PUT100).open_price == pytest.approx(5.0)
        assert _expiry_close(acct, ADJ_PUT100).quantity == 8
        assert acct._cash == pytest.approx(cash + 5.0 * 8 * 100)
        (row,) = acct.get_round_trip_trades()
        assert row["exit_price"] == pytest.approx(20.0) and row["size"] == pytest.approx(2.0)
        assert row["pnl"] == pytest.approx((20.0 - 15.0) * 2 * 100)


# =============================================================================================
# 2. the adjusted contract is not in the store
# =============================================================================================
def test_no_adjusted_contract_in_the_store_means_no_rekey_and_a_warning(caplog):
    bars = _series(PUT400, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    closes = _closes(overrides={EXPIRY: 95.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
        cash = acct._cash
        with caplog.at_level(logging.WARNING):
            assert _step(acct, ps, SPLIT) == 0
            assert _step(acct, ps, D0901) == 0
        warns = [r.getMessage() for r in caplog.records if "NOT RE-KEYED" in r.getMessage()]
        assert len(warns) == 1, warns
        assert ADJ_PUT100 in warns[0] and PUT400 in warns[0]
        lot = acct._option_positions[PUT400]
        assert (lot.qty, lot.basis_factor) == (1.0, 4.0)
        assert ADJ_PUT100 not in acct._option_positions
        # ...and it rides to expiry in its own basis (Task 1a): 95 adjusted = 380, P400 -> 20.
        _expire(engine, acct, ps)
        assert _expiry_close(acct, PUT400).open_price == pytest.approx(20.0)
        assert acct._cash == pytest.approx(cash + 20.0 * 100)


def test_the_rekey_waits_for_the_first_bar_that_lists_the_adjusted_contract():
    """No look-ahead: nothing on the ex-date, the adjusted contract first listed on 09-02 ->
    re-keyed on 09-02, not before."""
    bars = _series(PUT400, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    bars.update(_series(ADJ_PUT100, {D0902: 3.2}))
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
        assert _step(acct, ps, SPLIT) == 0
        assert _step(acct, ps, D0901) == 0
        assert acct._option_positions[PUT400].qty == 1
        assert _step(acct, ps, D0902) == 1
        new = acct._option_positions[ADJ_PUT100]
        assert (new.qty, new.basis_date) == (4.0, D0902)


# =============================================================================================
# 3. non-integer and reverse splits stay on Task 1a
# =============================================================================================
@pytest.mark.parametrize("ratio,word", [(1.5, "non-integer"), (0.25, "reverse")])
def test_a_non_integer_or_reverse_split_is_not_rekeyed_and_says_so(ratio, word, caplog):
    contract = "AAPL200918P00150000"
    bars = _series(contract, {OPEN_DAY: 5.0, LAST_PRE: 4.0})
    # Every string the adjusted contract could be is listed, so only the ratio refuses.
    for cand in ("AAPL200918P00100000", "AAPL200918P00600000"):
        bars.update(_series(cand, {d: 1.0 for d in _sessions() if d >= SPLIT}))
    with _harness(bars, _closes(ratio=ratio, pre_raw=150.0, post_raw=150.0),
                  basis=_basis(ratio)) as (engine, acct, ps):
        _open(acct, contract, OptionRight.PUT, 150.0, OrderDirection.BUY, "long_put")
        with caplog.at_level(logging.WARNING):
            assert _step(acct, ps, SPLIT) == 0
            assert _step(acct, ps, D0901) == 0
        warns = [r.getMessage() for r in caplog.records if "NOT RE-KEYED" in r.getMessage()]
        assert len(warns) == 1, warns
        assert word in warns[0] and contract in warns[0]
        assert acct._option_positions[contract].qty == 1
        assert acct._option_positions[contract].basis_factor == ratio


# =============================================================================================
# 4. covered call across a split
# =============================================================================================
def test_a_covered_call_is_rekeyed_and_stays_fully_covered():
    """400 adjusted shares (= 100 pre-split) cover one pre-split C500. After the re-key: 4 x C125
    written in today's basis, still exactly covered, no naked margin, every share pledged."""
    bars = _series(CALL500, {OPEN_DAY: 2.0, LAST_PRE: 1.6})
    bars.update(_series(ADJ_CALL125, {SPLIT: 0.4, D0901: 0.35}))
    with _harness(bars, _closes()) as (engine, acct, ps):
        acct._update_position("AAPL", 400.0, 100.0)
        _open(acct, CALL500, OptionRight.CALL, 500.0, OrderDirection.SELL, "covered_call")
        assert CALL500 in acct._covered_short_call_contracts()
        assert _step(acct, ps, SPLIT) == 1
        lot = acct._option_positions[ADJ_CALL125]
        assert (lot.qty, lot.avg_price) == (-4.0, pytest.approx(0.5))
        assert acct._positions["AAPL"].qty == 400.0          # the book is split-adjusted
        assert acct._covered_short_call_contracts() == {ADJ_CALL125}
        assert acct.maintenance_margin_requirement() == pytest.approx(0.0)
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 400
        assert acct.pledged_shares_in_equity_units("AAPL", 400) == 400
        assert acct._pledged_share_lock("AAPL", 100.0, context="test") == pytest.approx(0.0)
        acct._update_position("AAPL", 100.0, 100.0)
        assert acct._pledged_share_lock("AAPL", 100.0, context="test") == pytest.approx(100.0)


# =============================================================================================
# 5. a defined-risk vertical: all legs in one bar, or none
# =============================================================================================
def _vertical(acct):
    short = _leg(PUT410, OptionRight.PUT, 410.0, OrderDirection.SELL)
    long_ = _leg(PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY)
    acct.submit_option_order(legs=[short, long_], quantity=1, order_type="market",
                             option_strategy="bull_put_spread")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._option_positions[PUT410].qty == -1 and acct._option_positions[PUT400].qty == 1


def _vertical_bars(with_short_adj=True):
    bars = _series(PUT410, {OPEN_DAY: 15.0, LAST_PRE: 14.0})
    bars.update(_series(PUT400, {OPEN_DAY: 10.0, LAST_PRE: 9.0}))
    bars.update(_series(ADJ_PUT100, {SPLIT: 2.25, D0901: 2.2}))
    if with_short_adj:
        bars.update(_series(ADJ_PUT1025, {SPLIT: 3.5, D0901: 3.4}))
    return bars


def test_a_vertical_is_rekeyed_whole_in_one_bar():
    with _harness(_vertical_bars(), _closes()) as (engine, acct, ps):
        _vertical(acct)
        _step(acct, ps, LAST_PRE)
        eq_pre = acct.equity()
        assert _step(acct, ps, SPLIT) == 2
        assert acct._option_positions[ADJ_PUT1025].qty == -4
        assert acct._option_positions[ADJ_PUT100].qty == 4
        assert acct._defined_risk_contracts() == {ADJ_PUT1025, ADJ_PUT100}
        assert acct.maintenance_margin_requirement() == pytest.approx(0.0)
        # the structure: 4 structures of a 2.50-wide spread = the old 1 x 10.00 -> $1000 width
        _, bounds = acct._option_group_bounds()
        (gb,) = bounds.values()
        assert gb["width"] == pytest.approx(1000.0)
        # same spot as traded, the adjusted legs at the old prices / 4: equity unchanged
        assert acct.equity() == pytest.approx(eq_pre)
        parent = [o for o in acct.get_orders() if o.contract_symbol is None][0]
        assert parent.quantity == 4 and parent.data["split_rekey"]["ratio"] == 4
        legs = {p.contract_symbol: p.quantity for p in acct.get_option_positions()}
        assert legs == {ADJ_PUT1025: 4.0, ADJ_PUT100: 4.0}


def test_close_option_on_a_rekeyed_vertical_closes_both_adjusted_legs_x_k():
    """The shared multi-leg close (CloseOptionAction._close_multi_leg) reads the parent's
    structure count and nets the transaction's child rows: after the re-key it must reverse
    4 x P102.5 / 4 x P100, not 1 x P410 / 1 x P400."""
    from types import SimpleNamespace
    from ba2_common.core.TradeActions import CloseOptionAction
    from ba2_common.core.types import OrderRecommendation
    with _harness(_vertical_bars(), _closes()) as (engine, acct, ps):
        _vertical(acct)
        _step(acct, ps, SPLIT)
        _step(acct, ps, D0901)
        parent = [o for o in acct.get_orders() if o.contract_symbol is None][0]
        action = CloseOptionAction(instrument_name="AAPL", account=acct,
                                   order_recommendation=OrderRecommendation.BUY,
                                   existing_order=parent)
        action.create_and_save_action_result = lambda **kw: SimpleNamespace(**kw)
        result = action.execute()
        assert result.success, result.message
        closing = [o for o in acct.get_orders()
                   if o.contract_symbol and o.status != OrderStatus.FILLED]
        assert {(o.contract_symbol, o.side, o.quantity) for o in closing} == {
            (ADJ_PUT1025, OrderDirection.BUY, 4), (ADJ_PUT100, OrderDirection.SELL, 4)}
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[ADJ_PUT1025].qty == 0
        assert acct._option_positions[ADJ_PUT100].qty == 0


def test_a_vertical_with_one_leg_missing_its_adjusted_contract_is_not_rekeyed_at_all(caplog):
    with _harness(_vertical_bars(with_short_adj=False), _closes()) as (engine, acct, ps):
        _vertical(acct)
        with caplog.at_level(logging.WARNING):
            assert _step(acct, ps, SPLIT) == 0
        assert acct._option_positions[PUT410].qty == -1
        assert acct._option_positions[PUT400].qty == 1
        assert ADJ_PUT100 not in acct._option_positions
        assert {o.contract_symbol for o in acct.get_orders() if o.contract_symbol} == {
            PUT410, PUT400}
        warns = [r.getMessage() for r in caplog.records if "NOT RE-KEYED" in r.getMessage()]
        assert any(ADJ_PUT1025 in w for w in warns), warns
        assert any("whole" in w and PUT400 in w for w in warns), warns


# =============================================================================================
# 6. a short put re-keyed, then assigned at expiry
# =============================================================================================
def test_a_rekeyed_short_put_is_assigned_as_qty_x_k_x_100_shares_at_strike_over_k():
    bars = _series(PUT410, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    bars.update(_series(ADJ_PUT1025, {SPLIT: 3.0}))
    closes = _closes(overrides={EXPIRY: 95.0})
    with _harness(bars, closes) as (engine, acct, ps):
        _open(acct, PUT410, OptionRight.PUT, 410.0, OrderDirection.SELL, "short_put")
        cash = acct._cash
        assert _step(acct, ps, SPLIT) == 1
        assert acct._option_positions[ADJ_PUT1025].qty == -4
        _expire(engine, acct, ps)
        close = _expiry_close(acct, ADJ_PUT1025)
        assert close.open_price == pytest.approx(7.5) and close.quantity == 4
        pos = acct._positions["AAPL"]
        assert pos.qty == pytest.approx(400.0) and pos.avg_price == pytest.approx(102.5)
        assert acct._cash == pytest.approx(cash - 41_000.0)
        (row,) = [t for t in acct.get_round_trip_trades() if t.get("contract_symbol")]
        # original units: short 1 x P410 @ 15, closed at 30 (= 7.50 x 4) -> -1500
        assert (row["contract_symbol"], row["size"]) == (PUT410, pytest.approx(1.0))
        assert row["exit_price"] == pytest.approx(30.0)
        assert row["pnl"] == pytest.approx(-1500.0)


# =============================================================================================
# merging into a lot already held under the adjusted string; refusing an opposite sign
# =============================================================================================
def test_a_rekey_merges_into_a_same_side_lot_already_held_on_the_adjusted_contract():
    bars = _series(PUT400, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    bars.update(_series(ADJ_PUT100, {SPLIT: 3.0}))
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(SPLIT))
        _open(acct, ADJ_PUT100, OptionRight.PUT, 100.0, OrderDirection.BUY, "long_put")
        assert acct.apply_split_rekeys() == 1
        lot = acct._option_positions[ADJ_PUT100]
        assert lot.qty == 5 and lot.avg_price == pytest.approx((3.75 * 4 + 3.0) / 5)
        assert (lot.basis_factor, lot.basis_date) == (1.0, SPLIT)


def test_a_rekey_onto_an_opposite_side_lot_is_refused_loudly(caplog):
    bars = _series(PUT400, {OPEN_DAY: 15.0, LAST_PRE: 12.0})
    bars.update(_series(ADJ_PUT100, {SPLIT: 3.0}))
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, PUT400, OptionRight.PUT, 400.0, OrderDirection.BUY, "long_put")
        ps.set_clock(_dt(SPLIT))
        _open(acct, ADJ_PUT100, OptionRight.PUT, 100.0, OrderDirection.SELL, "short_put")
        with caplog.at_level(logging.WARNING):
            assert acct.apply_split_rekeys() == 0
        assert acct._option_positions[PUT400].qty == 1
        assert acct._option_positions[ADJ_PUT100].qty == -1
        assert any("opposite" in r.getMessage() and r.levelno >= logging.ERROR
                   for r in caplog.records)


# =============================================================================================
# 7. control: nothing crosses a split -> nothing changes
# =============================================================================================
def test_no_split_crossing_leaves_the_book_untouched():
    contract = "AAPL200828P00410000"
    bars = _series(contract, {OPEN_DAY: 15.0, date(2020, 8, 26): 11.0})
    with _harness(bars, _closes()) as (engine, acct, ps):
        _open(acct, contract, OptionRight.PUT, 410.0, OrderDirection.BUY, "long_put")
        for d in (date(2020, 8, 25), date(2020, 8, 26), LAST_PRE):
            assert _step(acct, ps, d) == 0
        (entry,) = _rows(acct, contract)
        assert "split_rekey" not in (entry.data or {})
        (row,) = acct.get_round_trip_trades()
        assert "split_rekey" not in row


def test_an_account_without_a_split_basis_never_rekeys():
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.set_clock(_dt(SPLIT))
    acct = BacktestAccount.__new__(BacktestAccount)
    acct._split_basis = None
    acct._options = _Chain({})
    acct._option_positions = {}
    acct._price = ps
    assert acct.apply_split_rekeys() == 0


def test_the_engine_bar_head_runs_the_rekey_pass():
    """daily_engine calls the pass right after the clock moves, before any expert reads the
    book -- so the ex-date's marks, quotes and exit rules all see the adjusted contract."""
    import inspect
    from app.services.backtest import daily_engine
    src = inspect.getsource(daily_engine.DailyBacktestEngine.run)
    clock = src.index("self.price.set_clock(as_of_dt)")
    rekey = src.index("apply_split_rekeys()")
    experts = src.index("for expert, expert_id, settings, ruleset_id in self.experts")
    assert clock < rekey < experts


# =============================================================================================
# 8. (Task 1a review) a MARKET entry decided before the split, cancelled on the ex-date,
#    releases its WAITING transaction
# =============================================================================================
def test_a_market_entry_cancelled_across_the_split_releases_its_waiting_transaction():
    from ba2_common.core.models import Transaction
    from ba2_common.core.trade_store import transactions_where
    bars = {(PUT410, d): _bar(300.5) for d in _sessions() if d >= SPLIT}
    with _harness(bars, _closes(), cfg=NBO) as (engine, acct, ps):
        ps.set_clock(_dt(LAST_PRE))
        acct.submit_option_order(legs=[_leg(PUT410, OptionRight.PUT, 410.0, OrderDirection.BUY)],
                                 quantity=1, order_type="market", option_strategy="long_put")
        acct.refresh_orders()
        acct.refresh_transactions()
        (order,) = _rows(acct, PUT410)
        txn_id = order.transaction_id
        assert txn_id is not None
        (txn,) = [t for t in transactions_where() if t.id == txn_id]
        assert txn.status == TransactionStatus.WAITING

        ps.set_clock(_dt(SPLIT))
        changed = acct.refresh_orders()
        assert changed        # the engine rolls transactions on this signal
        acct.refresh_transactions()
        assert order.status == OrderStatus.CANCELED
        # Released: the shared lifecycle closes a never-opened transaction whose entries are all
        # terminal, and its never_opened_cleanup deletes the row -- either way nothing WAITING
        # is left to lock AAPL out through the engine's dup gate.
        left = [t for t in transactions_where() if t.id == txn_id]
        assert all(t.status != TransactionStatus.WAITING for t in left)
        assert not [t for t in transactions_where(status=TransactionStatus.WAITING)]
        assert PUT410 not in acct._option_positions
