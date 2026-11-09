"""One WAITING_TRIGGER dependent order reaches the broker from at most ONE thread.

``TradeManager._check_all_waiting_trigger_orders`` is called by the scheduled account refresh
(under ``_REFRESH_LOCK``) AND by every expert's entry pass (under that expert's processing lock
only). Both load the WAITING_TRIGGER rows in Phase 1 and submit them in Phase 2 from those
snapshots, so before the per-order claim two threads could both send the same protective leg:
a duplicate stop/target at the broker, or a rejection (Alpaca 40310000) that the second thread
wrote back as ERROR over a leg the first thread had just placed.

Deterministic: the threads are sequenced with Events, never with sleeps. The DB is a sqlite
FILE (WAL) because the conftest ``:memory:`` engine gives every thread its own empty database.
"""
import threading

import pytest
from sqlalchemy import event as sa_event
from sqlmodel import SQLModel, create_engine

import ba2_common.core.db as _pkg_db
from ba2_trade_platform.core import TradeManager as tm_mod
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.db import get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from tests.conftest import MockAccount
from tests.factories import create_account_definition, create_trading_order, create_transaction

WAIT_S = 10.0   # a safety bound for a broken implementation, never a timing assumption


@pytest.fixture
def file_db(tmp_path, patch_db_engine):
    engine = create_engine(f"sqlite:///{tmp_path / 'trade.db'}",
                           connect_args={"check_same_thread": False, "timeout": 5})

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    SQLModel.metadata.create_all(engine)
    saved = _pkg_db._engine
    _pkg_db._engine = engine
    try:
        yield engine
    finally:
        _pkg_db._engine = saved
        engine.dispose()


def _filled_entry_with_waiting_stop():
    definition = create_account_definition()
    txn = create_transaction(symbol="AAPL", side=OrderDirection.BUY, quantity=10.0,
                             status=TransactionStatus.OPENED)
    entry = create_trading_order(account_id=definition.id, symbol="AAPL", quantity=10.0,
                                 side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                 status=OrderStatus.FILLED, filled_qty=10.0, open_price=150.0,
                                 transaction_id=txn.id, broker_order_id="brk-entry")
    stop = create_trading_order(account_id=definition.id, symbol="AAPL", quantity=10.0,
                                side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                                status=OrderStatus.WAITING_TRIGGER, stop_price=140.0,
                                transaction_id=txn.id, depends_on_order=entry.id,
                                depends_order_status_trigger=OrderStatus.FILLED)
    return stop


class _Broker:
    """Records every submit_order call. The FIRST call can be held on an Event so a second
    thread runs while it is in flight; ``outcome`` decides what the broker answers."""

    def __init__(self, block_first=False, outcome="accept"):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = block_first
        self.outcome = outcome
        self._lock = threading.Lock()

    def account_class(self):
        broker = self

        class Account(MockAccount):
            def get_available_position_quantity(self, symbol):
                return 1_000.0

            def submit_order(self, order, is_closing_order=False):
                with broker._lock:
                    broker.calls.append(order.id)
                    first = len(broker.calls) == 1
                if first and broker.block_first:
                    broker.entered.set()
                    assert broker.release.wait(WAIT_S), "test never released the first submit"
                if broker.outcome == "raise":
                    raise ValueError("Order validation failed: boom")
                if broker.outcome == "none":
                    return None
                # What a real broker impl persists once the broker accepted the order.
                row = get_instance(TradingOrder, order.id)
                row.broker_order_id = f"brk-{order.id}-{len(broker.calls)}"
                row.status = OrderStatus.ACCEPTED
                update_instance(row)
                return row

        return Account


def _use(monkeypatch, broker):
    import ba2_trade_platform.modules.accounts as accounts
    account_class = broker.account_class()
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: account_class)


def _run_in_thread(target):
    errors = []

    def _body():
        try:
            target()
        except BaseException as e:  # noqa: BLE001 -- surfaced by the assertion below
            errors.append(e)

    t = threading.Thread(target=_body, name="waiting-trigger-test")
    t.start()
    return t, errors


def _overlap_two_passes(broker):
    """Pass A sits inside submit_order while pass B runs start to finish."""
    a, a_errors = _run_in_thread(TradeManager()._check_all_waiting_trigger_orders)
    assert broker.entered.wait(WAIT_S), "pass A never reached submit_order"
    b, b_errors = _run_in_thread(TradeManager()._check_all_waiting_trigger_orders)
    b.join(WAIT_S)
    assert not b.is_alive(), "pass B blocked behind pass A's broker call"
    broker.release.set()
    a.join(WAIT_S)
    assert not a.is_alive()
    assert not a_errors and not b_errors, (a_errors, b_errors)


@pytest.mark.usefixtures("file_db")
def test_two_concurrent_passes_submit_the_leg_once(monkeypatch):
    stop = _filled_entry_with_waiting_stop()
    broker = _Broker(block_first=True)
    _use(monkeypatch, broker)

    _overlap_two_passes(broker)

    assert broker.calls == [stop.id], "the protective leg was sent to the broker twice"
    row = get_instance(TradingOrder, stop.id)
    assert row.status == OrderStatus.ACCEPTED
    assert row.broker_order_id == f"brk-{stop.id}-1"


@pytest.mark.usefixtures("file_db")
def test_a_stale_snapshot_taken_before_the_submit_does_not_resend(monkeypatch):
    """Pass B loads the row while it is still WAITING_TRIGGER, then reaches Phase 2 only AFTER
    pass A has submitted it and released its claim: B must re-read and skip, not resend."""
    stop = _filled_entry_with_waiting_stop()
    broker = _Broker()
    _use(monkeypatch, broker)

    b_loaded = threading.Event()
    a_done = threading.Event()
    real_classify = tm_mod.classify_waiting_trigger
    b_thread = {}

    def classify(*args, **kwargs):
        result = real_classify(*args, **kwargs)
        if threading.get_ident() == b_thread.get("ident"):
            b_loaded.set()
            assert a_done.wait(WAIT_S), "pass A never finished"
        return result

    monkeypatch.setattr(tm_mod, "classify_waiting_trigger", classify)

    def pass_b():
        b_thread["ident"] = threading.get_ident()
        TradeManager()._check_all_waiting_trigger_orders()

    b, b_errors = _run_in_thread(pass_b)
    assert b_loaded.wait(WAIT_S), "pass B never loaded the row"
    TradeManager()._check_all_waiting_trigger_orders()          # pass A, start to finish
    a_done.set()
    b.join(WAIT_S)
    assert not b.is_alive() and not b_errors, b_errors

    assert broker.calls == [stop.id], "a stale Phase-1 snapshot resent an accepted leg"
    row = get_instance(TradingOrder, stop.id)
    assert row.status == OrderStatus.ACCEPTED
    assert row.broker_order_id == f"brk-{stop.id}-1"


@pytest.mark.usefixtures("file_db")
@pytest.mark.parametrize("outcome", ["none", "raise"])
def test_a_failed_submit_still_ends_error_and_is_sent_once(monkeypatch, outcome):
    stop = _filled_entry_with_waiting_stop()
    broker = _Broker(block_first=True, outcome=outcome)
    _use(monkeypatch, broker)

    _overlap_two_passes(broker)

    assert broker.calls == [stop.id]
    row = get_instance(TradingOrder, stop.id)
    assert row.status == OrderStatus.ERROR
    assert row.broker_order_id is None
    if outcome == "raise":
        assert "[validation_error]" in (row.comment or "")


@pytest.mark.usefixtures("file_db")
def test_single_pass_submits_and_releases_its_claim(monkeypatch):
    stop = _filled_entry_with_waiting_stop()
    broker = _Broker()
    _use(monkeypatch, broker)

    TradeManager()._check_all_waiting_trigger_orders()

    assert broker.calls == [stop.id]
    assert get_instance(TradingOrder, stop.id).status == OrderStatus.ACCEPTED
    assert not tm_mod._DEPENDENT_SUBMIT_CLAIMS, "a finished pass left its claim behind"


@pytest.mark.usefixtures("file_db")
def test_a_deferred_leg_stays_waiting_and_is_retried_by_the_next_pass(monkeypatch):
    """The claim must never strand a row: a leg the qty gate defers stays WAITING_TRIGGER and
    the next pass submits it."""
    stop = _filled_entry_with_waiting_stop()
    row = get_instance(TradingOrder, stop.id)
    row.depends_order_status_trigger = OrderStatus.CANCELED      # a cancel-and-replace leg
    update_instance(row)
    parent = get_instance(TradingOrder, row.depends_on_order)
    parent.status = OrderStatus.CANCELED
    update_instance(parent)

    broker = _Broker()
    free = {"qty": 0.0}
    base = broker.account_class()

    class Account(base):
        def get_available_position_quantity(self, symbol):
            return free["qty"]

    import ba2_trade_platform.modules.accounts as accounts
    monkeypatch.setattr(accounts, "get_account_class", lambda provider: Account)

    TradeManager()._check_all_waiting_trigger_orders()
    assert broker.calls == []
    assert get_instance(TradingOrder, stop.id).status == OrderStatus.WAITING_TRIGGER

    free["qty"] = 10.0
    TradeManager()._check_all_waiting_trigger_orders()
    assert broker.calls == [stop.id]
    assert get_instance(TradingOrder, stop.id).status == OrderStatus.ACCEPTED
