"""OPT-S1, live half — what the Alpaca adapter leaves behind when a combo submit fails.

The seam (``OptionsAccountInterface._unwind_failed_option_submission``) decides whether to
terminalise a failed combo's rows by asking whether the broker HAS the order. That question
is only answerable if the adapter persists ``broker_order_id`` the moment
``client.submit_order`` returns — before cache invalidation, response mapping and per-leg
matching, every one of which can raise.

Two windows, opposite required outcomes:

* the broker REJECTED (or was never reached) — parent and every leg child must go terminal,
  or the stranded short put is counted as live delivery obligation for ever;
* the broker ACCEPTED and the WRITE-BACK failed — nothing may go terminal, because the
  contracts are live and the assignment gate has to keep seeing them.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.option_types import OptionLeg
from ba2_trade_platform.core.types import (
    OptionRight, OrderDirection, OrderStatus, TransactionStatus,
)
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from tests.factories import create_account_definition

SEP = date(2026, 9, 18)


def _occ(strike, right=OptionRight.PUT):
    return f"ACN{SEP:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}{int(strike * 1000):08d}"


def _legs():
    """Bull put spread: short the 120 put, long the 110 put."""
    return [
        OptionLeg(contract_symbol=_occ(120.0), side=OrderDirection.SELL,
                  position_intent="sell_to_open", option_type=OptionRight.PUT,
                  strike=120.0, expiry=SEP, underlying="ACN"),
        OptionLeg(contract_symbol=_occ(110.0), side=OrderDirection.BUY,
                  position_intent="buy_to_open", option_type=OptionRight.PUT,
                  strike=110.0, expiry=SEP, underlying="ACN"),
    ]


@pytest.fixture
def alpaca(monkeypatch):
    acct_def = create_account_definition(provider="AlpacaAccount")
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = acct_def.id
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    monkeypatch.setattr(acct, "get_option_quote", lambda cs: None, raising=False)
    monkeypatch.setattr(acct, "invalidate_balance_cache", lambda: None, raising=False)
    return acct


def _submit(acct):
    return acct.submit_option_order(_legs(), quantity=2, order_type="limit",
                                    limit_price=-1.5, option_strategy="bull_put_spread")


def _rows():
    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    with Session(get_db().bind) as s:
        return list(s.exec(select(TradingOrder)).all())


def test_a_broker_rejection_terminalises_the_leg_children(alpaca):
    """Nothing at the broker -> nothing may stay in the book."""
    class Rejecting:
        def submit_order(self, req):
            raise RuntimeError("APIError: account not approved for level 3 options")
    alpaca.client = Rejecting()

    assert _submit(alpaca) is None
    terminal = OrderStatus.get_terminal_statuses()
    rows = _rows()
    assert len(rows) == 3            # parent + 2 legs
    left_open = [(o.id, o.contract_symbol, str(o.status)) for o in rows
                 if o.status not in terminal]
    assert left_open == [], f"rejected combo left non-terminal rows: {left_open}"
    assert alpaca.short_put_assignment_exposure().cost == 0.0
    assert alpaca.open_option_orders_book_wide() == []


def test_an_accepted_order_whose_writeback_fails_keeps_its_broker_id_and_stays_open(
        alpaca, monkeypatch):
    """The contracts are LIVE. The rows must survive, carrying the id that finds them."""
    accepted = SimpleNamespace(id="alpaca-abc-123", legs=None)

    class Accepting:
        def submit_order(self, req):
            return accepted
    alpaca.client = Accepting()

    def boom(order):
        raise ValueError("unexpected broker payload shape")
    monkeypatch.setattr(alpaca, "alpaca_order_to_tradingorder", boom, raising=False)

    assert _submit(alpaca) is None
    rows = _rows()
    parent = [o for o in rows if o.parent_order_id is None][0]
    assert parent.broker_order_id == "alpaca-abc-123", (
        "the broker id was not persisted before the write-back threw, so the seam cannot "
        "tell an accepted order from a rejected one")
    terminal = OrderStatus.get_terminal_statuses()
    assert parent.status not in terminal
    assert all(o.status not in terminal for o in rows if o.parent_order_id == parent.id)
    assert alpaca.short_put_assignment_exposure().cost > 0, (
        "a live short put stopped counting against assignment capacity")


def test_the_rejection_reason_reaches_the_leg_rows(alpaca):
    class Rejecting:
        def submit_order(self, req):
            raise RuntimeError("APIError: insufficient buying power")
    alpaca.client = Rejecting()
    _submit(alpaca)
    kids = [o for o in _rows() if o.parent_order_id is not None]
    assert kids and all("insufficient buying power" in (k.comment or "") for k in kids)
