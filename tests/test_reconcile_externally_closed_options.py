"""OPT-S4 — reconcile the OPTION book against the broker.

``reconcile_externally_closed_transactions`` deliberately SKIPS every transaction carrying
an option order (its guard: "get_positions() reports EQUITY positions only"), and delegates
option lifecycle to ``TradeManager._reconcile_account_option_activities`` — which reads only
OPASN/OPEXC/OPEXP/OPCSH over a 7-day window. Anything OUTSIDE that activity feed is invisible:
a manual close in the broker's own UI, a broker risk action, a position that simply is not
there any more.

Four candidate seams were each checked and none catches it. ``refresh_orders`` only updates
orders already in the local DB. ``refresh_transactions`` derives state purely from local
orders. ``refresh_positions`` only logs a count. ``option_lifecycle_service`` builds its book
from local OPENED transactions and its only broker call is a liveness check on EQUITY
positions. And ``AlpacaAccount.get_option_positions()`` — the one call that could answer the
question — had ZERO production callers.

So a broker-side close leaked the ledger position AND its buying-power reserve permanently,
because FILLED is not a terminal transaction status and nothing else was ever going to look.

THE FAIL-SAFE DIRECTION IS THE WHOLE DESIGN HERE. This reconciler CLOSES transactions, so
every unknown must mean "do nothing": an exception, a ``None`` book (fetch failed), a
position whose quantity cannot be read, and a structure only PARTLY absent all leave the
transaction alone. Reading a broker outage as "flat" is the 2026-07-03 incident that
force-closed 8 real open transactions, and on options it would also cancel the protective
long of a spread.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import Transaction, TradingOrder
from ba2_trade_platform.core.option_types import OptionPosition
from ba2_trade_platform.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)

EXPIRY = date(2026, 9, 18)
SHORT_PUT = "AAPL260918P00120000"
LONG_PUT = "AAPL260918P00110000"
LONG_CALL = "AAPL260918C00150000"


def _pos(contract, *, side=OrderDirection.SELL, qty=2.0, strike=120.0,
         right=OptionRight.PUT):
    return OptionPosition(
        contract_symbol=contract, underlying="AAPL", option_type=right, strike=strike,
        expiry=EXPIRY, side=side, quantity=qty, avg_entry_price=1.5, multiplier=100)


def _leg(acct_id, txn_id, contract, *, side, qty=2.0, strike=120.0,
         right=OptionRight.PUT, parent_id=None, status=OrderStatus.FILLED):
    return create_trading_order(
        account_id=acct_id, symbol=(contract if parent_id else "AAPL"),
        underlying_symbol="AAPL", quantity=qty, side=side,
        order_type=(OrderType.BUY_LIMIT if side == OrderDirection.BUY else OrderType.SELL_LIMIT),
        status=status, filled_qty=(qty if status == OrderStatus.FILLED else None),
        transaction_id=txn_id, asset_class=AssetClass.OPTION, multiplier=100,
        contract_symbol=contract, option_type=right, strike=strike, expiry=EXPIRY,
        parent_order_id=parent_id, broker_order_id=f"b-{contract}",
    )


def _spread(acct_id, *, age_minutes=120, with_resting_order=False):
    """An OPENED bull put spread: parent + short 120 put + long 110 put, all FILLED."""
    txn = create_transaction(symbol="AAPL", quantity=2.0, side=OrderDirection.SELL,
                             status=TransactionStatus.OPENED, open_price=1.5,
                             asset_class=AssetClass.OPTION, multiplier=100.0)
    txn.open_date = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    update_instance(txn)
    parent = create_trading_order(
        account_id=acct_id, symbol="AAPL", underlying_symbol="AAPL", quantity=2.0,
        side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
        status=OrderStatus.FILLED, filled_qty=2.0, transaction_id=txn.id,
        asset_class=AssetClass.OPTION, multiplier=100, option_strategy="bull_put_spread",
        expiry=EXPIRY, broker_order_id="b-parent",
        data={"option_reserve": 2_000.0},
    )
    _leg(acct_id, txn.id, SHORT_PUT, side=OrderDirection.SELL, parent_id=parent.id)
    _leg(acct_id, txn.id, LONG_PUT, side=OrderDirection.BUY, strike=110.0,
         parent_id=parent.id)
    if with_resting_order:
        create_trading_order(
            account_id=acct_id, symbol="AAPL", underlying_symbol="AAPL", quantity=2.0,
            side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
            status=OrderStatus.ACCEPTED, transaction_id=txn.id,
            asset_class=AssetClass.OPTION, multiplier=100, option_strategy="close",
            broker_order_id="b-resting",
        )
    return txn


def _single_long_call(acct_id, age_minutes=120):
    txn = create_transaction(symbol="AAPL", quantity=1.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=5.0,
                             asset_class=AssetClass.OPTION, multiplier=100.0)
    txn.open_date = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    update_instance(txn)
    _leg(acct_id, txn.id, LONG_CALL, side=OrderDirection.BUY, qty=1.0, strike=150.0,
         right=OptionRight.CALL)
    return txn


@pytest.fixture
def account():
    acct_def = create_account_definition()
    acct = MockAccount(acct_def.id)
    acct._positions = []
    acct._option_positions = []
    return acct


def _status(txn_id):
    return get_instance(Transaction, txn_id).status


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------

def test_a_spread_closed_at_the_broker_is_reconciled(account):
    txn = _spread(account.id)
    account._option_positions = []           # the broker holds neither leg

    closed = account.reconcile_externally_closed_option_transactions()

    assert closed == 1
    assert _status(txn.id) == TransactionStatus.CLOSED
    assert get_instance(Transaction, txn.id).close_reason == "position_not_at_broker"


def test_the_buying_power_reserve_is_released(account):
    """The half that leaks money. FILLED is not terminal, so the order stays in the
    account-wide book — and keeps its $2,000 reserve charged — until its TRANSACTION
    closes. Nothing was ever going to close it."""
    txn = _spread(account.id)
    account._option_positions = []
    assert account.reserved_option_buying_power() == 2_000.0

    account.reconcile_externally_closed_option_transactions()

    assert account.reserved_option_buying_power() == 0.0, (
        "the reserve of a structure the broker no longer holds is still charged")


def test_a_single_leg_position_closed_at_the_broker_is_reconciled(account):
    txn = _single_long_call(account.id)
    account._option_positions = []
    assert account.reconcile_externally_closed_option_transactions() == 1
    assert _status(txn.id) == TransactionStatus.CLOSED


def test_a_resting_close_order_is_cancelled_with_the_transaction(account):
    """It can never fill correctly — the broker released the position already."""
    txn = _spread(account.id, with_resting_order=True)
    account._option_positions = []
    account.reconcile_externally_closed_option_transactions()

    from sqlmodel import Session, select
    from ba2_trade_platform.core.db import get_db
    with Session(get_db().bind) as s:
        resting = [o for o in s.exec(select(TradingOrder)).all()
                   if o.broker_order_id == "b-resting"]
    assert resting and resting[0].status == OrderStatus.CANCELED


# ---------------------------------------------------------------------------
# every unknown must mean "do nothing"
# ---------------------------------------------------------------------------

def test_a_position_still_held_at_the_broker_is_left_alone(account):
    txn = _spread(account.id)
    account._option_positions = [
        _pos(SHORT_PUT), _pos(LONG_PUT, side=OrderDirection.BUY, strike=110.0)]
    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_PARTLY_closed_structure_is_left_alone(account):
    """One leg gone is not a closed structure — and closing the row here would strand
    the surviving leg with nothing able to manage or flatten it."""
    txn = _spread(account.id)
    account._option_positions = [_pos(LONG_PUT, side=OrderDirection.BUY, strike=110.0)]
    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_fetch_FAILURE_closes_nothing(account):
    """``None`` is "we could not look", not "the book is empty"."""
    txn = _spread(account.id)
    account.get_option_positions = lambda: None
    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_raising_broker_closes_nothing(account):
    txn = _spread(account.id)

    def boom():
        raise RuntimeError("Failed to resolve 'api.alpaca.markets'")
    account.get_option_positions = boom

    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


@pytest.mark.parametrize("bad_qty", [None, float("nan"), "", "n/a"])
def test_a_position_with_an_unreadable_quantity_counts_as_STILL_HELD(account, bad_qty):
    """A broker reporting a position but not its size is saying the position EXISTS.

    SINGLE-LEG ON PURPOSE. The first version of this test used the two-leg spread and put
    the unreadable quantity on ONE leg — so the OTHER leg, plainly still held, kept the
    transaction open all by itself and the assertion could not fail. Making the unreadable
    quantity read as "flat" left it green. Here the unreadable position is the ONLY thing
    standing between the transaction and a force-close.
    """
    txn = _single_long_call(account.id)
    bad = _pos(LONG_CALL, side=OrderDirection.BUY, qty=1.0, strike=150.0,
               right=OptionRight.CALL)
    bad.quantity = bad_qty
    account._option_positions = [bad]

    assert account.reconcile_externally_closed_option_transactions() == 0, (
        f"a position whose quantity reads {bad_qty!r} was treated as flat and the "
        f"transaction was force-closed")
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_position_the_broker_reports_as_ZERO_is_genuinely_flat(account):
    """The other side of the same rule: a MEASURED zero is a close signal.

    Without this, "treat everything as held" would pass the test above and quietly
    disable the reconciler.
    """
    txn = _single_long_call(account.id)
    flat = _pos(LONG_CALL, side=OrderDirection.BUY, qty=0.0, strike=150.0,
                right=OptionRight.CALL)
    account._option_positions = [flat]

    assert account.reconcile_externally_closed_option_transactions() == 1
    assert _status(txn.id) == TransactionStatus.CLOSED


def test_a_freshly_opened_structure_is_inside_the_grace_period(account):
    txn = _spread(account.id, age_minutes=1)
    account._option_positions = []
    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_an_equity_transaction_is_not_touched_by_the_option_reconciler(account):
    """The two reconcilers must not overlap: this one owns option rows only."""
    txn = create_transaction(symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
                             status=TransactionStatus.OPENED, open_price=150.0)
    txn.open_date = datetime.now(timezone.utc) - timedelta(minutes=120)
    update_instance(txn)
    create_trading_order(
        account_id=account.id, symbol="AAPL", quantity=100.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=100.0,
        transaction_id=txn.id)
    account._option_positions = []
    assert account.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_transaction_whose_contracts_all_net_flat_is_not_closed_here(account):
    """Already balanced by our OWN orders — ``refresh_transactions`` owns that case.

    Closing it here would report an EXTERNAL close for a position the platform itself
    closed, putting the wrong reason in the ledger.
    """
    txn = _spread(account.id)
    _leg(account.id, txn.id, SHORT_PUT, side=OrderDirection.BUY)
    _leg(account.id, txn.id, LONG_PUT, side=OrderDirection.SELL, strike=110.0)
    account._option_positions = []
    assert account.reconcile_externally_closed_option_transactions() == 0


def test_another_accounts_structure_is_not_reconciled(account):
    """The broker book fetched here belongs to THIS account only."""
    other_def = create_account_definition(name="Other")
    other = MockAccount(other_def.id)
    other._option_positions = []
    txn = _spread(account.id)

    assert other.reconcile_externally_closed_option_transactions() == 0
    assert _status(txn.id) == TransactionStatus.OPENED


def test_a_stuck_CLOSING_transaction_is_reconciled_too(account):
    """Same reason the equity reconciler includes CLOSING: our close attempt failed
    repeatedly while the position went away at the broker."""
    txn = _spread(account.id)
    txn.status = TransactionStatus.CLOSING
    update_instance(txn)
    account._option_positions = []
    assert account.reconcile_externally_closed_option_transactions() == 1
    assert _status(txn.id) == TransactionStatus.CLOSED


# ---------------------------------------------------------------------------
# the live adapter's tri-state, and the wiring that gives the call a caller
# ---------------------------------------------------------------------------

def test_alpaca_get_option_positions_reports_a_fetch_failure_as_None():
    """``client.get_all_positions() or []`` turned an outage into a confirmed-flat book.

    Harmless while the method had no production caller; with the reconciler wired it is
    the difference between "do nothing" and "close every option transaction and cancel
    the protective long of every spread".
    """
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 1
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}

    class Down:
        def get_all_positions(self):
            raise RuntimeError("Failed to resolve 'api.alpaca.markets' (DNS outage)")
    acct.client = Down()
    assert acct.get_option_positions() is None

    class Silent:
        def get_all_positions(self):
            return None
    acct.client = Silent()
    assert acct.get_option_positions() is None


def test_alpaca_get_option_positions_reports_a_real_empty_book_as_empty_list():
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 1
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}

    class Flat:
        def get_all_positions(self):
            return []
    acct.client = Flat()
    assert acct.get_option_positions() == []


def test_the_refresh_loop_actually_calls_the_option_reconciler():
    """``get_option_positions()`` had ZERO production callers; a reconciler nobody runs
    fixes nothing. Asserted on the SOURCE of the refresh loop because standing the whole
    TradeManager pass up needs the live registry, but the call site is one line."""
    import inspect
    from ba2_trade_platform.core import TradeManager as TM

    src = inspect.getsource(TM)
    assert "account.reconcile_externally_closed_option_transactions()" in src, (
        "the option reconciler is never invoked by the live refresh loop")
    equity_at = src.index("account.reconcile_externally_closed_transactions()")
    option_at = src.index("account.reconcile_externally_closed_option_transactions()")
    assert equity_at < option_at, "the two reconcilers should sit together, equity first"
