"""Follow-ups of the option fill-print screen (review 2026-10-06):

* a CLOSE is never stuck or refused by the print screen or the structure bounds (it falls back
  through the mark chain and is counted);
* a market-type OPENING entry the screen refuses expires like a DAY order, once;
* a replaced price is clamped against the contract's own session range (a buy never below the
  day's low, a sell never above its high) and is built from the DECISION bar's iv alone -- the
  fill day's close-implied iv only decides accept / reject;
* the owner of a held contract is decided by what was FILLED (a cancelled-after-partial opening
  order still holds its contracts) and "latest" is explicit (fill time, then id);
* with no print rejected the whole run is bit-identical to a run with the screen off.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, OrderStatus
from tests.backtest._structure_engine import (
    SESSIONS, build_account, model_price, occ, run_structure)

EXPIRY = date(2024, 3, 15)
FILL_DAY = "2024-02-02"
DECISION_DAY = "2024-02-01"


def _leg(sym, strike, side, intent, right=OptionRight.CALL):
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                     option_type=right, strike=strike, expiry=EXPIRY, underlying="AAPL")


def _published(acct):
    from app.services.backtest.results import build_results
    from tests.backtest._structure_engine import BASE_CFG
    acct.snapshot_equity(datetime(2024, 2, 9, tzinfo=timezone.utc))
    return build_results(acct, {"initial_capital": BASE_CFG["starting_cash"],
                                "account_settings": dict(BASE_CFG), "option_trade_records": True})


def _hold_long_call(acct, ps, sym, strike, qty=2):
    acct.submit_option_order(legs=[_leg(sym, strike, OrderDirection.BUY, "buy_to_open")],
                             quantity=qty, order_type="market", option_strategy="long_call")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert acct._option_positions[sym].qty == qty
    ps.set_clock(datetime(2024, 2, 5))


# ----------------------------------------------------------------------------------------
# item 5 -- closes are never stuck
# ----------------------------------------------------------------------------------------
def test_a_close_with_a_rejected_print_and_no_decision_iv_falls_back_to_intrinsic_and_counts():
    """The garbage open is rejected (the close-implied iv says ~12), the decision bar carries no
    iv to reprice from. An OPEN would be refused; a CLOSE falls back to the mark chain's next
    stage, intrinsic at the underlying's open (10.00 for the 170 call at 180), and fills."""
    sym = occ("C", 170.0)
    acct, ps, ctx, _ = build_account(overrides={
        (sym, "2024-02-06"): {"open": 0.10},            # garbage open on the closing session
        (sym, "2024-02-05"): {"iv": None},               # decision bar of the close: no iv
    })
    try:
        _hold_long_call(acct, ps, sym, 170.0)
        closing = acct.submit_option_order(
            legs=[_leg(sym, 170.0, OrderDirection.SELL, "sell_to_close")], quantity=2,
            order_type="market", option_strategy="close")
        acct.refresh_orders()
        closed = acct.get_order(closing.id)
        assert closed.status == OrderStatus.FILLED
        assert closed.open_price == pytest.approx(10.0)
        stats = acct.option_integrity_stats()
        assert stats["option_close_prints_fallback"] == 1
        assert stats["option_fill_prints_replaced"] == 1 and stats["option_fill_prints_refused"] == 0
    finally:
        ctx.__exit__(None, None, None)


def test_a_closing_structure_whose_net_breaks_the_bounds_is_repriced_not_refused():
    """Closing a bull call spread: sell the 180 call, buy back the 185 call. The prints (12.00 /
    5.00, no iv anywhere to judge them) collect 7.00 > the 5.00 width -- outside the structure's
    bounds. An opening order would be refused; the close is repriced through the mark chain
    (intrinsic at the open: 0.00 / 0.00 at a spot of 180), filled and counted."""
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    overrides = {}
    for d in SESSIONS:
        for sym in (long_call, short_call):
            overrides[(sym, d.isoformat())] = {"iv": None}
    overrides[(long_call, "2024-02-06")] = {"open": 12.0, "close": 12.0, "iv": None}
    overrides[(short_call, "2024-02-06")] = {"open": 5.0, "close": 5.0, "iv": None}
    acct, ps, ctx, _ = build_account(overrides=overrides)
    try:
        acct.submit_option_order(
            legs=[_leg(long_call, 180.0, OrderDirection.BUY, "buy_to_open"),
                  _leg(short_call, 185.0, OrderDirection.SELL, "sell_to_open")],
            quantity=1, order_type="market", option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[long_call].qty == 1
        ps.set_clock(datetime(2024, 2, 5))
        closing = acct.submit_option_order(
            legs=[_leg(long_call, 180.0, OrderDirection.SELL, "sell_to_close"),
                  _leg(short_call, 185.0, OrderDirection.BUY, "buy_to_close")],
            quantity=1, order_type="market", option_strategy="close")
        acct.refresh_orders()
        assert acct.get_order(closing.id).status == OrderStatus.FILLED
        stats = acct.option_integrity_stats()
        assert stats["option_close_structure_fallbacks"] == 1
        assert stats["option_structure_fills_refused"] == 0
        assert acct._option_positions.get(long_call) is None or acct._option_positions[long_call].qty == 0
    finally:
        ctx.__exit__(None, None, None)


def test_a_refused_market_type_opening_entry_expires_once_and_does_not_retry_every_bar():
    """A market-type BUY whose open print is rejected with no iv to reprice from is REFUSED. The
    DAY sweep never ages a market order, so it used to retry every bar on a stale decision; it
    is now terminalised the first time (counted once) and never fills later."""
    sym = occ("C", 180.0)
    acct, ps, ctx, _ = build_account(overrides={
        (sym, FILL_DAY): {"open": 1.0},
        (sym, DECISION_DAY): {"iv": None}})
    try:
        order = acct.submit_option_order(
            legs=[_leg(sym, 180.0, OrderDirection.BUY, "buy_to_open")], quantity=2,
            order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        order = acct.get_order(order.id)
        assert order.status == OrderStatus.EXPIRED
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_refused"] == 1
        assert stats["option_market_entries_expired"] == 1
        for day in (datetime(2024, 2, 5), datetime(2024, 2, 6)):    # later sessions: no retry
            ps.set_clock(day)
            acct.refresh_orders()
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_refused"] == 1 and stats["option_market_entries_expired"] == 1
        assert acct.get_order(order.id).status == OrderStatus.EXPIRED
        assert acct._option_positions.get(sym) is None
    finally:
        ctx.__exit__(None, None, None)


def test_a_market_type_structure_refused_for_its_net_expires_parent_and_legs():
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    overrides = {}
    for d in SESSIONS:
        for sym in (long_call, short_call):
            overrides[(sym, d.isoformat())] = {"iv": None}
    overrides[(long_call, FILL_DAY)] = {"open": 3.0, "close": 3.0, "iv": None}
    overrides[(short_call, FILL_DAY)] = {"open": 5.0, "close": 5.0, "iv": None}   # net debit -2
    acct, ps, ctx, _ = build_account(overrides=overrides)
    try:
        parent = acct.submit_option_order(
            legs=[_leg(long_call, 180.0, OrderDirection.BUY, "buy_to_open"),
                  _leg(short_call, 185.0, OrderDirection.SELL, "sell_to_open")],
            quantity=1, order_type="market", option_strategy="bull_call_spread")
        acct.refresh_orders()
        assert acct.get_order(parent.id).status == OrderStatus.EXPIRED
        legs = [o for o in acct.get_orders() if o.parent_order_id == parent.id]
        assert legs and all(o.status == OrderStatus.EXPIRED for o in legs)
        stats = acct.option_integrity_stats()
        assert stats["option_structure_fills_refused"] == 1
        assert stats["option_market_entries_expired"] == 1
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# item 6 -- conservative replacement
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("price,is_buy,bar,expected", [
    (6.5, True, {"low": 7.0, "high": 11.0}, 7.0),       # a buy never fills below the day's low
    (8.0, True, {"low": 7.0, "high": 11.0}, 8.0),       # inside the range: untouched
    (12.0, True, {"low": 7.0, "high": 11.0}, 12.0),     # model above the range: stays adverse
    (12.0, False, {"low": 7.0, "high": 11.0}, 11.0),    # a sell never fills above the day's high
    (9.0, False, {"low": 7.0, "high": 11.0}, 9.0),
    (6.0, False, {"low": 7.0, "high": 11.0}, 6.0),      # model below the range: stays adverse
])
def test_the_replacement_is_clamped_against_the_contracts_own_session(price, is_buy, bar, expected):
    from app.services.backtest.backtest_account import BacktestAccount
    assert BacktestAccount._adverse_clamp(price, is_buy, bar) == expected


@pytest.mark.parametrize("bar", [{}, {"low": None, "high": 5.0}, {"low": 5.0, "high": 4.0},
                                 {"low": float("nan"), "high": 5.0}, {"low": "x", "high": 5.0}])
def test_no_usable_session_range_is_no_clamp(bar):
    from app.services.backtest.backtest_account import BacktestAccount
    assert BacktestAccount._adverse_clamp(6.0, True, bar) is None


def test_a_garbage_high_buy_is_replaced_but_never_below_the_days_low():
    """The BUY's open is garbage-high (11.00). The decision-iv reference says 6.50, but the
    contract never traded below 6.60 that session (under the entry's 6.63 limit), so the buy
    fills at 6.60."""
    sym = occ("C", 180.0)
    _, acct, ctx, _res = run_structure(
        "buy_call", strike_method="percent_otm", strike_param=0.0,
        overrides={(sym, FILL_DAY): {"open": 11.0, "close": 6.6, "low": 6.6, "high": 11.0}})
    try:
        order = next(o for o in acct.get_orders()
                     if o.contract_symbol == sym and o.status == OrderStatus.FILLED)
        assert order.open_price == pytest.approx(6.6)
        stats = acct.option_integrity_stats()
        assert stats["option_fill_prints_replaced"] == 1 and stats["option_fills_at_replaced_print"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_an_opening_order_with_no_usable_session_range_is_refused(monkeypatch):
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_adverse_clamp", staticmethod(lambda p, b, bar: None))
    sym = occ("C", 180.0)
    acct, ps, ctx, _ = build_account(overrides={(sym, FILL_DAY): {"open": 1.0}})
    try:
        order = acct.submit_option_order(
            legs=[_leg(sym, 180.0, OrderDirection.BUY, "buy_to_open")], quantity=2,
            order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        assert acct.get_order(order.id).status == OrderStatus.EXPIRED
        assert acct.option_integrity_stats()["option_fill_prints_refused"] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_a_close_with_no_usable_session_range_takes_the_unclamped_fallback(monkeypatch):
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_adverse_clamp", staticmethod(lambda p, b, bar: None))
    sym = occ("C", 180.0)
    acct, ps, ctx, _ = build_account(overrides={(sym, "2024-02-06"): {"open": 0.10}})
    try:
        _hold_long_call(acct, ps, sym, 180.0)
        closing = acct.submit_option_order(
            legs=[_leg(sym, 180.0, OrderDirection.SELL, "sell_to_close")], quantity=2,
            order_type="market", option_strategy="close")
        acct.refresh_orders()
        closed = acct.get_order(closing.id)
        assert closed.status == OrderStatus.FILLED
        assert closed.open_price == pytest.approx(model_price("C", 180.0, date(2024, 2, 6)), abs=0.05)
    finally:
        ctx.__exit__(None, None, None)


def test_the_close_implied_iv_decides_accept_or_reject_but_never_prices_the_replacement():
    """The fill-day close prints 7.50 with an iv of 0.29 (BS at the open's spot would be ~7.5).
    The garbage open is rejected, and the replacement is BS at the DECISION bar's iv (0.25,
    ~6.5), unmoved by the close-implied iv, which only decided accept / reject."""
    from app.services.backtest import backtest_account as ba
    sym = occ("C", 180.0)
    _, acct, ctx, _res = run_structure(
        "buy_call", strike_method="percent_otm", strike_param=0.0,
        overrides={(sym, FILL_DAY): {"open": 1.0, "close": 7.5, "iv": 0.29, "low": 1.0,
                                     "high": 8.0}})
    try:
        order = next(o for o in acct.get_orders()
                     if o.contract_symbol == sym and o.status == OrderStatus.FILLED)
        decision_ref = model_price("C", 180.0, date(2024, 2, 2))
        close_ref = ba.bs_price(180.0, 180.0, 42, 0.29, OptionRight.CALL, r=0.04)
        assert order.open_price == pytest.approx(decision_ref, abs=0.05)
        assert abs(order.open_price - close_ref) > 0.5
    finally:
        ctx.__exit__(None, None, None)


def test_a_buy_back_with_no_iv_and_no_range_keeps_its_time_value(monkeypatch):
    """Closing a SHORT call: buy_to_close. The garbage print (0.05) is rejected, the decision bar has
    no iv to reprice from and the session has no usable range. Intrinsic (0.00 at a spot of 180)
    would hand the buy-back its time value for free; the fallback is the LAST TRADED price (the
    decision bar's close, ~3.9) -- never below intrinsic -- and is counted."""
    from app.services.backtest.backtest_account import BacktestAccount
    monkeypatch.setattr(BacktestAccount, "_adverse_clamp", staticmethod(lambda p, b, bar: None))
    sym = occ("C", 185.0)
    acct, ps, ctx, _ = build_account(overrides={
        (sym, "2024-02-06"): {"open": 0.05},
        (sym, "2024-02-05"): {"iv": None}})
    try:
        acct.submit_option_order(
            legs=[_leg(sym, 185.0, OrderDirection.SELL, "sell_to_open")], quantity=2,
            order_type="market", option_strategy="naked_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[sym].qty == -2
        ps.set_clock(datetime(2024, 2, 5))
        closing = acct.submit_option_order(
            legs=[_leg(sym, 185.0, OrderDirection.BUY, "buy_to_close")], quantity=2,
            order_type="market", option_strategy="close")
        acct.refresh_orders()
        closed = acct.get_order(closing.id)
        assert closed.status == OrderStatus.FILLED
        last_traded = model_price("C", 185.0, date(2024, 2, 5))
        assert closed.open_price == pytest.approx(last_traded, abs=0.01)
        assert closed.open_price > 3.0                      # not the 0.00 intrinsic
        stats = acct.option_integrity_stats()
        assert stats["option_close_buyback_last_price"] == 1
        assert stats["option_close_prints_fallback"] == 1
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# item 9 -- owner selection
# ----------------------------------------------------------------------------------------
def test_owner_rank_is_decided_by_filled_quantity_not_by_status():
    from app.services.backtest.backtest_account import (
        _OWNER_CLOSING, _OWNER_DEAD, _OWNER_FILLED_OPEN, _OWNER_PENDING_OPEN, _owner_rank)

    def order(**kw):
        base = dict(position_intent="buy_to_open", status=OrderStatus.FILLED, filled_qty=1.0)
        base.update(kw)
        return SimpleNamespace(**base)

    assert _owner_rank(order()) == _OWNER_FILLED_OPEN
    # partially filled, then CANCELED: the contracts that printed are held
    assert _owner_rank(order(status=OrderStatus.CANCELED, filled_qty=3.0)) == _OWNER_FILLED_OPEN
    assert _owner_rank(order(status=OrderStatus.EXPIRED, filled_qty=1.0)) == _OWNER_FILLED_OPEN
    # filled nothing: dead / closing / pending, by status
    assert _owner_rank(order(status=OrderStatus.EXPIRED, filled_qty=None)) == _OWNER_DEAD
    assert _owner_rank(order(status=OrderStatus.CANCELED, filled_qty=0.0)) == _OWNER_DEAD
    assert _owner_rank(order(position_intent="sell_to_close", status=OrderStatus.PENDING,
                             filled_qty=None)) == _OWNER_CLOSING
    assert _owner_rank(order(status=OrderStatus.PENDING, filled_qty=None)) == _OWNER_PENDING_OPEN
    # a CLOSE that filled is not an opening claim
    assert _owner_rank(order(position_intent="sell_to_close", filled_qty=2.0)) == _OWNER_CLOSING


def _open_close_reopen(acct, ps, long_call, short_call):
    """A, held and closed; then B on the SAME contracts. Returns (A_parent, B_parent)."""
    def open_vertical():
        return acct.submit_option_order(
            legs=[_leg(long_call, 180.0, OrderDirection.BUY, "buy_to_open"),
                  _leg(short_call, 185.0, OrderDirection.SELL, "sell_to_open")],
            quantity=1, order_type="market", option_strategy="bull_call_spread")

    a = open_vertical()
    acct.refresh_orders()
    acct.refresh_transactions()
    ps.set_clock(datetime(2024, 2, 5))
    acct.submit_option_order(
        legs=[_leg(long_call, 180.0, OrderDirection.SELL, "sell_to_close"),
              _leg(short_call, 185.0, OrderDirection.BUY, "buy_to_close")],
        quantity=1, order_type="market", option_strategy="close")
    acct.refresh_orders()
    acct.refresh_transactions()
    ps.set_clock(datetime(2024, 2, 6))
    b = open_vertical()
    acct.refresh_orders()
    acct.refresh_transactions()
    return a, b


def test_the_owner_of_a_reopened_contract_is_the_latest_fill_whatever_the_book_order(monkeypatch):
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    acct, ps, ctx, _ = build_account()
    try:
        a, b = _open_close_reopen(acct, ps, long_call, short_call)
        assert acct._option_positions[long_call].qty == 1
        contract_group, group_bounds = acct._option_group_bounds()
        assert set(contract_group.values()) == {b.id}
        # the answer does not depend on how get_orders() happens to order the book
        original = acct.get_orders
        monkeypatch.setattr(acct, "get_orders", lambda *a_, **k: list(reversed(original(*a_, **k))))
        acct.invalidate_order_cache()
        acct._bump_option_memo()
        contract_group, _ = acct._option_group_bounds()
        assert set(contract_group.values()) == {b.id}
    finally:
        ctx.__exit__(None, None, None)


def test_a_partially_filled_opening_leg_cancelled_afterwards_still_holds_its_contracts():
    """The long leg of the held structure ends CANCELED but filled; the group must still derive
    its bounds from all its legs instead of falling back."""
    long_call, short_call = occ("C", 180.0), occ("C", 185.0)
    acct, ps, ctx, _ = build_account()
    try:
        acct.submit_option_order(
            legs=[_leg(long_call, 180.0, OrderDirection.BUY, "buy_to_open"),
                  _leg(short_call, 185.0, OrderDirection.SELL, "sell_to_open")],
            quantity=1, order_type="market", option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()
        child = next(o for o in acct.get_orders() if o.contract_symbol == long_call)
        child.status = OrderStatus.CANCELED          # filled_qty stays 1: a cancel does not un-trade
        acct.invalidate_order_cache()
        acct._bump_option_memo()
        _, group_bounds = acct._option_group_bounds()
        (gb,) = group_bounds.values()
        assert gb["lo"] is not None and gb["hi"] is not None
        assert acct.option_integrity_stats()["option_clamp_fallbacks"] == 0
    finally:
        ctx.__exit__(None, None, None)


# ----------------------------------------------------------------------------------------
# item 10 -- bit-identical when nothing is rejected
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("action,kw", [
    ("open_bull_call_spread", {}),
    ("buy_call", {"strike_method": "percent_otm", "strike_param": 0.0}),
    ("open_iron_condor", {}),
])
def test_a_run_with_no_rejected_print_is_bit_identical_to_a_run_with_the_screen_off(
        action, kw, monkeypatch):
    from app.services.backtest.backtest_account import BacktestAccount

    def snapshot():
        _, acct, ctx, _res = run_structure(action, **kw)
        try:
            res = _published(acct)
            stats = acct.option_integrity_stats()
            return {k: res[k] for k in ("equity_curve", "drawdown_curve", "trades", "final_equity",
                                       "total_return", "total_trades")}, stats
        finally:
            ctx.__exit__(None, None, None)

    screened, stats = snapshot()
    assert stats["option_fill_prints_rejected"] == 0 and stats["option_structure_fills_refused"] == 0
    assert screened["total_trades"] >= 0 and screened["equity_curve"]

    monkeypatch.setattr(BacktestAccount, "_screen_open_print",
                        lambda self, order, px, *a, **k: px)
    monkeypatch.setattr(BacktestAccount, "_structure_net_bound_reason", lambda self, *a, **k: None)
    unscreened, _ = snapshot()
    assert screened == unscreened
