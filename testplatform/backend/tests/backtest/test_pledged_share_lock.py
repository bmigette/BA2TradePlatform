"""OPT-L1, THE SIMULATOR'S EXIT HALF — a broker LOCKS the shares that cover a short call.

What the backtest did
---------------------
In reality the shares collateralising a written call are pledged: while the covered call is
open the broker will not let you sell them, because that would leave a NAKED short call with
unbounded upside risk. ``BacktestAccount`` modelled no such lock. An equity exit rule — O_CC
carries a staged trailing stop plus a time exit — sold the shares out from under the call and
nothing said a word.

MEASURED, on O_CC / GOOG,BAC,INTC,F,T / 2023-01-10..2023-03-28 / $100k / gates-off / 1d, by
instrumenting ``_covered_short_call_contracts``::

    BAC230303C00037000   shares_held=  4   shares_needed=200   (40 bars)
    INTC230303C00030000  shares_held=  2   shares_needed=200   (27 bars)
    BAC230210C00036000   shares_held=  4   shares_needed=200   (15 bars)
    BAC230414C00034000   shares_held=  4   shares_needed=200   (15 bars)

Four genuinely naked short calls, 4 shares against 200 needed, carried for up to 40 bars. The
GA arms that sell premium (O_CC / O_PP / the wheel) can therefore book premium against a risk
profile no broker would permit, which in a benign window reads as free money — and a GA is
exactly the machine that finds and breeds toward it.

The ENTRY half of this guard was already live in the simulator
(``submit_option_order`` refuses a ``covered_call`` whose cover is short — see
``test_options_review_fixes.py::test_the_entry_seam_refuses_a_partially_covered_call_in_the_
backtest_too``), and the LIVE exit half exists too
(``AccountInterface.cover_refusal_for_equity_sale``, reached from ``close_transaction``).
What was missing was the fill-time lock that covers the paths neither of those sees: the
bracket TP/SL exit, which synthesises and fills its own close order, and the assignment
orphan liquidation.

What the fix does
-----------------
CLAMP, never blanket-refuse: a broker lets you sell the UNPLEDGED EXCESS. 297 held against
200 pledged sells 97; 200 against 200 sells nothing and the order is CANCELED. Only SHORT
CALL lots pledge shares (short puts pledge CASH; long options pledge nothing), and an
unresolvable short-call lot makes the pledge UNMEASURABLE and refuses the sale outright —
"we could not find out" must never read as "nothing is pledged".

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_pledged_share_lock.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import (
    OptionRight,
    OrderDirection,
    OrderStatus,
    OrderType,
)


CFG = {
    "starting_cash": 200_000.0,
    "commission_per_trade": 0.0,   # zero so cash/qty assertions are exact
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

_CALL_200 = "AAPL240315C00200000"   # the pledging short call
_CALL_210 = "AAPL240315C00210000"   # a second short call on the same underlying
_PUT_170 = "AAPL240315P00170000"    # a short PUT — pledges cash, not shares
_MSFT_CALL = "MSFT240315C00400000"  # a short call on an UNRELATED ticker

_AAPL_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1100},
    {"Date": datetime(2024, 3, 8), "Open": 180, "High": 181, "Low": 179, "Close": 180, "Volume": 1200},
]
_MSFT_BARS = [
    {"Date": datetime(2024, 3, 5), "Open": 400, "High": 401, "Low": 399, "Close": 400, "Volume": 1000},
    {"Date": datetime(2024, 3, 6), "Open": 400, "High": 401, "Low": 399, "Close": 400, "Volume": 1100},
    {"Date": datetime(2024, 3, 8), "Open": 400, "High": 401, "Low": 399, "Close": 400, "Volume": 1200},
]


# --------------------------------------------------------------------------- #
# Harness — the terse pledge-arithmetic shape from test_options_review_fixes.py
# --------------------------------------------------------------------------- #
def _chain(sym, k, ot="call", underlying="AAPL"):
    return {"occ_symbol": sym, "option_type": ot, "strike": k, "expiry": "2024-03-15",
            "bid": 2.0, "ask": 2.2, "last": 2.1, "iv": 0.25}


def _bar(sym, d, close, ot, k, underlying="AAPL"):
    return {"occ_symbol": sym, "date": d, "open": close, "high": close, "low": close,
            "close": close, "volume": 100, "underlying": underlying,
            "option_type": ot, "strike": k, "expiry": "2024-03-15"}


def _leg(sym, ot, k, underlying="AAPL", side=OrderDirection.SELL):
    from ba2_common.core.option_types import OptionLeg

    intent = "buy_to_open" if side == OrderDirection.BUY else "sell_to_open"
    return OptionLeg(contract_symbol=sym, side=side, ratio_qty=1, position_intent=intent,
                     option_type=ot, strike=k, expiry=date(2024, 3, 15), underlying=underlying)


def _account(tmp_path, tag, *, chains, bars, symbols=("AAPL",)):
    """A BacktestAccount over a seeded options cache, clocked at 2024-03-05."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / "c.sqlite")
    cache = OptionsHistoryCache(cache_db)
    for underlying, rows in chains.items():
        cache.write_chain_rows(underlying, "2024-03-01", rows)
    if bars:
        cache.write_bar_rows(bars)
    prov = HistoricalOptionsProvider(cache_db)

    ps = AsOfPriceSource(ohlcv_provider=None)
    if "AAPL" in symbols:
        ps.load_bars("AAPL", _AAPL_BARS)
    if "MSFT" in symbols:
        ps.load_bars("MSFT", _MSFT_BARS)
    ps.set_clock(datetime(2024, 3, 5))

    wire_backtest_seams()
    ctx = backtest_trading_db(tag)
    ctx.__enter__()
    seed_account_definition(1, CFG)
    acct = BacktestAccount(1, ps, CFG, options_provider=prov)
    wire_backtest_seams().register_account(1, acct)
    return acct, ps, ctx


def _write_short(acct, sym, ot, strike, contracts, *, strategy, underlying="AAPL"):
    """Write ``contracts`` short options and fill them off the next bar."""
    acct.submit_option_order(
        legs=[_leg(sym, ot, strike, underlying=underlying)],
        quantity=contracts, order_type="market", option_strategy=strategy,
    )
    acct.refresh_orders()
    acct.refresh_transactions()


def _equity_sell(acct, symbol, qty, px, *, as_of=datetime(2024, 3, 6), transaction_id=None):
    """Fill a plain equity SELL through ``_apply_fill`` — the strategy-exit choke point.

    Returns the persisted order so the caller can read its terminal status / filled_qty.
    """
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder

    order = TradingOrder(
        account_id=acct.id, symbol=symbol, quantity=qty, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.NEW,
        transaction_id=transaction_id,
        broker_order_id=acct._next_broker_id(), comment="strategy-exit")
    add_instance(order)
    acct.invalidate_order_cache()
    persisted = acct.get_order(order.broker_order_id)
    acct._apply_fill(persisted, px, as_of)
    acct.invalidate_order_cache()
    return acct.get_order(order.broker_order_id)


def _shares(acct, symbol="AAPL"):
    pos = acct._positions.get(symbol)
    return 0.0 if pos is None else float(pos.qty)


# =========================================================================== #
# THE CLAMP — a sale may take the unpledged excess and nothing more
# =========================================================================== #
def test_a_sell_that_would_strip_cover_is_clamped_to_the_unpledged_excess(tmp_path):
    """297 held, 200 pledged to two short calls -> a 150-share exit sells 97 and stops.

    This is the shape the brief calls out as NOT a bug when it appears in the greedy
    ``_covered_short_call_contracts`` view (297-vs-200): the account genuinely owns 97
    shares no call has a claim on, and selling exactly those is what a broker permits.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-clamp",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 297, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 2, strategy="covered_call")
        assert acct._option_positions[_CALL_200].qty == -2

        order = _equity_sell(acct, "AAPL", 150, 190.0)

        assert _shares(acct) == pytest.approx(200.0), (
            "the exit must stop exactly on the pledged 200 — one share further and the "
            "written calls are naked")
        assert order.status == OrderStatus.FILLED
        assert float(order.filled_qty) == pytest.approx(97.0)
        assert float(order.quantity) == pytest.approx(97.0), (
            "the persisted quantity must match the clamped fill or refresh_transactions' "
            "net-filled arithmetic disagrees with the ledger")
    finally:
        ctx.__exit__(None, None, None)


def test_a_sell_with_no_unpledged_excess_is_canceled_not_filled_at_zero(tmp_path):
    """200 held, 200 pledged -> nothing may be sold: the order is CANCELED, shares unmoved.

    A zero-quantity FILL would be a lie in the trade ledger (a round trip that moved no
    stock) and would still stamp ``open_price``; the cash-secured precedent cancels in the
    same situation and so does this.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-cancel",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 200, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 2, strategy="covered_call")
        cash_before = acct._cash

        order = _equity_sell(acct, "AAPL", 200, 190.0)

        assert order.status == OrderStatus.CANCELED, (
            f"expected the fill to be refused outright, got {order.status}")
        assert _shares(acct) == pytest.approx(200.0), "pledged shares must not move"
        assert acct._cash == pytest.approx(cash_before), "a refused fill moves no cash"
        assert not order.filled_qty, "a canceled order has no fill"
    finally:
        ctx.__exit__(None, None, None)


def test_a_sale_of_shares_short_of_the_pledge_is_refused_not_partially_allowed(tmp_path):
    """The measured O_CC state: 4 shares held against 200 pledged.

    The pledge already EXCEEDS the holding (the cover was lost before this guard existed,
    or a partial assignment ate it), so there is no unpledged excess at all and the sale is
    refused. Selling the last 4 would deepen an already-naked call.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-shortfall",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 200, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 2, strategy="covered_call")
        # Cover lost the way it is lost in the wild: an assignment delivers 196 away.
        acct._book_assignment_share_leg("AAPL", -196.0, 200.0)
        assert _shares(acct) == pytest.approx(4.0)

        order = _equity_sell(acct, "AAPL", 4, 190.0)

        assert order.status == OrderStatus.CANCELED
        assert _shares(acct) == pytest.approx(4.0)
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# WHAT THE LOCK MUST NOT TOUCH
# =========================================================================== #
def test_the_assignment_delivery_is_never_blocked(tmp_path):
    """A called-away covered call delivers its 100 shares at 100-held / 100-pledged.

    The delivery is the BROKER taking the stock, not a discretionary sale, and it removes
    the call and the shares together — blocking it would deadlock the wheel, whose only
    exit IS being called away. This is why the guard lives in ``_apply_fill`` and never in
    the shared ``_update_position``.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-assign",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="covered_call")
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 100

        acct._book_assignment_share_leg("AAPL", -100.0, 200.0)

        assert _shares(acct) == pytest.approx(0.0), (
            "the assigned call must deliver the held shares — that IS the wheel's exit")
    finally:
        ctx.__exit__(None, None, None)


def test_a_short_sale_from_a_flat_position_is_not_blocked(tmp_path):
    """Selling from FLAT opens a short; there are no long shares to pledge.

    The lock is about long inventory a call can call away. Reading a short-open as a
    cover-stripping sale would silently forbid short selling everywhere.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-shortopen",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        # A short call on AAPL exists and pledges 100 shares the account does not hold.
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="naked_call")
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 100
        assert _shares(acct) == pytest.approx(0.0)

        order = _equity_sell(acct, "AAPL", 50, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct) == pytest.approx(-50.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_short_put_pledges_no_shares(tmp_path):
    """A short put obliges CASH, not stock — the shares stay free to sell.

    Counting it here would refuse a sale nothing has a claim on, which is the mirror error
    of the defect: over-refusing is not "safe", it silently disables exits.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-shortput",
        chains={"AAPL": [_chain(_PUT_170, 170.0, ot="put")]},
        bars=[_bar(_PUT_170, "2024-03-06", 2.0, "put", 170.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)
        _write_short(acct, _PUT_170, OptionRight.PUT, 170.0, 1, strategy="cash_secured_put")
        assert acct._option_positions[_PUT_170].qty == -1
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 0

        order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct) == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_short_call_on_another_ticker_does_not_lock_this_one(tmp_path):
    """The pledge is per UNDERLYING: a short MSFT call has no claim on AAPL shares."""
    acct, ps, ctx = _account(
        tmp_path, "lock-otherticker", symbols=("AAPL", "MSFT"),
        chains={"MSFT": [_chain(_MSFT_CALL, 400.0, underlying="MSFT")]},
        bars=[_bar(_MSFT_CALL, "2024-03-06", 2.0, "call", 400.0, underlying="MSFT")])
    try:
        acct._update_position("AAPL", 100, 180.0)
        acct._update_position("MSFT", 100, 400.0)
        _write_short(acct, _MSFT_CALL, OptionRight.CALL, 400.0, 1,
                     strategy="covered_call", underlying="MSFT")
        assert acct._ledger_shares_pledged_to_short_calls("MSFT") == 100
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 0

        order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct, "AAPL") == pytest.approx(0.0)
        assert _shares(acct, "MSFT") == pytest.approx(100.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_long_call_pledges_nothing(tmp_path):
    """Only the SHORT side can be called away; a long call places no claim on inventory."""
    acct, ps, ctx = _account(
        tmp_path, "lock-longcall",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)
        acct.submit_option_order(
            legs=[_leg(_CALL_200, OptionRight.CALL, 200.0, side=OrderDirection.BUY)],
            quantity=1, order_type="market", option_strategy="long_call")
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct._option_positions[_CALL_200].qty == 1
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 0

        order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct) == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_bought_back_call_releases_its_pledge(tmp_path):
    """A lot netted to zero pledges nothing — the shares are free again.

    Zeroed lots are LEFT in ``_option_positions`` rather than removed, so a pledge that
    read every key rather than every OPEN short would lock the shares forever.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-release",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="covered_call")
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 100

        # Buy the call back — the lot nets to zero but stays in the dict.
        acct._update_option_position(_CALL_200, 1.0, 2.0, 100.0)
        assert _CALL_200 in acct._option_positions
        assert acct._option_positions[_CALL_200].qty == 0
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 0

        order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct) == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)


def test_the_margin_call_short_cover_is_unaffected(tmp_path):
    """``_liquidate_stock_position`` only ever BUYS to cover a short — the lock is a no-op.

    Its single caller iterates ``p.qty < 0``, so ``signed = -pos.qty`` is positive. Recorded
    here because the brief classified this site as a possible strategy exit and it is not:
    refusing it would strand a position, and the guard can never reach it.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-margincover",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", -100, 180.0)   # a SHORT stock lot
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="naked_call")
        ps.set_clock(datetime(2024, 3, 6))

        assert acct._liquidate_stock_position(acct._positions["AAPL"]) is True
        assert _shares(acct) == pytest.approx(0.0), "the buy-to-cover must go through"
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# UNKNOWN IS NEVER ZERO
# =========================================================================== #
def test_an_unresolvable_short_call_lot_refuses_the_sell_and_names_it(tmp_path, caplog):
    """A short call lot whose contract terms cannot be read makes the pledge UNMEASURABLE.

    ``_lot_order`` returns None for a contract with no order row (or whose every row has a
    NULL strike). Today ``_covered_short_call_contracts`` ``continue``s past such a lot,
    i.e. reads it as "not a call" — for a MARGIN estimate that is a defensible skip, for a
    PLEDGE it is the wrong direction: the lot might be the very call whose cover is about
    to be sold. One unreadable lot must poison the WHOLE answer (a partial sum is a smaller
    number that looks exactly like a measured one) and the message must NAME it, so an
    operator repairs the order book instead of hunting a phantom shortfall.
    """
    import logging
    from app.services.backtest.backtest_account import _OptionLot

    acct, ps, ctx = _account(
        tmp_path, "lock-unmeasurable",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 500, 180.0)
        # A short lot with NO order row behind it — the contract terms are unreadable.
        acct._option_positions[_CALL_210] = _OptionLot(
            contract_symbol=_CALL_210, qty=-1.0, avg_price=2.0, multiplier=100.0)
        acct._option_memo_gen += 1
        assert acct._lot_order(_CALL_210) is None

        assert acct._ledger_shares_pledged_to_short_calls("AAPL") is None, (
            "an unresolvable short-call lot must read as UNKNOWN, never as 'nothing pledged'")

        with caplog.at_level(logging.ERROR):
            order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.CANCELED
        assert _shares(acct) == pytest.approx(500.0), (
            "500 shares with an unmeasurable pledge must not be sold on an assumption")
        assert _CALL_210 in caplog.text, (
            f"the refusal must name the lot that could not be measured; got:\n{caplog.text}")
    finally:
        ctx.__exit__(None, None, None)


def test_a_short_call_lot_with_an_unreadable_multiplier_is_unmeasurable(tmp_path):
    """A multiplier of 0/None is a MISSING FIELD, not a contract that delivers no shares.

    ``float(lot.multiplier or 100)`` — the shape used elsewhere in this file — would guess
    100 here and could under-report an adjusted (post-split) contract's pledge on precisely
    the contract whose oddity nobody remembers.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-badmult",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 500, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="covered_call")
        acct._option_positions[_CALL_200].multiplier = 0.0

        assert acct._ledger_shares_pledged_to_short_calls("AAPL") is None

        order = _equity_sell(acct, "AAPL", 100, 190.0)
        assert order.status == OrderStatus.CANCELED
        assert _shares(acct) == pytest.approx(500.0)
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# THE PATH THAT ACTUALLY LEAKED — the bracket TP/SL exit
# =========================================================================== #
def test_the_bracket_stop_cannot_sell_pledged_shares(tmp_path):
    """O_CC's staged trailing stop is a transaction ``stop_loss``, filled by
    ``_apply_bracket_exits`` — which synthesises its OWN close order and never touches
    ``submit_close_order_for_transaction``, so the live cover guard cannot see it.

    MEASURED before the fix, on this exact shape: ``BEFORE shares 100 / pledged 100`` ->
    ``AFTER shares 0 / pledged 100``, short lot still open, covered set emptied — a naked
    short call produced with no error and no log line.
    """
    from ba2_common.core.db import add_instance, update_instance
    from ba2_common.core.models import TradingOrder

    acct, ps, ctx = _account(
        tmp_path, "lock-bracket",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        # A real equity lot through the real fill path, so the transaction exists.
        buy = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=100,
                           side=OrderDirection.BUY, order_type=OrderType.MARKET,
                           status=OrderStatus.NEW, comment="equity-lot")
        acct.submit_order(buy)
        persisted = acct.get_order(buy.broker_order_id)
        eq_txn_id = persisted.transaction_id
        acct._apply_fill(persisted, 180.0, datetime(2024, 3, 5))
        acct.refresh_transactions()

        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="covered_call")
        assert _shares(acct) == pytest.approx(100.0)
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 100

        # Arm a stop the NEXT bar (2024-03-08, low 179) crosses. The clock must sit on
        # 2024-03-06: the fill model is next_bar_open, so a bracket evaluated ON the last
        # bar has nothing to fill against and would read as "refused" for the wrong reason.
        from ba2_common.core.models import Transaction
        from ba2_common.core.db import get_instance
        txn = get_instance(Transaction, eq_txn_id)
        txn.stop_loss = 179.5
        update_instance(txn)
        ps.set_clock(datetime(2024, 3, 6))

        fired = acct._apply_bracket_exits(datetime(2024, 3, 6))

        assert _shares(acct) == pytest.approx(100.0), (
            "the bracket stop sold the cover out from under the written call")
        assert acct._option_positions[_CALL_200].qty == -1
        assert _CALL_200 in acct._covered_short_call_contracts(), (
            "the call must still be COVERED after the refused exit")
        assert fired is False, (
            "a bracket exit the lock refuses did not fire; reporting it as a fill makes the "
            "engine believe the position is flat")

        # And it must not litter the order book with one CANCELED row per crossing bar:
        # the O_CC case carried a stuck call for 40 consecutive bars.
        acct._apply_bracket_exits(datetime(2024, 3, 6))
        acct._apply_bracket_exits(datetime(2024, 3, 6))
        canceled = [o for o in acct.get_orders()
                    if o.symbol == "AAPL" and o.status == OrderStatus.CANCELED]
        assert canceled == [], (
            f"the refused bracket exit must not be written at all, got {len(canceled)} rows")
    finally:
        ctx.__exit__(None, None, None)


def test_the_bracket_stop_still_sells_the_unpledged_excess(tmp_path):
    """297 held / 200 pledged: the stop takes the 97 free shares and leaves the cover."""
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import TradingOrder, Transaction

    acct, ps, ctx = _account(
        tmp_path, "lock-bracket-partial",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        buy = TradingOrder(account_id=acct.id, symbol="AAPL", quantity=297,
                           side=OrderDirection.BUY, order_type=OrderType.MARKET,
                           status=OrderStatus.NEW, comment="equity-lot")
        acct.submit_order(buy)
        persisted = acct.get_order(buy.broker_order_id)
        eq_txn_id = persisted.transaction_id
        acct._apply_fill(persisted, 180.0, datetime(2024, 3, 5))
        acct.refresh_transactions()

        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 2, strategy="covered_call")
        assert acct._ledger_shares_pledged_to_short_calls("AAPL") == 200

        txn = get_instance(Transaction, eq_txn_id)
        txn.stop_loss = 179.5
        update_instance(txn)
        ps.set_clock(datetime(2024, 3, 6))   # next_bar_open -> fills off the 03-08 bar

        assert acct._apply_bracket_exits(datetime(2024, 3, 6)) is True
        assert _shares(acct) == pytest.approx(200.0)
        sells = [o for o in acct.get_orders()          # equity rows only — the written
                 if o.symbol == "AAPL"                 # call is a SELL on AAPL too
                 and o.side == OrderDirection.SELL
                 and not getattr(o, "contract_symbol", None)]
        assert len(sells) == 1 and float(sells[0].filled_qty) == pytest.approx(97.0)
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# THE OTHER LEAK — the assignment orphan liquidation
# =========================================================================== #
def test_the_assignment_liquidation_leaves_pledged_shares_pending(tmp_path):
    """``hold_assigned_stock`` OFF (the default) sells assigned stock at the next bar's
    open — including stock a covered call written in the SAME manage pass now covers.

    ``daily_engine.py`` already documents this hazard in prose; the only mitigation was the
    opt-in per-strategy ``hold_assigned_stock``, so every arm that is not O_WHEEL was
    exposed. The lock refuses the sale and the symbol STAYS PENDING (mirroring the existing
    "no bar this tick -> retry next bar" idiom), so the shares liquidate the moment the call
    is bought back or expires rather than being silently forgotten.
    """
    acct, ps, ctx = _account(
        tmp_path, "lock-assignliq",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 100, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="covered_call")
        acct._pending_assignment_sells["AAPL"] = 100.0
        ps.set_clock(datetime(2024, 3, 6))

        assert acct.process_pending_assignment_liquidations() is False
        assert _shares(acct) == pytest.approx(100.0), (
            "the orphan liquidation sold the covered call's collateral")
        assert acct._pending_assignment_sells.get("AAPL") == pytest.approx(100.0), (
            "the liquidation must stay QUEUED, not be silently dropped")

        # Buy the call back -> the pledge is released and the queued liquidation runs.
        acct._update_option_position(_CALL_200, 1.0, 2.0, 100.0)
        assert acct.process_pending_assignment_liquidations() is True
        assert _shares(acct) == pytest.approx(0.0)
        assert "AAPL" not in acct._pending_assignment_sells
    finally:
        ctx.__exit__(None, None, None)


def test_the_assignment_liquidation_buy_back_of_a_short_is_never_blocked(tmp_path):
    """The ``assigned < 0`` branch BUYS to cover an assigned short — it can only ADD cover."""
    acct, ps, ctx = _account(
        tmp_path, "lock-assignliq-short",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", -100, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 1, strategy="naked_call")
        acct._pending_assignment_sells["AAPL"] = -100.0
        ps.set_clock(datetime(2024, 3, 6))

        assert acct.process_pending_assignment_liquidations() is True
        assert _shares(acct) == pytest.approx(0.0)
        assert "AAPL" not in acct._pending_assignment_sells
    finally:
        ctx.__exit__(None, None, None)


def test_the_assignment_liquidation_sells_only_the_unpledged_excess(tmp_path):
    """297 held with 200 pledged and 150 orphaned: 97 liquidate, 53 stay queued."""
    acct, ps, ctx = _account(
        tmp_path, "lock-assignliq-partial",
        chains={"AAPL": [_chain(_CALL_200, 200.0)]},
        bars=[_bar(_CALL_200, "2024-03-06", 2.0, "call", 200.0)])
    try:
        acct._update_position("AAPL", 297, 180.0)
        _write_short(acct, _CALL_200, OptionRight.CALL, 200.0, 2, strategy="covered_call")
        acct._pending_assignment_sells["AAPL"] = 150.0
        ps.set_clock(datetime(2024, 3, 6))

        assert acct.process_pending_assignment_liquidations() is True
        assert _shares(acct) == pytest.approx(200.0)
        assert acct._pending_assignment_sells.get("AAPL") == pytest.approx(53.0)
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# EQUITY-ONLY RUNS PAY NOTHING
# =========================================================================== #
def test_an_equity_only_account_is_untouched(tmp_path):
    """No option lots -> the pledge is a MEASURED zero and the sell path is unchanged."""
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db("lock-equityonly")
    ctx.__enter__()
    try:
        seed_account_definition(1, CFG)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _AAPL_BARS)
        ps.set_clock(datetime(2024, 3, 5))
        acct = BacktestAccount(1, ps, CFG)      # no options provider at all
        wire_backtest_seams().register_account(1, acct)

        acct._update_position("AAPL", 100, 180.0)
        order = _equity_sell(acct, "AAPL", 100, 190.0)

        assert order.status == OrderStatus.FILLED
        assert _shares(acct) == pytest.approx(0.0)
    finally:
        ctx.__exit__(None, None, None)
