"""OPT-B4 — an option LIMIT order is a DAY order; it must not rest for the contract's life.

Live forces ``TimeInForce.DAY`` on every option order (``AlpacaAccount``), and all 17 option
entry builders submit ``order_type="limit"`` (``TradeActions``). The simulator had no TIF and
no age handling anywhere in ``backtest_account.py`` / ``daily_engine.py``, so a limit that the
premium never crossed stayed ACCEPTED for the whole life of the contract:

  * it kept its ``option_reserve`` charged against buying power
    (``OptionsAccountInterface.reserved_option_buying_power_detail``), and
  * for the pure-option families it locked the symbol out of the rest of the run via the
    engine's WAITING dup gate (``daily_engine``), because the parent's Transaction sat WAITING
    forever, and
  * it could still fill weeks later at a price the strategy quoted on a different bar.

That let the GA quote aggressively and never pay for the misses — it changes WHICH TRADES
EXIST, the worst distortion class for a fitness function.

Scope, stated on purpose: the rule applies to LIMIT option orders. A MARKET option order that
does not fill in the simulator has not met a market refusal, it has met a MISSING PREMIUM BAR;
terminalising it would turn a data gap into a cancelled trade, which is a different (and
invented) fact. Equity orders are untouched — this is the option TIF, not a global one.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import (
    OptionRight, OrderDirection, OrderStatus, TransactionStatus,
)


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_C180 = "AAPL240315C00180000"
_C190 = "AAPL240315C00190000"
_EXPIRY = date(2024, 3, 15)

_D0 = datetime(2024, 3, 5)     # submit bar; next_bar_open aims at 2024-03-06
_D1 = datetime(2024, 3, 6)     # the DAY the order was working for
_D2 = datetime(2024, 3, 7)     # the session after — a DAY order is gone by now
_D3 = datetime(2024, 3, 8)     # ...and the premium finally comes to the resting bid here

_AAPL_BARS = [
    {"Date": _D0, "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": _D1, "Open": 181, "High": 183, "Low": 180, "Close": 182, "Volume": 1000},
    {"Date": _D2, "Open": 182, "High": 184, "Low": 181, "Close": 183, "Volume": 1000},
    {"Date": _D3, "Open": 176, "High": 178, "Low": 175, "Close": 177, "Volume": 1000},
    {"Date": datetime(2024, 3, 11), "Open": 176, "High": 178, "Low": 175, "Close": 177,
     "Volume": 1000},
]

_TERMS = {_C180: ("call", 180.0), _C190: ("call", 190.0)}
# Premium bars on every later day, so a resting order genuinely COULD keep filling — the
# only thing stopping it must be the day-order rule, not a missing bar. The 2024-03-08 bar
# on the 180 call drops to 2.00, which the stale 3.00 bid WOULD have crossed.
_PREMIUMS = {
    (_C180, "2024-03-06"): 5.00, (_C190, "2024-03-06"): 2.00,
    (_C180, "2024-03-07"): 5.00, (_C190, "2024-03-07"): 2.00,
    (_C180, "2024-03-08"): 2.00,
}


def _seed_cache(db_path):
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows("AAPL", "2024-03-01", [
        {"occ_symbol": occ, "option_type": kind, "strike": k, "expiry": "2024-03-15",
         "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}
        for occ, (kind, k) in _TERMS.items()])
    cache.write_bar_rows([
        {"occ_symbol": occ, "date": day, "open": px, "high": px, "low": px, "close": px,
         "volume": 500, "underlying": "AAPL", "option_type": _TERMS[occ][0],
         "strike": _TERMS[occ][1], "expiry": "2024-03-15"}
        for (occ, day), px in _PREMIUMS.items()])


@pytest.fixture
def acct(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / "c.sqlite")
    _seed_cache(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db("optday")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(_D0)
    a = BacktestAccount(1, ps, CFG, options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(1, a)
    try:
        yield a, ps
    finally:
        ctx.__exit__(None, None, None)


def _leg(occ, side, intent="buy_to_open"):
    from ba2_common.core.option_types import OptionLeg

    kind, k = _TERMS[occ]
    return OptionLeg(contract_symbol=occ, side=side, position_intent=intent,
                     option_type=OptionRight.CALL if kind == "call" else OptionRight.PUT,
                     strike=k, expiry=_EXPIRY, underlying="AAPL")


def _open_txns(acct):
    from ba2_common.core.trade_store import transactions_where

    return transactions_where(statuses=[TransactionStatus.WAITING, TransactionStatus.OPENED])


# =========================================================================== #
# single leg
# =========================================================================== #
def test_an_unfilled_single_leg_limit_is_gone_after_its_session(acct):
    """A 3.00 bid on a 5.00 call never crosses. It gets ONE session, then it is over."""
    a, ps = acct
    parent = a.submit_option_order(legs=[_leg(_C180, OrderDirection.BUY)], quantity=1,
                                   order_type="limit", limit_price=3.00,
                                   option_strategy="long_call")
    a.refresh_orders()                       # its own session: aims at the 2024-03-06 bar
    assert a.get_order(parent.id).status == OrderStatus.ACCEPTED, \
        "the order must get its full session before the day rule can touch it"

    ps.set_clock(_D1)
    a.refresh_orders()                       # the next session — the DAY order is done

    o = a.get_order(parent.id)
    assert o.status in OrderStatus.get_terminal_statuses(), (
        f"the limit is still {o.status} a session after it was placed. Live forces "
        f"TimeInForce.DAY; here it rests at a stale price for the contract's whole life, "
        f"keeps its reserve charged, and can still fill weeks later (OPT-B4)."
    )
    assert not (o.filled_qty or 0)
    assert a.get_option_positions() == []


def test_the_expired_order_cannot_fill_on_a_later_bar(acct):
    """The point of terminalising it: a price it quoted on Tuesday must not trade Friday."""
    a, ps = acct
    a.submit_option_order(legs=[_leg(_C180, OrderDirection.BUY)], quantity=1,
                          order_type="limit", limit_price=3.00, option_strategy="long_call")
    a.refresh_orders()
    ps.set_clock(_D1)
    a.refresh_orders()
    cash_after_expiry = a._cash

    ps.set_clock(_D2)                        # next_bar_open -> the 2024-03-08 premium, 2.00
    a.refresh_orders()
    a.refresh_transactions()

    assert a.get_option_positions() == [], (
        "the order the strategy placed on 2024-03-05 traded on 2024-03-08 at a price it "
        "quoted three sessions earlier"
    )
    assert a._cash == pytest.approx(cash_after_expiry)


def test_the_symbol_is_free_again_after_the_order_expires(acct):
    """The parent's Transaction is what the engine's WAITING dup gate reads. While it sat
    WAITING forever, the whole run could never take another position in that name."""
    a, ps = acct
    a.submit_option_order(legs=[_leg(_C180, OrderDirection.BUY)], quantity=1,
                          order_type="limit", limit_price=3.00, option_strategy="long_call")
    a.refresh_orders()
    a.refresh_transactions()
    assert _open_txns(a), "precondition: the unfilled entry does hold a WAITING transaction"

    ps.set_clock(_D1)
    a.refresh_orders()
    a.refresh_transactions()

    assert _open_txns(a) == [], (
        "the expired entry still holds a WAITING transaction — the symbol stays locked out "
        "of the rest of the run"
    )


def test_refresh_orders_signals_the_roll_on_an_expiry_with_no_fill(acct):
    """F1 (option-grid probe 2026-08-27): the engine runs the transaction roll ONLY when
    refresh_orders() reports a change. An option DAY-limit that ages out terminalises its
    entry order with NO fill — if refresh_orders() stays falsy the roll is skipped, the
    parent Transaction stays WAITING, and the dup gate locks the symbol for the whole run
    (measured: 146 consecutive entry skips, June → December, from ONE expired order).

    This test mimics the engine's gate verbatim: roll if and only if the signal is truthy.
    The account-level test above calls refresh_transactions() unconditionally, which is
    exactly why it could not catch this.
    """
    a, ps = acct
    a.submit_option_order(legs=[_leg(_C180, OrderDirection.BUY)], quantity=1,
                          order_type="limit", limit_price=3.00, option_strategy="long_call")
    if a.refresh_orders():          # the engine's gate, verbatim
        a.refresh_transactions()
    assert _open_txns(a), "precondition: the unfilled entry does hold a WAITING transaction"

    ps.set_clock(_D1)               # the session after — the DAY order ages out here
    changed = a.refresh_orders()
    assert changed, (
        "the expiry of an unfilled DAY-limit must be reported as a book change: it is the "
        "ONLY event on this bar, and the transaction roll it gates is what releases the "
        "WAITING Transaction"
    )
    if changed:
        a.refresh_transactions()

    assert _open_txns(a) == [], (
        "under the engine's own gating the expired entry still holds a WAITING transaction"
    )


def test_a_limit_that_crosses_in_its_own_session_is_untouched(acct):
    """The rule ages orders out; it must not cancel one that traded."""
    a, ps = acct
    parent = a.submit_option_order(legs=[_leg(_C180, OrderDirection.BUY)], quantity=1,
                                   order_type="limit", limit_price=6.00,
                                   option_strategy="long_call")
    a.refresh_orders()
    a.refresh_transactions()

    assert a.get_order(parent.id).status == OrderStatus.FILLED
    assert len(a.get_option_positions()) == 1

    ps.set_clock(_D1)
    a.refresh_orders()
    assert len(a.get_option_positions()) == 1     # still held, not aged out


def test_a_market_option_order_is_not_aged_out(acct):
    """Scope guard. A market option order that did not fill met a MISSING BAR, not a market
    refusal; expiring it would invent a cancellation out of a data gap."""
    a, ps = acct
    parent = a.submit_option_order(legs=[_leg("AAPL240315C00180000", OrderDirection.BUY)],
                                   quantity=1, order_type="market",
                                   option_strategy="long_call")
    # Fill it on its own bar, then confirm a SECOND market order placed with no reachable
    # bar survives into the next session.
    a.refresh_orders()
    assert a.get_order(parent.id).status == OrderStatus.FILLED

    ps.set_clock(_D2)     # next_bar_open from 03-07 -> 03-08; the 190 call has no bar there
    late = a.submit_option_order(legs=[_leg(_C190, OrderDirection.SELL, "sell_to_open")],
                                 quantity=1, order_type="market",
                                 option_strategy="naked_call")
    a.refresh_orders()
    assert a.get_order(late.id).status == OrderStatus.ACCEPTED
    ps.set_clock(_D3)
    a.refresh_orders()
    assert a.get_order(late.id).status == OrderStatus.ACCEPTED, \
        "a MARKET option order must not be aged out by the LIMIT day rule"


# =========================================================================== #
# multi-leg
# =========================================================================== #
def test_an_unfilled_combo_takes_its_legs_down_with_it(acct):
    """A stranded child leg is the OPT-S1 shape: the parent dies and the legs live on."""
    a, ps = acct
    parent = a.submit_option_order(
        legs=[_leg(_C180, OrderDirection.BUY), _leg(_C190, OrderDirection.SELL, "sell_to_open")],
        quantity=1, order_type="limit", limit_price=1.00,   # net is 3.00; 1.00 never clears
        option_strategy="bull_call_spread")
    a.refresh_orders()
    assert a.get_order(parent.id).status == OrderStatus.ACCEPTED

    ps.set_clock(_D1)
    a.refresh_orders()

    terminal = OrderStatus.get_terminal_statuses()
    rows = [o for o in a.get_orders()
            if o.id == parent.id or o.parent_order_id == parent.id]
    assert len(rows) == 3
    assert all(o.status in terminal for o in rows), (
        f"combo rows left working after their session: "
        f"{[(o.contract_symbol, o.status) for o in rows if o.status not in terminal]}"
    )
    assert a.get_option_positions() == []


def test_an_expired_credit_combo_stops_reserving_buying_power(acct):
    """A reserve belongs to a POSITION. An order that never traded holds no position."""
    from ba2_common.core.db import update_instance

    a, ps = acct
    parent = a.submit_option_order(
        legs=[_leg(_C180, OrderDirection.SELL, "sell_to_open"),
              _leg(_C190, OrderDirection.BUY)],
        quantity=1, order_type="limit", limit_price=-9.00,  # net credit is 3.00; never clears
        option_strategy="bear_call_spread")
    # A reserve is persisted on the parent by TradeActions._submit_option_order for every
    # short-premium structure; stamp it the same way so the pool has something to release.
    parent.data = {**(parent.data or {}), "option_reserve": 1_000.0}
    update_instance(parent)
    a.invalidate_order_cache()
    a.refresh_orders()
    assert a.reserved_option_buying_power() == pytest.approx(1_000.0), \
        "precondition: the working order does reserve buying power"

    ps.set_clock(_D1)
    a.refresh_orders()

    assert a.get_option_positions() == []
    assert a.reserved_option_buying_power() == pytest.approx(0.0), (
        "an order that never filled is still consuming buying power for the rest of the run"
    )
