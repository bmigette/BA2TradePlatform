"""The Live Trades header account filter keys on the ORDERS' account, not on the expert.

A ``Transaction`` has no ``account_id``. It belongs to an account through its
``TradingOrder`` rows -- which is exactly the rule the page already DISPLAYS: the
account-name column is read from the transaction's first order
(``_build_transaction_rows``). The loader's filter used to ask a different question:
it mapped the selected account to its ``ExpertInstance`` ids and kept transactions
whose ``expert_id`` was in that list, returning nothing at all when the account had
no experts. Two ways that lied about real money:

    * A MANUAL account (TastyTrade, production account 2) has zero experts, so the
      page went blank for it -- while "All" happily showed its trades.
    * Transactions written by the portfolio allocator or by hand have
      ``expert_id IS NULL`` even on expert-driven accounts, so they disappeared the
      moment their own account was selected (production transactions 184-191).

The fix routes the Live Trades loader through the same
``ui/components/account_scope.scope_transactions_to_account`` helper the Overview
widgets already use, so the whole UI answers "whose transaction is this?" once.

HOW IT RUNS. The real ``_transactions_data_loader`` is driven end to end against the
in-memory test database, so the query really executes; only ``_build_transaction_rows``
is stubbed, because it fetches live broker prices and margin factors that have nothing
to do with which rows were selected. The loader wraps its body in a broad
``except Exception`` that returns ``([], 0)``, so every assertion here also checks that
the loader logged no error -- otherwise a crash and a correctly-empty result would look
identical.
"""
import asyncio

import pytest

from ba2_trade_platform.core.types import (
    OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from ba2_trade_platform.ui.pages import live_trades as live_trades_mod
from ba2_trade_platform.ui.pages.live_trades import LiveTradesTab
from tests.factories import (
    create_account_definition, create_expert_instance, create_trading_order,
    create_transaction,
)


def _open_trade(account_id, symbol, expert_id=None, qty=10.0, open_price=100.0):
    """An OPENED transaction attributed to *account_id* by its filled order."""
    txn = create_transaction(
        symbol=symbol, quantity=qty, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=open_price,
        expert_id=expert_id,
    )
    create_trading_order(
        account_id=account_id, symbol=symbol, quantity=qty,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        status=OrderStatus.FILLED, transaction_id=txn.id,
        filled_qty=qty, open_price=open_price,
    )
    return txn


@pytest.fixture
def expert_account():
    """An account driven by an expert, as the page was originally written for."""
    account_id = create_account_definition(name='Automated', provider='Alpaca').id
    expert_id = create_expert_instance(account_id=account_id, alias='Alpha').id
    return account_id, expert_id


@pytest.fixture
def manual_account():
    """A TastyTrade-shaped account: real trades, NO expert instances at all."""
    return create_account_definition(name='Manual', provider='TastyTrade').id


@pytest.fixture
def loader(monkeypatch):
    """Drive the real loader; return ``load(selected_account_id) -> (ids, total)``.

    ``LiveTradesTab.__new__`` skips ``render()``, so none of the page's filter
    widgets exist; the loader's ``hasattr`` guards then fall back to its defaults
    (status Waiting/Open/Closing, no expert/symbol/broker-id filter), which is what
    the user sees on a freshly opened page.
    """
    tab = LiveTradesTab.__new__(LiveTradesTab)

    # Only the ROW BUILD is stubbed -- it fetches broker prices and margin factors.
    monkeypatch.setattr(
        tab, '_build_transaction_rows',
        lambda transactions, transaction_experts, session: [
            {'id': t.id, 'symbol': t.symbol} for t in transactions])

    errors = []

    class _Recorder:
        def error(self, msg, *a, **kw):
            errors.append(msg)

        def __getattr__(self, _name):  # debug/info/warning: ignored
            return lambda *a, **kw: None

    monkeypatch.setattr(live_trades_mod, 'logger', _Recorder())

    def load(selected_account_id):
        monkeypatch.setattr(live_trades_mod, 'get_selected_account_id',
                            lambda: selected_account_id)
        rows, total = asyncio.run(tab._transactions_data_loader(
            page=1, page_size=50, filters={}, sort_by='', descending=True))
        assert not errors, f"loader logged an error: {errors}"
        return sorted(r['id'] for r in rows), total

    return load


# ---------------------------------------------------------------------------
# The bug: an account with no experts, and an expertless transaction
# ---------------------------------------------------------------------------

def test_an_account_with_no_experts_shows_its_own_trades(
        loader, manual_account, expert_account):
    """Production account 2: zero experts, eight allocator transactions, blank page."""
    other_account, expert_id = expert_account
    mine = _open_trade(manual_account, 'AAPL', expert_id=None)
    _open_trade(other_account, 'MSFT', expert_id=expert_id)

    assert loader(manual_account) == ([mine.id], 1)


def test_selecting_the_expert_account_shows_only_its_trades(
        loader, manual_account, expert_account):
    other_account, expert_id = expert_account
    _open_trade(manual_account, 'AAPL', expert_id=None)
    theirs = _open_trade(other_account, 'MSFT', expert_id=expert_id)

    assert loader(other_account) == ([theirs.id], 1)


def test_all_shows_every_account(loader, manual_account, expert_account):
    """``None`` is the header's "All" -- the one value that widens the query."""
    other_account, expert_id = expert_account
    mine = _open_trade(manual_account, 'AAPL', expert_id=None)
    theirs = _open_trade(other_account, 'MSFT', expert_id=expert_id)

    assert loader(None) == (sorted([mine.id, theirs.id]), 2)


def test_a_hand_placed_trade_on_an_expert_account_is_not_hidden(
        loader, expert_account):
    """``expert_id IS NULL`` on an account that DOES have experts: still its trade."""
    account_id, expert_id = expert_account
    by_expert = _open_trade(account_id, 'MSFT', expert_id=expert_id)
    by_hand = _open_trade(account_id, 'AAPL', expert_id=None)

    assert loader(account_id) == (sorted([by_expert.id, by_hand.id]), 2)


def test_an_account_that_never_traded_shows_nothing(
        loader, manual_account, expert_account):
    """Empty must stay empty -- never widen to "every account" on no match."""
    other_account, expert_id = expert_account
    _open_trade(other_account, 'MSFT', expert_id=expert_id)

    assert loader(manual_account) == ([], 0)


def test_a_transaction_with_no_orders_belongs_to_no_account(loader, manual_account):
    """Unattributable, so it must not leak into a filtered view (it shows under All)."""
    orphan = create_transaction(symbol='ORPHAN', status=TransactionStatus.OPENED)

    assert loader(manual_account) == ([], 0)
    assert loader(None) == ([orphan.id], 1)


def test_the_loader_never_consults_the_expert_mapping(loader, monkeypatch,
                                                      manual_account):
    """The expert mapping is the wrong question here; asking it IS the bug.

    ``raising=False`` because the fixed page does not even import
    ``get_expert_ids_for_account`` any more -- the pin is "never called", which holds
    whether the name is absent or merely unused.
    """
    calls = []
    monkeypatch.setattr(live_trades_mod, 'get_expert_ids_for_account',
                        lambda account_id: calls.append(account_id), raising=False)
    mine = _open_trade(manual_account, 'AAPL', expert_id=None)

    assert loader(manual_account) == ([mine.id], 1)
    assert calls == []
