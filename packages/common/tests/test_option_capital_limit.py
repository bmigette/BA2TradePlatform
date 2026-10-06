"""The capital limit of the option book (owner rule, 2026-10-06).

No new portfolio cap -- but the debits paid plus the collateral reserved by every open option
structure, plus the stock the account holds, can never exceed what the account has: "options must
not use more than 100 % of the equity, like with stocks".

``OptionsAccountInterface.option_capital_headroom`` is the one answer, and it is DEFINED THE SAME
in backtest and live:  ``equity (snapshot) - cost of open positions - option reserves - pending
debit entries``.  ``get_balance()`` is NOT in it: it is spendable CASH in a backtest and EQUITY at
every live broker (a filled debit leaves live equity unchanged), so a headroom built on it
tightened in the backtest and never live.  Read by ``TradeActions._size_and_submit`` (debits: cut
to fit, refused when nothing fits, the refusal names every unreadable order) and by
``check_option_buying_power`` (reserves).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from ba2_common.core.db import add_instance, get_instance
from ba2_common.core.models import ExpertInstance, TradingOrder, Transaction
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OrderDirection, OrderStatus, OrderType, TransactionStatus)
from tests.test_bull_put_spread import BPS, FakeAccount, _own_db  # noqa: F401  (autouse DB)
from tests.test_bull_put_spread import act

EQUITY = 100_000.0
BUY_CALL = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40)


class _Book(FakeAccount):
    """The fake broker with a controllable open-order book (what the reserve pool and the
    pending-debit read see). Its balance IS its equity: no position holds capital."""

    def __init__(self, balance=EQUITY):
        super().__init__(balance=balance)
        self.book = []

    def open_option_orders_book_wide(self):
        return list(self.book)

    def check_short_put_assignment_capacity(self, **kw):
        return SimpleNamespace(ok=True)       # the assignment gate has its own tests


class _LiveLike(_Book):
    """A LIVE broker: ``get_balance()`` is EQUITY (Alpaca account.equity, TastyTrade
    net_liquidating_value, IBKR net_liquidation). A filled call purchase leaves it unchanged."""


class _BTLike(_Book):
    """The BACKTEST: ``get_balance()`` is spendable CASH (already net of every filled debit and
    stock purchase) while the snapshot publishes the (deployed) equity."""

    def __init__(self, cash=EQUITY, equity=EQUITY):
        super().__init__(balance=cash)
        self.cash, self.equity = cash, equity

    def get_balance(self):
        return self.cash

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        return AccountSnapshot(cash=self.cash, equity=self.equity, net_liquidation=self.equity)


@pytest.fixture
def expert():
    return add_instance(ExpertInstance(account_id=1, expert="MockExpert", virtual_equity_pct=100.0))


def _open_option_txn(expert_id, *, price, qty, strategy="long_call", side=OrderDirection.BUY):
    return add_instance(Transaction(
        symbol="XYZ", quantity=qty, side=side, open_price=price, multiplier=100,
        asset_class=AssetClass.OPTION, option_strategy=strategy, status=TransactionStatus.OPENED,
        expert_id=expert_id))


def _open_stock_txn(expert_id, *, price, qty):
    return add_instance(Transaction(
        symbol="ABC", quantity=qty, side=OrderDirection.BUY, open_price=price,
        asset_class=AssetClass.EQUITY, status=TransactionStatus.OPENED, expert_id=expert_id))


def _reserve_row(amount, strategy="bull_put_spread", oid=None, symbol="XYZ"):
    return SimpleNamespace(
        id=oid or (int(amount) % 997 + 1), option_strategy=strategy, symbol=symbol,
        data={"option_reserve": amount}, status=OrderStatus.FILLED, quantity=1, filled_qty=1,
        parent_order_id=None, contract_symbol=None, limit_price=None, position_intent=None,
        side=OrderDirection.SELL, multiplier=100)


def _pending_debit_row(limit, qty, *, parent=True, **over):
    row = SimpleNamespace(
        id=900 + int(round((limit or 0) * 100)) + (qty % 7), option_strategy="long_call",
        symbol="XYZ", data={},
        status=OrderStatus.PENDING, quantity=qty, filled_qty=0, parent_order_id=None,
        contract_symbol=None if parent else "XYZ240621C00100000", limit_price=limit,
        position_intent=None, side=OrderDirection.BUY, multiplier=100)
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _buy(acct, sizing):
    return act(acct, ExpertActionType.BUY_CALL.value, sizing=sizing, **BUY_CALL).execute()


def _bps(acct, sizing):
    return act(acct, **{**BPS, "sizing": sizing}).execute()


def _unit_cost(acct):
    """What ONE contract of the long call costs (what a probe entry paid)."""
    probe = _Book()
    assert _buy(probe, 1.0)["success"]
    return probe.submitted[-1]["limit_price"] * 100.0


# ------------------------------------------------------------------ the headroom arithmetic
def test_headroom_is_equity_less_open_cost_less_reserves_less_pending_debits(expert):
    acct = _Book()
    assert acct.option_capital_headroom() == EQUITY
    _open_stock_txn(expert, price=100.0, qty=100)                    # 10k of shares
    _open_option_txn(expert, price=5.0, qty=20)                      # 10k of premium paid
    acct.book = [_reserve_row(30_000.0), _pending_debit_row(5.0, 20)]  # 30k + 10k pending
    assert acct.pending_option_debit_outlay() == pytest.approx(10_000.0)
    assert acct.option_capital_headroom() == pytest.approx(EQUITY - 10_000 - 10_000 - 30_000 - 10_000)


def test_an_open_credit_structure_is_its_reserve_not_its_premium(expert):
    """A credit structure's transaction is NOT position cost (its premium is not capital spent);
    its collateral is the pool's reserve, counted once."""
    acct = _Book()
    _open_option_txn(expert, price=-1.5, qty=10, strategy="bull_put_spread", side=OrderDirection.SELL)
    _open_option_txn(expert, price=2.0, qty=5, strategy="covered_call", side=OrderDirection.SELL)
    acct.book = [_reserve_row(20_000.0)]
    assert acct.option_capital_headroom() == pytest.approx(EQUITY - 20_000.0)


def test_pending_outlay_counts_only_unfilled_buys_and_whole_priced_parents():
    acct = _Book()
    sold = _pending_debit_row(5.0, 20, parent=False)
    sold.side = OrderDirection.SELL                                          # a credit: no outlay
    credit_parent = _pending_debit_row(-1.5, 10, option_strategy="bull_put_spread",
                                       side=OrderDirection.SELL)             # net credit parent
    filled = _pending_debit_row(5.0, 20)
    filled.status = OrderStatus.FILLED                                       # already paid
    closing = _pending_debit_row(5.0, 20, parent=False)
    closing.position_intent = "buy_to_close"
    child = _pending_debit_row(5.0, 20, parent=False)
    child.parent_order_id = 7                                                # the parent prices it
    partial = _pending_debit_row(2.0, 10, parent=False)
    partial.status, partial.filled_qty = OrderStatus.PARTIALLY_FILLED, 4     # 6 left x 2 x 100
    acct.book = [sold, credit_parent, filled, closing, child, partial]
    detail = acct.pending_option_debit_outlay_detail()
    assert detail.total == pytest.approx(1_200.0) and detail.unmeasurable == ()


# ------------------------------------------------------------------ item 1: ONE definition, BT == live
def test_a_live_like_account_cuts_the_second_60pct_entry_exactly_like_the_backtest():
    """Same session, two 60 % entries. The first is in flight (pending). The second is cut to the
    40 % that is left -- identically on a live-like account (balance = equity) and on a
    backtest-like one (balance = cash)."""
    unit = _unit_cost(_Book())
    results = {}
    for name, acct in (("live", _LiveLike()), ("bt", _BTLike())):
        first = _buy(acct, 60.0)
        assert first["success"], first["message"]
        sub = acct.submitted[-1]
        assert sub["quantity"] == int(0.60 * EQUITY // unit)
        acct.book.append(_pending_debit_row(sub["limit_price"], sub["quantity"]))
        second = _buy(acct, 60.0)
        assert second["success"], second["message"]
        results[name] = acct.submitted[-1]["quantity"]
        assert second["data"]["capital_headroom_cut"].startswith(
            f"sized {int(0.60 * EQUITY // unit)} -> {results[name]} contract(s)")
    assert results["live"] == results["bt"]
    # exactly the 40 % that is left, to the contract
    first_cost = int(0.60 * EQUITY // unit) * unit
    assert results["live"] == int((EQUITY - first_cost) // unit)


def test_after_the_fill_a_live_balance_does_not_move_but_the_headroom_does(expert):
    """THE CRITICAL DEFECT. A filled 60k call purchase leaves a live account's balance (equity)
    at 100k and a backtest's cash at 40k. Both report the SAME 40k of headroom, because both
    subtract the cost of the open position from the (published) equity."""
    live, bt = _LiveLike(), _BTLike(cash=EQUITY - 60_000.0, equity=EQUITY)
    assert live.get_balance() == EQUITY and bt.get_balance() == 40_000.0
    _open_option_txn(expert, price=6.0, qty=100)                             # 60,000 paid
    assert live.option_capital_headroom() == pytest.approx(40_000.0)
    assert bt.option_capital_headroom() == pytest.approx(40_000.0)


def test_after_the_fill_a_live_account_still_cuts_a_second_60pct_entry(expert):
    """The old headroom (balance - reserves - pending) read 100k live after the fill, so a second
    60 % entry passed whole: 120 % of the equity."""
    unit = _unit_cost(_Book())
    live = _LiveLike()
    _open_option_txn(expert, price=60_000.0 / 100 / 10, qty=10)              # 60,000 paid
    res = _buy(live, 60.0)
    assert res["success"], res["message"]
    qty = live.submitted[-1]["quantity"]
    assert qty == int(40_000.0 // unit) < int(0.60 * EQUITY // unit)
    assert qty * unit <= 40_000.0


def test_an_existing_stock_position_is_capital_a_new_debit_cannot_spend(expert):
    """50k of shares are held: the option book may commit at most the other 50k, however the
    broker's balance reads."""
    unit = _unit_cost(_Book())
    _open_stock_txn(expert, price=250.0, qty=200)                            # 50,000 of stock
    live = _LiveLike()
    assert _buy(live, 60.0)["success"]
    assert live.submitted[-1]["quantity"] == int(50_000.0 // unit)       # 60k cut to the 50k left
    # The backtest sizes its budget from CASH (50k here, a pre-existing sizing-base difference),
    # so its own 60 % is smaller still -- and never past the shared headroom.
    bt = _BTLike(cash=50_000.0, equity=EQUITY)
    assert bt.option_capital_headroom() == pytest.approx(live.option_capital_headroom())
    assert _buy(bt, 60.0)["success"]
    assert bt.submitted[-1]["quantity"] * unit <= 50_000.0


def test_a_stock_position_is_priced_by_the_stock_paths_function(expert):
    """One used-balance function: the classic RM's per-expert ``_calculate_used_balance`` and the
    option bound read the same numbers for the same transactions."""
    from ba2_common.core.interfaces.MarketExpertInterface import used_balance_for_transactions
    t_stock = get_instance(Transaction, _open_stock_txn(expert, price=100.0, qty=10))
    t_opt = get_instance(Transaction, _open_option_txn(expert, price=2.0, qty=3))
    acct = _Book()
    assert used_balance_for_transactions(acct, [t_stock, t_opt], loss_adjusted=False) == \
        pytest.approx(100.0 * 10 + 2.0 * 3 * 100)
    assert acct.open_position_cost_basis().total == pytest.approx(1_600.0)


# ------------------------------------------------------------------ debit structures
def test_a_debit_entry_on_an_empty_book_sizes_exactly_as_before():
    acct = _Book()
    res = _buy(acct, 40.0)
    assert res["success"], res["message"]
    qty = acct.submitted[-1]["quantity"]
    cost = acct.submitted[-1]["limit_price"] * 100.0
    assert qty == int(0.40 * EQUITY // cost)
    assert "capital_headroom_cut" not in res["data"] and "sized" not in res["message"]


def test_a_debit_entry_is_cut_to_the_capital_the_reserves_leave():
    """90k of collateral is reserved: a 40 % sizing (40k) may spend only the remaining 10k."""
    acct = _Book()
    acct.book = [_reserve_row(90_000.0)]
    res = _buy(acct, 40.0)
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    cost = sub["limit_price"] * 100.0
    assert sub["quantity"] == int(10_000.0 // cost) < int(0.40 * EQUITY // cost)
    assert sub["quantity"] * cost <= 10_000.0


def test_a_debit_entry_is_cut_to_the_capital_pending_debits_leave():
    """Another entry decided the same session will still pay 70k: this one may spend 30k."""
    acct = _Book()
    acct.book = [_pending_debit_row(7.0, 100)]                               # 70,000
    res = _buy(acct, 60.0)
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    cost = sub["limit_price"] * 100.0
    assert sub["quantity"] == int(30_000.0 // cost)


def test_a_debit_entry_with_no_capital_left_is_refused_loudly():
    acct = _Book()
    acct.book = [_reserve_row(EQUITY)]
    res = _buy(acct, 40.0)
    assert res["success"] is False and "Insufficient option capital" in res["message"]
    assert acct.submitted == []


# ------------------------------------------------------------------ item 4: the cut is announced
class _RowBook(_Book):
    """``submit_option_order`` writes the REAL order row, so the persisted comment is testable."""

    def submit_option_order(self, *, legs, quantity, order_type, limit_price, option_strategy,
                            expert_recommendation_id=None, transaction_id=None):
        super().submit_option_order(legs=legs, quantity=quantity, order_type=order_type,
                                    limit_price=limit_price, option_strategy=option_strategy)
        oid = add_instance(TradingOrder(
            account_id=1, symbol="XYZ", quantity=quantity, side=OrderDirection.BUY,
            order_type=OrderType.BUY_LIMIT, status=OrderStatus.PENDING, limit_price=limit_price,
            asset_class=AssetClass.OPTION, option_strategy=option_strategy, good_for=None,
            filled_qty=None, comment=None, data={}))
        return get_instance(TradingOrder, oid)


def test_the_cut_is_in_the_result_message_the_result_data_and_the_order_comment():
    acct = _RowBook()
    acct.book = [_reserve_row(90_000.0)]
    res = _buy(acct, 40.0)
    assert res["success"], res["message"]
    note = res["data"]["capital_headroom_cut"]
    assert note.startswith("sized ") and "option capital headroom $10,000.00" in note
    assert note in res["message"]
    stored = get_instance(TradingOrder, res["data"]["order_id"])
    assert note in (stored.comment or "")
    # the per-contract debit rides the order row, so a market-type working entry can be valued
    assert stored.data["entry_debit_per_contract"] > 0


# ------------------------------------------------------------------ item 2: unreadable reserve is NAMED
def test_an_unreadable_reserve_refuses_debit_entries_and_names_the_order(monkeypatch):
    import ba2_common.core.TradeActions as TA
    warned = []
    monkeypatch.setattr(TA.logger, "warning", lambda m, *a, **k: warned.append(str(m)))
    acct = _Book()
    blind = _reserve_row(0.0, strategy="cash_secured_put", oid=4321, symbol="LEGACY")
    blind.data = {}
    acct.book = [blind]
    res = _buy(acct, 5.0)
    assert res["success"] is False and "cannot be measured" in res["message"]
    assert "order 4321" in res["message"] and "LEGACY" in res["message"]
    assert acct.submitted == []
    logged = " ".join(warned)
    assert "order 4321" in logged and "LEGACY" in logged


class _DbBook(FakeAccount):
    """The REAL ``open_option_orders_book_wide`` over real order / transaction rows."""

    def check_short_put_assignment_capacity(self, **kw):
        return SimpleNamespace(ok=True)


def _order_row(expert_id, *, status, filled, txn_status, symbol, reserve=None):
    txn = add_instance(Transaction(
        symbol=symbol, quantity=1, side=OrderDirection.SELL, open_price=-1.0, multiplier=100,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put", status=txn_status,
        expert_id=expert_id))
    return add_instance(TradingOrder(
        account_id=1, symbol=symbol, quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, status=status, filled_qty=filled,
        asset_class=AssetClass.OPTION, option_strategy="cash_secured_put", transaction_id=txn,
        good_for=None, comment=None,
        data=({"option_reserve": reserve} if reserve is not None else {})))


def test_a_closed_or_terminal_order_is_never_the_cause_only_a_genuinely_open_one(expert):
    acct = _DbBook()
    # (1) cancelled, nothing ever filled; (2) filled but its position CLOSED: neither holds capital
    _order_row(expert, status=OrderStatus.CANCELED, filled=None,
               txn_status=TransactionStatus.CLOSED, symbol="DEAD1")
    _order_row(expert, status=OrderStatus.FILLED, filled=1.0,
               txn_status=TransactionStatus.CLOSED, symbol="DEAD2")
    detail = acct.option_capital_headroom_detail()
    assert detail.unmeasurable == () and detail.value == EQUITY
    # (3) filled, position OPEN, no reserve recorded: genuinely unknown, and named
    live_id = _order_row(expert, status=OrderStatus.FILLED, filled=1.0,
                         txn_status=TransactionStatus.OPENED, symbol="OPEN3")
    detail = acct.option_capital_headroom_detail()
    assert detail.value is None
    assert len(detail.unmeasurable) == 1
    assert f"order {live_id}" in detail.unmeasurable[0] and "OPEN3" in detail.unmeasurable[0]
    assert "DEAD1" not in detail.unmeasurable[0] and "DEAD2" not in detail.unmeasurable[0]


# ------------------------------------------------------------------ item 3: pending outlay never fails open
def test_a_working_entry_with_no_price_is_unmeasurable_and_named():
    acct = _Book()
    acct.book = [_pending_debit_row(None, 10, id=77, symbol="NOPRICE")]
    detail = acct.pending_option_debit_outlay_detail()
    assert detail.total == 0.0 and len(detail.unmeasurable) == 1
    assert "order 77" in detail.unmeasurable[0] and "NOPRICE" in detail.unmeasurable[0]
    assert acct.option_capital_headroom() is None
    res = _buy(acct, 5.0)
    assert res["success"] is False and "order 77" in res["message"] and "NOPRICE" in res["message"]


def test_a_market_type_entry_is_valued_by_the_debit_its_builder_recorded():
    acct = _Book()
    acct.book = [_pending_debit_row(None, 10, data={"entry_debit_per_contract": 450.0})]
    detail = acct.pending_option_debit_outlay_detail()
    assert detail.total == pytest.approx(4_500.0) and detail.unmeasurable == ()
    assert acct.option_capital_headroom() == pytest.approx(EQUITY - 4_500.0)


@pytest.mark.parametrize("over,needle", [
    ({"quantity": None}, "unreadable quantity"),
    ({"quantity": "many"}, "unreadable quantity"),
    ({"quantity": 0}, "unreadable quantity"),
    ({"multiplier": None}, "unreadable multiplier"),
    ({"multiplier": 0}, "unreadable multiplier"),
    ({"limit_price": "n/a", "data": {}}, "no readable limit price"),
    ({"limit_price": 0.0, "data": {}}, "no readable limit price"),
])
def test_no_default_is_substituted_for_an_unreadable_field(over, needle):
    acct = _Book()
    acct.book = [_pending_debit_row(5.0, 10, **over)]
    detail = acct.pending_option_debit_outlay_detail()
    assert detail.total == 0.0
    assert len(detail.unmeasurable) == 1 and needle in detail.unmeasurable[0]


# ------------------------------------------------------------------ item 7: no silent fail-open
def test_an_account_without_a_capital_model_is_a_defect_not_a_free_pass():
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "XYZ"
    a.account = object()
    with pytest.raises(AttributeError):
        a._fit_debit_to_capital(5, 100.0, "long_call")


def test_a_non_numeric_headroom_is_a_defect_not_a_free_pass():
    import ba2_common.core.TradeActions as TA
    a = TA.BuyCallAction.__new__(TA.BuyCallAction)
    a.instrument_name = "XYZ"
    a.account = SimpleNamespace(
        option_capital_headroom_detail=lambda: SimpleNamespace(value="plenty", unmeasurable=()))
    with pytest.raises(TypeError):
        a._fit_debit_to_capital(5, 100.0, "long_call")


# ------------------------------------------------------------------ reserve-based structures (item 8)
def test_a_credit_spread_that_does_not_fit_is_refused_loudly():
    acct = _Book()
    acct.book = [_reserve_row(EQUITY - 100.0)]                               # 100 of room
    res = _bps(acct, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]
    assert acct.submitted == []


def test_a_credit_spread_cannot_reserve_cash_a_pending_debit_is_about_to_spend():
    """The reserve gate reads the same headroom: 95k of unfilled debit entries leave 5k, and a
    credit spread whose collateral is larger is refused."""
    acct = _Book()
    acct.book = [_pending_debit_row(9.5, 100)]                               # 95,000
    res = _bps(acct, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]


def test_a_credit_spread_cannot_reserve_capital_an_open_stock_position_holds(expert):
    _open_stock_txn(expert, price=100.0, qty=990)                            # 99,000 of shares
    res = _bps(_LiveLike(), 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]


# ------------------------------------------------------------------ the invariant
@pytest.mark.parametrize("make", [_LiveLike, _BTLike], ids=["live", "backtest"])
def test_no_sequence_of_entries_commits_more_than_the_account_has(make, expert):
    """Open debit and credit structures alternately, each sized at 40 % of the account, filling
    each debit into an open transaction the way the broker would: after EVERY entry, cost of open
    positions + reserves + pending debits is at most the equity -- and the limit actually bites."""
    acct = make()
    cut_or_refused = 0
    for i in range(16):
        before = len(acct.submitted)
        res = _buy(acct, 40.0) if i % 2 == 0 else _bps(acct, 40.0)
        if len(acct.submitted) == before:
            cut_or_refused += 1
        else:
            sub = acct.submitted[-1]
            if i % 2 == 0:
                if i % 4 == 0:      # fills: becomes an open position (cash falls on a BT account)
                    _open_option_txn(expert, price=sub["limit_price"], qty=sub["quantity"])
                    if isinstance(acct, _BTLike):
                        acct.cash -= sub["limit_price"] * 100 * sub["quantity"]
                else:               # still working
                    acct.book.append(_pending_debit_row(sub["limit_price"], sub["quantity"]))
            else:
                acct.book.append(_reserve_row(float(res["data"]["option_reserve"])))
        committed = (acct.pending_option_debit_outlay()
                     + acct.reserved_option_buying_power_detail().total
                     + acct.open_position_cost_basis().total)
        assert committed <= EQUITY + 1e-6, (i, committed)
    assert cut_or_refused >= 1, "the limit never bit: the scenario does not exercise it"
    assert acct.option_capital_headroom() >= -1e-6
