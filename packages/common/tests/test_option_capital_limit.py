"""The capital limit of the option book (owner rule, 2026-10-06).

No new portfolio cap -- but the debits paid plus the collateral reserved by every open option
structure can never exceed what the account has, "like with stocks". Credit structures always
refused when their reserve did not fit; a DEBIT entry (sized as a percentage of the account) never
asked, so it could spend cash a credit structure had reserved, or cash a debit entry decided in the
same session was about to pay. ``OptionsAccountInterface.option_capital_headroom`` is the one
answer now, read by ``TradeActions._size_and_submit`` (debits: cut to fit, refused when nothing
fits) and by ``check_option_buying_power`` (reserves). Live and backtest share both.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ba2_common.core.types import ExpertActionType, OrderDirection, OrderStatus
from tests.test_bull_put_spread import BPS, FakeAccount, _own_db  # noqa: F401  (autouse DB)
from tests.test_bull_put_spread import act

BALANCE = 100_000.0
BUY_CALL = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40)


class _Book(FakeAccount):
    """The fake broker with a controllable open-order book (what the reserve pool and the
    pending-debit read see)."""

    def __init__(self, balance=BALANCE):
        super().__init__(balance=balance)
        self.book = []

    def open_option_orders_book_wide(self):
        return list(self.book)

    def check_short_put_assignment_capacity(self, **kw):
        return SimpleNamespace(ok=True)       # the assignment gate has its own tests


def _reserve_row(amount, strategy="bull_put_spread"):
    return SimpleNamespace(
        id=len(str(amount)) + int(amount), option_strategy=strategy, symbol="XYZ",
        data={"option_reserve": amount}, status=OrderStatus.FILLED, quantity=1, filled_qty=1,
        parent_order_id=None, contract_symbol=None, limit_price=None, position_intent=None,
        side=OrderDirection.SELL, multiplier=100)


def _pending_debit_row(limit, qty, *, parent=True):
    return SimpleNamespace(
        id=900 + int(limit * 100), option_strategy="long_call", symbol="XYZ", data={},
        status=OrderStatus.PENDING, quantity=qty, filled_qty=0, parent_order_id=None,
        contract_symbol=None if parent else "XYZ240621C00100000", limit_price=limit,
        position_intent=None, side=OrderDirection.BUY, multiplier=100)


def _buy(acct, sizing):
    return act(acct, ExpertActionType.BUY_CALL.value, sizing=sizing, **BUY_CALL).execute()


def _bps(acct, sizing):
    return act(acct, **{**BPS, "sizing": sizing}).execute()


# ------------------------------------------------------------------ the headroom arithmetic
def test_headroom_is_the_balance_less_reserves_less_pending_debits():
    acct = _Book()
    assert acct.option_capital_headroom() == BALANCE
    acct.book = [_reserve_row(30_000.0), _pending_debit_row(5.0, 20)]       # 30k + 10k
    assert acct.pending_option_debit_outlay() == pytest.approx(10_000.0)
    assert acct.option_capital_headroom() == pytest.approx(60_000.0)


def test_pending_outlay_counts_only_unfilled_buys_and_whole_priced_parents():
    acct = _Book()
    sold = _pending_debit_row(5.0, 20, parent=False)
    sold.side = OrderDirection.SELL                                          # a credit: no outlay
    credit_parent = _pending_debit_row(-1.5, 10)                             # net credit parent
    filled = _pending_debit_row(5.0, 20)
    filled.status = OrderStatus.FILLED                                       # already paid
    closing = _pending_debit_row(5.0, 20, parent=False)
    closing.position_intent = "buy_to_close"
    child = _pending_debit_row(5.0, 20, parent=False)
    child.parent_order_id = 7                                                # the parent prices it
    partial = _pending_debit_row(2.0, 10, parent=False)
    partial.status, partial.filled_qty = OrderStatus.PARTIALLY_FILLED, 4     # 6 left x 2 x 100
    acct.book = [sold, credit_parent, filled, closing, child, partial]
    assert acct.pending_option_debit_outlay() == pytest.approx(1_200.0)


# ------------------------------------------------------------------ debit structures
def test_a_debit_entry_on_an_empty_book_sizes_exactly_as_before():
    acct = _Book()
    res = _buy(acct, 40.0)
    assert res["success"], res["message"]
    qty = acct.submitted[-1]["quantity"]
    cost = acct.submitted[-1]["limit_price"] * 100.0
    assert qty == int(0.40 * BALANCE // cost)


def test_a_debit_entry_is_cut_to_the_capital_the_reserves_leave():
    """90k of collateral is reserved: a 40 % sizing (40k) may spend only the remaining 10k."""
    acct = _Book()
    acct.book = [_reserve_row(90_000.0)]
    res = _buy(acct, 40.0)
    assert res["success"], res["message"]
    sub = acct.submitted[-1]
    cost = sub["limit_price"] * 100.0
    assert sub["quantity"] == int(10_000.0 // cost) < int(0.40 * BALANCE // cost)
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


def test_a_debit_entry_with_no_capital_left_is_refused_loudly(caplog):
    acct = _Book()
    acct.book = [_reserve_row(BALANCE)]
    res = _buy(acct, 40.0)
    assert res["success"] is False and "Insufficient option capital" in res["message"]
    assert acct.submitted == []


def test_a_debit_entry_is_refused_when_the_pool_cannot_be_measured():
    """A reserving order whose reserve is unreadable: the headroom is UNKNOWN, and unknown
    never reads as room."""
    acct = _Book()
    blind = _reserve_row(0.0)
    blind.data = {}
    acct.book = [blind]
    res = _buy(acct, 5.0)
    assert res["success"] is False and "cannot be measured" in res["message"]
    assert acct.submitted == []


# ------------------------------------------------------------------ reserve-based structures
def test_a_credit_spread_that_does_not_fit_is_refused_loudly():
    acct = _Book()
    acct.book = [_reserve_row(BALANCE - 100.0)]                              # 100 of room
    res = _bps(acct, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]
    assert acct.submitted == []


def test_a_credit_spread_cannot_reserve_cash_a_pending_debit_is_about_to_spend():
    """The reserve gate reads the same headroom: 95k of unfilled debit entries leave 5k, and a
    credit spread whose collateral is larger is refused (it used to pass on the full balance)."""
    acct = _Book()
    acct.book = [_pending_debit_row(9.5, 100)]                               # 95,000
    res = _bps(acct, 40.0)
    assert res["success"] is False and "Insufficient buying power" in res["message"]


# ------------------------------------------------------------------ the invariant
def test_no_sequence_of_entries_commits_more_than_the_account_has():
    """Open debit and credit structures alternately, each sized at 40 % of the account, and keep
    the book the way the broker would: after EVERY entry, debits paid (still pending) plus
    collateral reserved is at most the balance -- and the limit actually bites (some entries are
    cut or refused)."""
    acct = _Book()
    cut_or_refused = 0
    for i in range(16):
        before = len(acct.submitted)
        res = _buy(acct, 40.0) if i % 2 == 0 else _bps(acct, 40.0)
        if len(acct.submitted) == before:
            cut_or_refused += 1
        else:
            sub = acct.submitted[-1]
            if i % 2 == 0:
                acct.book.append(_pending_debit_row(sub["limit_price"], sub["quantity"]))
            else:
                acct.book.append(_reserve_row(float(res["data"]["option_reserve"])))
        committed = (acct.pending_option_debit_outlay()
                     + acct.reserved_option_buying_power_detail().total)
        assert committed <= BALANCE + 1e-6, (i, committed)
    assert cut_or_refused >= 1, "the limit never bit: the scenario does not exercise it"
    assert acct.option_capital_headroom() >= -1e-6
