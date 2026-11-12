"""The live account refresh force-closes a position whose stop was breached but never filled.

WHY THIS EXISTS. Until APP 2026.09.1261 Alpaca OCO exits had a stop-LIMIT stop leg, its
limit ``OCO_STOP_LIMIT_CUSHION`` (0.5%) *through* the stop. On a GAP that jumps the stop AND
the cushion in one print, the triggered leg becomes a limit sell above the market: it rests
unfilled and the position is left open with no stop under it (prod RARE/ENOV 2026-09-10,
PLTR 2026-11-03). The backtest models every stop as a MARKET stop that fills at the stop or
at the gap open (``backtest_account.py::_gap_stop_fill``).

New Alpaca OCO stop legs are PLAIN stops (pinned below), so they fill like the backtest.
``TradeManager._force_close_breached_stops`` stays as the backstop: on each periodic account
refresh, any OPENED equity transaction whose price is beyond its stop by more than the
cushion is force-closed at market (legacy stop-limit legs still resting, IBKR stop-limit
legs, a stop that is held or missing).

Log assertions record ``TradeManager.logger`` directly: ``ba2_common.logger`` sets
``propagate = False``, so ``caplog`` sees nothing and every log pin would pass vacuously.
"""
import pytest
from unittest.mock import MagicMock

from ba2_trade_platform.core.TradeManager import TradeManager, OCO_STOP_LIMIT_CUSHION
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import (
    AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus,
)
from tests.factories import (
    create_account_definition, create_trading_order, create_transaction,
)


pytestmark = pytest.mark.usefixtures("reset_test_db")

STOP = 100.0
# A long is breached only BELOW this; a short only ABOVE its mirror.
LONG_TRIGGER = STOP * (1.0 - OCO_STOP_LIMIT_CUSHION)   # 99.50
SHORT_TRIGGER = STOP * (1.0 + OCO_STOP_LIMIT_CUSHION)  # 100.50


class _LogRecorder:
    """Records what the manager logged, per level."""

    def __init__(self):
        self.warning_msgs = []
        self.error_msgs = []
        self.info_msgs = []
        self.debug_msgs = []

    def warning(self, msg, *a, **k):
        self.warning_msgs.append(str(msg))

    def error(self, msg, *a, **k):
        self.error_msgs.append(str(msg))

    def info(self, msg, *a, **k):
        self.info_msgs.append(str(msg))

    def debug(self, msg, *a, **k):
        self.debug_msgs.append(str(msg))


class _FakeAccount:
    """A live-shaped account double that records every call the safety net makes."""

    def __init__(self, account_id, prices, positions, raise_on_close=(), market_open=True):
        self.id = account_id
        self._market_open = market_open
        self.market_open_calls = 0
        self._prices = prices          # symbol -> price (None == unavailable)
        self._positions = positions    # symbol -> signed qty (None == unreadable book)
        self._raise_on_close = set(raise_on_close)
        self.price_calls = []
        self.position_calls = []
        self.closed = []

    def is_market_open(self, *, now=None):
        self.market_open_calls += 1
        return self._market_open

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        self.price_calls.append(symbol_or_symbols)
        if isinstance(symbol_or_symbols, str):
            return self._prices[symbol_or_symbols]
        return {s: self._prices[s] for s in symbol_or_symbols}

    def get_signed_position_quantity(self, symbol):
        self.position_calls.append(symbol)
        return self._positions[symbol]

    def close_transaction(self, transaction_id):
        if transaction_id in self._raise_on_close:
            raise RuntimeError("broker refused the cancel")
        self.closed.append(transaction_id)
        return {
            'success': True,
            'message': 'closing',
            'canceled_count': 1,
            'deleted_count': 0,
            'close_order_id': 4242,
        }


def _position(account, symbol, side=OrderDirection.BUY, stop_loss=STOP,
              status=TransactionStatus.OPENED, **txn_kwargs):
    """An OPENED transaction of ``account`` (the link is through its order)."""
    txn = create_transaction(symbol=symbol, side=side, stop_loss=stop_loss,
                             status=status, quantity=10.0, open_price=STOP,
                             **txn_kwargs)
    create_trading_order(account_id=account.id, symbol=symbol, side=side,
                         quantity=10.0, transaction_id=txn.id,
                         order_type=OrderType.MARKET, status=OrderStatus.FILLED)
    return txn


def _run(account, monkeypatch=None):
    """Run the safety net with a recording logger; returns (manager, recorder)."""
    tm = TradeManager()
    rec = _LogRecorder()
    tm.logger = rec
    tm._force_close_breached_stops(account)
    return tm, rec


# --------------------------------------------------------------------------- #
# 1-4: the breach rule itself
# --------------------------------------------------------------------------- #

def test_long_gapped_through_stop_and_cushion_is_force_closed(monkeypatch):
    acct_def = create_account_definition()
    txn = _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 0.01}, {"RARE": 10.0})

    logged = []
    monkeypatch.setattr("ba2_common.core.utils.log_close_order_activity",
                        lambda **kw: logged.append(kw))

    _, rec = _run(account)

    assert account.closed == [txn.id]
    assert len(logged) == 1
    assert logged[0]["transaction"].id == txn.id
    assert logged[0]["account_id"] == acct_def.id
    assert logged[0]["success"] is True
    assert logged[0]["close_order_id"] == 4242
    assert any("RARE" in m for m in rec.warning_msgs)
    # The activity carries the reason the operator has to be able to search for.
    assert get_instance(Transaction, txn.id).close_reason == "stop_breached_forced_close"


def test_long_inside_the_cushion_is_left_alone():
    """The triggered stop-limit can still fill here — do not pre-empt the broker."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER + 0.01}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_long_above_its_stop_is_left_alone():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": STOP + 5.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []
    assert account.position_calls == []  # no broker round-trip for a healthy position


def test_short_gapped_up_through_stop_and_cushion_is_force_closed():
    acct_def = create_account_definition()
    txn = _position(acct_def, "ENOV", side=OrderDirection.SELL)
    account = _FakeAccount(acct_def.id, {"ENOV": SHORT_TRIGGER + 0.01}, {"ENOV": -10.0})

    _run(account)

    assert account.closed == [txn.id]


# --------------------------------------------------------------------------- #
# 5-9: refusing to act on anything unverified
# --------------------------------------------------------------------------- #

def test_position_no_longer_held_at_broker_is_not_closed():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 0.0})

    _, rec = _run(account)

    assert account.closed == []
    assert any("RARE" in m for m in rec.warning_msgs)


def test_unreadable_position_book_is_not_closed_on_a_guess():
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": None})

    _, rec = _run(account)

    assert account.closed == []
    assert any("RARE" in m for m in rec.warning_msgs)


@pytest.mark.parametrize("bad_price", [None, 0.0, -3.0, float("nan")])
def test_an_unusable_price_skips_that_symbol_and_still_processes_the_rest(bad_price):
    """UNKNOWN IS NOT SAFE — but it is also not a close signal. Skip loudly, carry on."""
    acct_def = create_account_definition()
    _position(acct_def, "AAA")
    breached = _position(acct_def, "BBB")
    account = _FakeAccount(
        acct_def.id,
        {"AAA": bad_price, "BBB": LONG_TRIGGER - 1.0},
        {"AAA": 10.0, "BBB": 10.0},
    )

    _, rec = _run(account)

    assert account.closed == [breached.id]
    assert any("AAA" in m for m in rec.warning_msgs)


def test_a_transaction_already_closing_is_untouched():
    """close_transaction moved it to CLOSING on the previous refresh — idempotent."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE", status=TransactionStatus.CLOSING)
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_option_transactions_are_skipped():
    """An option Transaction's symbol is the UNDERLYING and its stop is not a share price."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE", asset_class=AssetClass.OPTION, multiplier=100)
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []
    assert account.price_calls == []  # nothing to watch -> no price fetch at all


def test_a_transaction_without_a_stop_is_skipped():
    acct_def = create_account_definition()
    _position(acct_def, "RARE", stop_loss=None)
    account = _FakeAccount(acct_def.id, {"RARE": 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


def test_one_failing_transaction_does_not_abort_the_others():
    acct_def = create_account_definition()
    boom = _position(acct_def, "AAA")
    survivor = _position(acct_def, "BBB")
    account = _FakeAccount(
        acct_def.id,
        {"AAA": LONG_TRIGGER - 1.0, "BBB": LONG_TRIGGER - 1.0},
        {"AAA": 10.0, "BBB": 10.0},
        raise_on_close=[boom.id],
    )

    _, rec = _run(account)

    assert account.closed == [survivor.id]
    assert any("AAA" in m for m in rec.error_msgs)


def test_prices_are_fetched_in_one_bulk_call():
    acct_def = create_account_definition()
    _position(acct_def, "AAA")
    _position(acct_def, "BBB")
    account = _FakeAccount(acct_def.id, {"AAA": 200.0, "BBB": 200.0},
                           {"AAA": 10.0, "BBB": 10.0})

    _run(account)

    assert len(account.price_calls) == 1
    assert sorted(account.price_calls[0]) == ["AAA", "BBB"]


def test_other_accounts_positions_are_not_touched():
    mine = create_account_definition(name="Mine")
    theirs = create_account_definition(name="Theirs")
    _position(theirs, "RARE")
    account = _FakeAccount(mine.id, {"RARE": LONG_TRIGGER - 1.0}, {"RARE": 10.0})

    _run(account)

    assert account.closed == []


# --------------------------------------------------------------------------- #
# 10: ONE cushion, TWO readers
# --------------------------------------------------------------------------- #

def test_the_check_reads_the_alpaca_module_constant():
    # NOTE: `import ...accounts.AlpacaAccount as m` yields the CLASS here — the package
    # __init__ re-exports it under the submodule's own name — so import the name itself.
    from ba2_trade_platform.modules.accounts.AlpacaAccount import (
        OCO_STOP_LIMIT_CUSHION as alpaca_cushion,
    )

    assert OCO_STOP_LIMIT_CUSHION is alpaca_cushion
    assert OCO_STOP_LIMIT_CUSHION == 0.005


@pytest.mark.parametrize("side", [OrderDirection.SELL, OrderDirection.BUY])
def test_the_oco_stop_leg_is_a_plain_stop(side):
    """The submitted OCO's stop leg carries NO limit price: triggered, it is a market order
    that fills at the stop or the gap open, like the backtest's _gap_stop_fill."""
    from alpaca.trading.enums import OrderClass
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    acct_def = create_account_definition(provider="Alpaca")
    order = create_trading_order(
        account_id=acct_def.id, symbol="RARE", side=side, quantity=10.0,
        order_type=OrderType.OCO, status=OrderStatus.PENDING,
        limit_price=140.0 if side == OrderDirection.SELL else 60.0,
        stop_price=STOP, good_for="gtc",
    )

    account = object.__new__(AlpacaAccount)
    account.id = acct_def.id
    account.client = MagicMock()
    account._margin_info_cache = {}

    captured = []

    def _capture(request):
        captured.append(request)
        raise RuntimeError("stop before the broker call")

    account.client.submit_order.side_effect = _capture

    account._submit_order_impl(order)

    assert len(captured) == 1
    request = captured[0]
    assert request.order_class == OrderClass.OCO
    assert float(request.stop_loss.stop_price) == STOP
    assert request.stop_loss.limit_price is None
    assert request.take_profit.limit_price is not None


# --------------------------------------------------------------------------- #
# Market-hours gate (operator decision 2026-09-10: only act while the market is open)
# --------------------------------------------------------------------------- #

def test_market_closed_means_no_price_read_and_no_close():
    """After hours the latest quote may be a stale or one-sided print; the sweep must not
    act on it. Nothing is read and nothing is closed -- the next in-hours refresh decides."""
    acct_def = create_account_definition()
    _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 0.01}, {"RARE": 10.0},
                           market_open=False)

    _, rec = _run(account)

    assert account.market_open_calls == 1
    assert account.price_calls == []
    assert account.position_calls == []
    assert account.closed == []
    assert any("market not open" in m for m in rec.debug_msgs)


def test_market_open_is_consulted_before_any_close():
    acct_def = create_account_definition()
    txn = _position(acct_def, "RARE")
    account = _FakeAccount(acct_def.id, {"RARE": LONG_TRIGGER - 0.01}, {"RARE": 10.0})

    _run(account)

    assert account.market_open_calls == 1
    assert account.closed == [txn.id]


# --------------------------------------------------------------------------- #
# The forced close never sells more than THIS transaction still holds
#
# Dev 8080 NVDA transaction 468, 2026-11-09: long 4; its OCO stop leg (order 1560) sold 1
# share at 14:30:59 and was then cancelled by this safety net at 14:31:27, which staged the
# close (WAITING_TRIGGER on 1560's CANCELED) at the 4 the DB still believed were held. When
# the refresh synced 1560 CANCELED with filled_qty 1, the trigger sweep submitted the close
# for 4: 5 shares sold for a 4-share position, the 5th out of another NVDA transaction.
#
# These run the REAL chain: the safety net -> AccountInterface.close_transaction (cancel the
# legs, stage the close) -> the refresh confirms the cancel -> the WAITING_TRIGGER sweep sizes
# and submits the close. The broker is a fake order book; nothing reaches a real API.
# --------------------------------------------------------------------------- #

from datetime import datetime, timedelta, timezone  # noqa: E402

from ba2_trade_platform.core.db import update_instance  # noqa: E402
from ba2_trade_platform.core.models import TradingOrder  # noqa: E402
from tests.conftest import MockAccount  # noqa: E402

NVDA_STOP = 178.71
NVDA_BREACH_PRICE = 171.96
T0 = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


class _BrokerBook(MockAccount):
    """``MockAccount`` (so ``close_transaction`` / ``submit_close_order_for_transaction`` are
    the real AccountInterface code) whose broker side is a dict order book.

    ``book`` maps broker_order_id -> {"status": OrderStatus, "filled_qty": float}; a missing
    id (or None) means the broker could not be read for that order."""

    def __init__(self, account_id, *, book, held=10.0, price=NVDA_BREACH_PRICE):
        super().__init__(account_id)
        self._prices = {"NVDA": price}
        self._held = held
        self.book = book
        self.cancels = []
        self.sent = []
        self.order_reads = []

    def is_market_open(self, *, now=None):
        return True

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        if isinstance(symbol_or_symbols, str):
            return self._prices[symbol_or_symbols]
        return {s: self._prices[s] for s in symbol_or_symbols}

    def get_signed_position_quantity(self, symbol):
        return self._held

    def get_available_position_quantity(self, symbol):
        return 1_000.0

    def cancel_order(self, order_id):
        """What a broker adapter does: request the cancel, mark the row PENDING_CANCEL."""
        from sqlmodel import select
        from ba2_common.core.db import get_db
        self.cancels.append(order_id)
        with get_db() as session:
            row = session.exec(select(TradingOrder).where(
                TradingOrder.broker_order_id == order_id)).first()
            row.status = OrderStatus.PENDING_CANCEL
            session.add(row)
            session.commit()
        return True

    def get_order(self, order_id):
        self.order_reads.append(order_id)
        state = self.book.get(order_id)
        if state is None:
            return None
        return TradingOrder(broker_order_id=order_id, symbol="NVDA",
                            status=state["status"], filled_qty=state["filled_qty"])

    def submit_order(self, order, is_closing_order=False):
        self.sent.append((order.side, float(order.quantity), is_closing_order))
        row = get_instance(TradingOrder, order.id)
        row.quantity = order.quantity
        row.broker_order_id = f"brk-close-{order.id}"
        row.status = OrderStatus.ACCEPTED
        update_instance(row)
        return row


def _nvda_long_with_oco(acct_def):
    """Transaction 468's shape: long 4, a FILLED entry, an Alpaca OCO (parent row = the TP
    limit, child row = the stop leg) resting at the broker."""
    txn = create_transaction(symbol="NVDA", side=OrderDirection.BUY, quantity=4.0,
                             status=TransactionStatus.OPENED, open_price=207.775,
                             stop_loss=NVDA_STOP, take_profit=240.0)
    entry = create_trading_order(
        account_id=acct_def.id, symbol="NVDA", quantity=4.0, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=4.0,
        open_price=207.775, transaction_id=txn.id, broker_order_id="brk-entry",
        created_at=T0)
    oco = create_trading_order(
        account_id=acct_def.id, symbol="NVDA", quantity=4.0, side=OrderDirection.SELL,
        order_type=OrderType.OCO, status=OrderStatus.ACCEPTED, filled_qty=0.0,
        limit_price=240.0, stop_price=NVDA_STOP, transaction_id=txn.id,
        depends_on_order=entry.id, depends_order_status_trigger=OrderStatus.FILLED,
        broker_order_id="brk-oco", created_at=T0 + timedelta(minutes=1))
    stop_leg = create_trading_order(
        account_id=acct_def.id, symbol="NVDA", quantity=4.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, status=OrderStatus.ACCEPTED, filled_qty=0.0,
        stop_price=NVDA_STOP, transaction_id=txn.id, parent_order_id=oco.id,
        broker_order_id="brk-stop", created_at=T0 + timedelta(minutes=2))
    return txn, entry, oco, stop_leg


def _set(order_id, status, filled_qty):
    row = get_instance(TradingOrder, order_id)
    row.status = status
    row.filled_qty = filled_qty
    update_instance(row)


def _deferred_close_row(txn, exclude):
    rows = [o for o in _orders_of(txn.id)
            if o.order_type == OrderType.MARKET and o.id not in exclude
            and (o.comment or "").startswith("Closing position for transaction")]
    assert len(rows) == 1, rows
    return rows[0]


def _orders_of(txn_id):
    from sqlmodel import select
    from ba2_common.core.db import get_db
    with get_db() as session:
        rows = session.exec(select(TradingOrder).where(
            TradingOrder.transaction_id == txn_id)).all()
        for r in rows:
            session.expunge(r)
        return rows


def _breach_and_stage(monkeypatch, account):
    """Pass 1: the safety net fires; close_transaction cancels both OCO rows and stages the
    close behind the stop leg's cancel. Nothing may reach the broker yet."""
    import ba2_trade_platform.modules.accounts as accounts
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: (lambda _id: account))
    monkeypatch.setattr("ba2_common.core.utils.log_close_order_activity", lambda **kw: None)
    _run(account)
    assert sorted(account.cancels) == ["brk-oco", "brk-stop"]
    assert account.sent == []


def _sweep(account):
    """Pass 2: the WAITING_TRIGGER sweep (refresh step 2)."""
    tm = TradeManager()
    rec = _LogRecorder()
    tm.logger = rec
    tm._check_all_waiting_trigger_orders()
    return rec


@pytest.mark.parametrize("db_saw_the_fill", [False, True])
def test_partial_stop_fill_found_after_the_cancel_closes_only_the_remainder(
        monkeypatch, db_saw_the_fill):
    """1 of 4 sold by the stop leg before the cancel landed -> the market close is for 3.

    ``db_saw_the_fill`` False: the refresh marked the leg CANCELED but the DB copy of its
    filled_qty is stale -- the broker's own final figure must be what sizes the close."""
    acct_def = create_account_definition()
    txn, _entry, oco, stop_leg = _nvda_long_with_oco(acct_def)
    book = {"brk-entry": {"status": OrderStatus.FILLED, "filled_qty": 4.0},
            "brk-oco": {"status": OrderStatus.CANCELED, "filled_qty": 0.0},
            "brk-stop": {"status": OrderStatus.CANCELED, "filled_qty": 1.0}}
    account = _BrokerBook(acct_def.id, book=book)
    _breach_and_stage(monkeypatch, account)
    close = _deferred_close_row(txn, exclude=())
    assert close.status == OrderStatus.WAITING_TRIGGER
    assert close.depends_on_order == stop_leg.id
    assert close.quantity == pytest.approx(4.0)   # staged before the fill was known

    # The refresh confirms both cancels.
    _set(oco.id, OrderStatus.CANCELED, 0.0)
    _set(stop_leg.id, OrderStatus.CANCELED, 1.0 if db_saw_the_fill else 0.0)

    rec = _sweep(account)

    assert account.sent == [(OrderDirection.SELL, pytest.approx(3.0), True)]
    assert "brk-stop" in account.order_reads and "brk-oco" in account.order_reads
    assert get_instance(TradingOrder, close.id).quantity == pytest.approx(3.0)
    assert any("3" in m and str(txn.id) in m for m in rec.warning_msgs)


def test_stop_fully_filled_during_the_cancel_race_sends_no_market_order(monkeypatch):
    """The stop leg filled all 4 before the cancel reached it: the position is already flat,
    so the staged close must never be sent (it would sell 4 shares of OTHER positions)."""
    acct_def = create_account_definition()
    txn, _entry, oco, stop_leg = _nvda_long_with_oco(acct_def)
    book = {"brk-entry": {"status": OrderStatus.FILLED, "filled_qty": 4.0},
            "brk-oco": {"status": OrderStatus.CANCELED, "filled_qty": 0.0},
            "brk-stop": {"status": OrderStatus.FILLED, "filled_qty": 4.0}}
    account = _BrokerBook(acct_def.id, book=book)
    _breach_and_stage(monkeypatch, account)
    close = _deferred_close_row(txn, exclude=())

    _set(oco.id, OrderStatus.CANCELED, 0.0)
    _set(stop_leg.id, OrderStatus.FILLED, 4.0)

    rec = _sweep(account)
    _sweep(account)   # and it stays unsent on every later pass

    assert account.sent == []
    row = get_instance(TradingOrder, close.id)
    assert row.status == OrderStatus.CLOSED
    assert row.broker_order_id is None
    assert any(str(txn.id) in m and "flat" in m.lower() for m in rec.warning_msgs)
    # The fills alone now close the transaction on the next transaction refresh.
    assert get_instance(Transaction, txn.id).get_current_open_qty() == pytest.approx(0.0)


@pytest.mark.parametrize("broker_state", [
    None,                                                          # unreadable
    {"status": OrderStatus.PENDING_CANCEL, "filled_qty": 1.0},     # cancel not confirmed
])
def test_cancel_outcome_unknown_sends_nothing_this_pass_and_logs_error(
        monkeypatch, broker_state):
    """The DB says the stop leg is CANCELED, but the broker cannot confirm its final state.
    No market order this pass (an oversell is worse than one more pass), an ERROR, and the
    close stays staged so the next pass retries it."""
    acct_def = create_account_definition()
    txn, _entry, oco, stop_leg = _nvda_long_with_oco(acct_def)
    book = {"brk-entry": {"status": OrderStatus.FILLED, "filled_qty": 4.0},
            "brk-oco": {"status": OrderStatus.CANCELED, "filled_qty": 0.0},
            "brk-stop": broker_state}
    account = _BrokerBook(acct_def.id, book=book)
    _breach_and_stage(monkeypatch, account)
    close = _deferred_close_row(txn, exclude=())

    _set(oco.id, OrderStatus.CANCELED, 0.0)
    _set(stop_leg.id, OrderStatus.CANCELED, 0.0)

    rec = _sweep(account)

    assert account.sent == []
    assert get_instance(TradingOrder, close.id).status == OrderStatus.WAITING_TRIGGER
    assert any(str(close.id) in m and str(txn.id) in m for m in rec.error_msgs)

    # Next pass: the broker answers (CANCELED after selling 1) -> close the remaining 3.
    book["brk-stop"] = {"status": OrderStatus.CANCELED, "filled_qty": 1.0}
    _sweep(account)
    assert account.sent == [(OrderDirection.SELL, pytest.approx(3.0), True)]


def test_leg_still_pending_cancel_in_the_db_is_not_released():
    """The refresh has not confirmed the cancel yet: the staged close keeps waiting (this
    already held before the fix; pinned so the new sizing step cannot bypass it)."""
    acct_def = create_account_definition()
    txn, _entry, _oco, stop_leg = _nvda_long_with_oco(acct_def)
    close = create_trading_order(
        account_id=acct_def.id, symbol="NVDA", quantity=4.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.WAITING_TRIGGER,
        transaction_id=txn.id, depends_on_order=stop_leg.id,
        depends_order_status_trigger=OrderStatus.CANCELED,
        comment=f"Closing position for transaction {txn.id}")
    _set(stop_leg.id, OrderStatus.PENDING_CANCEL, 0.0)

    sent = []

    class _Never(MockAccount):
        def submit_order(self, order, is_closing_order=False):
            sent.append(order.id)

    import ba2_trade_platform.modules.accounts as accounts
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(accounts, "get_account_class", lambda provider: _Never)
        _sweep(None)

    assert sent == []
    assert get_instance(TradingOrder, close.id).status == OrderStatus.WAITING_TRIGGER


def _oversold_transaction(acct_def):
    """Transaction 468 AFTER the incident: bought 4, the stop leg sold 1 before its cancel,
    the forced close sold 4 -> 5 sold, the transaction CLOSING."""
    txn, entry, oco, stop_leg = _nvda_long_with_oco(acct_def)
    _set(oco.id, OrderStatus.CANCELED, 0.0)
    _set(stop_leg.id, OrderStatus.CANCELED, 1.0)
    create_trading_order(
        account_id=acct_def.id, symbol="NVDA", quantity=4.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=4.0,
        open_price=174.32, transaction_id=txn.id, depends_on_order=stop_leg.id,
        depends_order_status_trigger=OrderStatus.CANCELED, broker_order_id="brk-close",
        comment=f"Closing position for transaction {txn.id}",
        created_at=T0 + timedelta(minutes=30))
    row = get_instance(Transaction, txn.id)
    row.status = TransactionStatus.CLOSING
    update_instance(row)
    return txn


def test_an_oversold_transaction_is_reported_once_and_not_rewritten_by_abs(monkeypatch):
    """refresh_transactions used to log "NEGATIVE calculated_quantity ... Using abs() as
    safety measure" on EVERY refresh and write quantity = abs(-1) = 1: a 1-share LONG that
    does not exist. Now: one ERROR per transaction per process naming the oversell, and the
    quantity is left alone."""
    import sys
    from unittest.mock import MagicMock
    ro = sys.modules["ba2_common.core.interfaces.ReadOnlyAccountInterface"]
    log = MagicMock()
    monkeypatch.setattr(ro, "logger", log)
    monkeypatch.setattr(ro, "_OVERSOLD_REPORTED", set(), raising=False)

    acct_def = create_account_definition()
    txn = _oversold_transaction(acct_def)
    account = MockAccount(acct_def.id)

    account.refresh_transactions()
    account.refresh_transactions()
    account.refresh_transactions()

    errors = [str(c.args[0]) for c in log.error.call_args_list]
    oversold = [m for m in errors if "OVERSOLD" in m and f"transaction {txn.id}" in m]
    assert len(oversold) == 1, errors
    assert "sold 5" in oversold[0] and "bought 4" in oversold[0]
    assert not any("abs()" in m for m in errors)
    row = get_instance(Transaction, txn.id)
    assert row.quantity == pytest.approx(4.0), "the oversell must not be rewritten as a long"
    assert row.status == TransactionStatus.CLOSING


def test_normal_breach_with_no_fills_still_closes_all_four(monkeypatch):
    """The ordinary case is unchanged: nothing filled before the cancel -> close all 4."""
    acct_def = create_account_definition()
    txn, _entry, oco, stop_leg = _nvda_long_with_oco(acct_def)
    book = {"brk-entry": {"status": OrderStatus.FILLED, "filled_qty": 4.0},
            "brk-oco": {"status": OrderStatus.CANCELED, "filled_qty": 0.0},
            "brk-stop": {"status": OrderStatus.CANCELED, "filled_qty": 0.0}}
    account = _BrokerBook(acct_def.id, book=book)
    _breach_and_stage(monkeypatch, account)
    close = _deferred_close_row(txn, exclude=())

    _set(oco.id, OrderStatus.CANCELED, 0.0)
    _set(stop_leg.id, OrderStatus.CANCELED, 0.0)

    rec = _sweep(account)

    assert account.sent == [(OrderDirection.SELL, pytest.approx(4.0), True)]
    assert get_instance(TradingOrder, close.id).quantity == pytest.approx(4.0)
    assert rec.error_msgs == []
