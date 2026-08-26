"""OPT-L5 / OPT-S6 — the "cash-secured" gate must read CASH, not total equity.

``assignment_capacity`` is the rail that answers "could this account actually take
delivery?". It is designed, documented ("against N of cash") and unit-tested as a
cash-on-hand gate — and it read ``get_balance()``, which on the only options-capable live
adapter returns ``TradeAccount.equity``: cash PLUS every position marked to market.

So a $100,000-equity / $3,000-cash account was admitted to a $20,000 delivery obligation.
The structure the platform advertises as its most explicitly unlevered — the cash-secured
put — was in fact margin-secured, silently.

The right number was already being fetched and thrown away: ``AccountSnapshot.cash``, the
broker-agnostic seam every adapter already implements (Alpaca and TastyTrade override it;
the base reads ``get_account_info()`` tolerantly).

This is also a LIVE/BACKTEST PARITY gap, which is why no grid run could have surfaced it:
``BacktestAccount.get_balance`` returns ``self._cash``, so the same expression means cash in
one engine and equity in the other. Reading the snapshot makes both engines read cash.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection, TransactionStatus

DEC = date(2026, 12, 18)


class _Account(OptionsAccountInterface):
    """Rich in stock, poor in cash — the shape the gate was built to refuse.

    ``get_balance()`` reports total EQUITY, exactly as ``AlpacaAccount.get_balance`` does
    (``float(account.equity)``). ``get_account_snapshot()`` publishes both figures, exactly
    as ``AlpacaAccount.get_account_snapshot`` does.
    """

    def __init__(self, *, equity=100_000.0, cash=3_000.0, account_id=1):
        self.id = account_id
        self._equity = equity
        self._cash = cash

    def get_balance(self):
        return self._equity                      # EQUITY. This is the live semantics.

    def get_account_snapshot(self):
        return AccountSnapshot(cash=self._cash, equity=self._equity,
                               net_liquidation=self._equity, buying_power=self._equity)

    def get_option_chain(self, *a, **k):
        raise AssertionError("no chain fetch here")

    def get_option_quote(self, contract_symbol):
        raise AssertionError("no quote fetch here")

    def get_atm_implied_volatility(self, underlying):
        raise AssertionError("no IV fetch here")

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        raise AssertionError("no close here")

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        raise AssertionError("these tests never submit")


@pytest.fixture
def store():
    with ts.inmem_trades() as s:
        yield s


# ---------------------------------------------------------------------------

def test_a_cash_poor_but_equity_rich_account_cannot_take_delivery(store):
    """$3k of cash does not secure a $20k put, however much stock is in the account."""
    acct = _Account(equity=100_000.0, cash=3_000.0)
    verdict = acct.check_short_put_assignment_capacity(strike=200.0, contracts=1)
    assert verdict.ok is False, (
        "a $100k-equity / $3k-cash account was admitted to a $20,000 delivery obligation — "
        "the gate promises cash-securing and delivered equity-securing")
    assert verdict.cash == 3_000.0, (
        f"the verdict reports {verdict.cash} as 'cash'; it is the account's total equity")


def test_the_refusal_quotes_the_cash_figure_it_actually_used(store):
    """The sentence says "of cash". It must be true."""
    acct = _Account(equity=100_000.0, cash=3_000.0)
    verdict = acct.check_short_put_assignment_capacity(strike=200.0, contracts=1)
    assert "3,000.00 of cash" in verdict.reason, verdict.reason


def test_a_genuinely_cash_funded_put_is_still_admitted(store):
    """Fail-closed must not become refuse-everything: real cash still passes."""
    acct = _Account(equity=100_000.0, cash=25_000.0)
    verdict = acct.check_short_put_assignment_capacity(strike=200.0, contracts=1)
    assert verdict.ok is True, verdict.reason
    assert verdict.cash == 25_000.0


def test_exactly_equal_cash_admits(store):
    """The boundary every other option rail uses — kept identical here."""
    acct = _Account(equity=100_000.0, cash=20_000.0)
    assert acct.check_short_put_assignment_capacity(strike=200.0, contracts=1).ok is True


def test_unreadable_cash_refuses_rather_than_falling_back_to_equity(store):
    """A broker that does not publish cash is UNMEASURABLE, not "use equity instead".

    Falling back to ``get_balance()`` here would silently restore the whole defect on
    exactly the accounts whose cash we cannot see.
    """
    acct = _Account(equity=100_000.0, cash=None)
    verdict = acct.check_short_put_assignment_capacity(strike=200.0, contracts=1)
    assert verdict.ok is False
    assert verdict.cash is None
    assert "cash" in verdict.reason.lower()


def test_an_unreadable_snapshot_refuses_and_does_not_raise(store):
    """A locked DB / broker outage is a refusal, not a crash and not an admission."""
    class _Broken(_Account):
        def get_account_snapshot(self):
            raise OSError("connection reset by peer")

    verdict = _Broken().check_short_put_assignment_capacity(strike=200.0, contracts=1)
    assert verdict.ok is False and verdict.cash is None


def test_a_programming_error_is_not_absorbed_into_a_refusal(store):
    """A defect in our own code must not wear the face of a safety measure.

    Absorbing ``SQLAlchemyError`` here would swallow ``ProgrammingError`` ("no such
    column"), turning a schema defect into a permanent, silent refusal of every
    put-selling structure — a prior review finding on this same file.
    """
    from sqlalchemy.exc import ProgrammingError

    class _Broken(_Account):
        def get_account_snapshot(self):
            raise ProgrammingError("SELECT cash", {}, Exception("no such column: cash"))

    with pytest.raises(ProgrammingError):
        _Broken().check_short_put_assignment_capacity(strike=200.0, contracts=1)
