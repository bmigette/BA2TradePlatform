"""The funded-entry loop in ``TradeManager.process_expert_recommendations_after_analysis``.

Two live trades were lost here (CVS 2026-08-10, WSC 2026-08-24) and the same loop leaves a
stranded ``WAITING`` transaction behind whenever a submit fails, which permanently blocks
re-entry for that symbol+expert.

Three properties are pinned:

F3  a submit that never reached the broker is COMPENSATED — order CANCELED, protective legs
    CANCELED, transaction FAILED — so the symbol is released instead of being blocked forever.
F1  the long-lived ``with get_db() as session:`` block must never be DIRTY across the broker
    round trip. A dirty session + any autoflushing query takes sqlite's single write lock, and
    ``submit_order`` then writes from a SECOND connection and blocks on a lock its own thread
    holds. No retry can win that.
F4  the RM-sized quantity (and the RM safeguard stop) must be on disk BEFORE the broker call,
    so a failed submit leaves a re-drivable row rather than an unsized one.

WIRING NOTES

* The DB is FILE-BACKED (``tmp_path``) with WAL and a 150 ms ``busy_timeout``. The shared
  conftest engine is ``sqlite:///:memory:`` with a StaticPool — a single connection, which
  cannot exhibit cross-connection locking at all, so the bug is invisible there. The tiny
  busy_timeout means a real self-deadlock surfaces in milliseconds instead of 30 s.
* ``_WriteTxnProbe`` flags any connection sitting inside an uncommitted write transaction.
  It is THREAD-FILTERED: ba2_common runs a daemon ``ActivityLogWorker`` that writes from
  another thread, and that thread's perfectly legitimate writes must not be mistaken for the
  caller's self-inflicted lock.
* The fake account inherits the REAL ``AccountInterface.submit_order`` so the transaction
  creation / order update / wash-trade gate all run for real; only ``_submit_order_impl``
  (the broker call) and the protective-leg staging are doubled.
"""
from __future__ import annotations

import threading
import time as _real_time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect
from sqlmodel import SQLModel, Session, create_engine, select

import ba2_common.core.db as _pkg_db
from ba2_common.core.db import add_instance, get_db
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import (AnalysisUseCase, ExpertActionType, OrderDirection,
                                   OrderOpenType, OrderRecommendation, OrderStatus, OrderType,
                                   TransactionStatus)

from tests import factories
from tests.conftest import MockAccount

# Frozen deliberately in the past: nothing here may depend on the wall clock.
FROZEN_NOW = datetime(2026, 3, 17, 15, 37, 54, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------- #
# Frozen clock
# --------------------------------------------------------------------------------------- #
class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is not None else FROZEN_NOW.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return FROZEN_NOW.replace(tzinfo=None)


# --------------------------------------------------------------------------------------- #
# Write-transaction probe
# --------------------------------------------------------------------------------------- #
class _WriteTxnProbe:
    """Reports connections of the CURRENT thread that are inside an open write transaction.

    Two independent signals, unioned:

    1. a ``before_cursor_execute`` listener flags a connection on INSERT/UPDATE/DELETE and the
       flag is cleared on commit/rollback (the mechanism asked for);
    2. pysqlite's own ``Connection.in_transaction``, which is True exactly while a BEGIN is
       open — and pysqlite only BEGINs before a write, so it IS "a write lock is held".

    ``activitylog`` writes are ignored: they come from the async worker thread and are not the
    caller's business (the thread filter already covers them; this is belt and braces).
    """

    _DML = ("INSERT", "UPDATE", "DELETE", "REPLACE")

    def __init__(self, engine):
        self._engine = engine
        self._flagged = {}          # id(dbapi_conn) -> (thread_ident, statement)
        self._conns = {}            # id(dbapi_conn) -> dbapi_conn
        sa_event.listen(engine, "connect", self._on_connect)
        sa_event.listen(engine, "before_cursor_execute", self._on_cursor)
        sa_event.listen(engine, "commit", self._on_end)
        sa_event.listen(engine, "rollback", self._on_end)

    # -- listeners --------------------------------------------------------------------- #
    def _on_connect(self, dbapi_conn, _record):
        self._conns[id(dbapi_conn)] = dbapi_conn

    @staticmethod
    def _raw(conn):
        return conn.connection.dbapi_connection

    def _on_cursor(self, conn, _cursor, statement, _params, _context, _executemany):
        head = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if head not in self._DML:
            return
        if "activitylog" in statement.lower():
            return
        self._flagged.setdefault(id(self._raw(conn)),
                                 (threading.get_ident(), " ".join(statement.split())[:140]))

    def _on_end(self, conn):
        self._flagged.pop(id(self._raw(conn)), None)

    # -- query ------------------------------------------------------------------------- #
    def open_write_transactions(self):
        """Statements of this thread's connections that are still inside a write transaction."""
        me = threading.get_ident()
        out = []
        for key, (owner, statement) in list(self._flagged.items()):
            if owner == me:
                out.append(statement)
        for key, raw in list(self._conns.items()):
            owner = self._flagged.get(key, (None, None))[0]
            try:
                open_txn = bool(raw.in_transaction)
            except Exception:  # pragma: no cover - connection already closed
                continue
            if open_txn and owner in (me, None) and key not in self._flagged:
                out.append(f"<uncommitted BEGIN on connection {key}>")
        return out


# --------------------------------------------------------------------------------------- #
# File-backed engine (the in-memory conftest engine CANNOT show cross-connection locking)
# --------------------------------------------------------------------------------------- #
class _NoSleep:
    """Stand-in for ``ba2_common.core.db.time`` that never actually sleeps.

    ``retry_on_lock`` sleeps at least 0.5 s per attempt, four times. That turns a deliberate
    lock into a multi-second test; the sleeps carry no meaning here.
    """
    perf_counter = staticmethod(_real_time.perf_counter)
    monotonic = staticmethod(_real_time.monotonic)
    time = staticmethod(_real_time.time)

    @staticmethod
    def sleep(_seconds):
        return None


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """Point ba2_common at a real sqlite FILE with WAL + a 150 ms busy_timeout."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'trade.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.15},
        pool_size=10, max_overflow=20, echo=False,
    )

    @sa_event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):          # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=OFF")
        cur.execute("PRAGMA busy_timeout=150")
        cur.close()

    SQLModel.metadata.create_all(engine)
    probe = _WriteTxnProbe(engine)

    monkeypatch.setattr(_pkg_db, "time", _NoSleep)
    saved = _pkg_db._engine
    _pkg_db._engine = engine
    try:
        yield probe
    finally:
        _pkg_db._engine = saved
        engine.dispose()


# --------------------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------------------- #
class _EnterExpert(MarketExpertInterface):
    """Minimal classic-RM expert (inherits the base RM trading-setting definitions)."""

    def __init__(self, id_val):
        self.id = id_val
        self._settings_cache = None

    @classmethod
    def description(cls):
        return "funded-entry-loop test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


class _FundedEntryAccount(MockAccount):
    """Real ``AccountInterface.submit_order``; doubled broker call + doubled leg staging.

    ``fail_symbols`` names symbols whose broker call raises; ``fail_exc`` is what it raises.
    Every submit is observed BEFORE the interface does anything, which is what lets the tests
    assert on session hygiene and on what is already on disk.
    """

    def __init__(self, id_val, probe=None, fail_symbols=(), fail_exc=None,
                 broker_id_before_failure=False, washtrade_lock_symbols=()):
        super().__init__(id_val)
        self._probe = probe
        self._fail_symbols = set(fail_symbols)
        self._fail_exc = fail_exc
        self._broker_id_before_failure = broker_id_before_failure
        self._washtrade_lock_symbols = set(washtrade_lock_symbols)
        self._balance = 100_000.0
        self._prices = {"AAPL": 100.0, "MSFT": 100.0, "GOOGL": 100.0}
        self.submits = []               # list of dicts, one per submit_order call
        self.attached_at_submit = []    # symbols whose order was still session-attached
        self.write_locks_at_submit = [] # (symbol, [statements]) seen holding a write lock
        self.disk_qty_at_submit = {}    # symbol -> quantity ON DISK when submit was entered
        self.disk_stop_at_submit = {}   # symbol -> stop_price ON DISK when submit was entered

    # -- observation ------------------------------------------------------------------- #
    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        symbol = trading_order.symbol
        if sa_inspect(trading_order).session is not None:
            self.attached_at_submit.append(symbol)
        if self._probe is not None:
            held = self._probe.open_write_transactions()
            if held:
                self.write_locks_at_submit.append((symbol, held))
        row = self._read_row(trading_order.id)
        self.disk_qty_at_submit[symbol] = None if row is None else row[0]
        self.disk_stop_at_submit[symbol] = None if row is None else row[1]
        self.submits.append({"symbol": symbol, "quantity": trading_order.quantity,
                             "sl_price": sl_price, "order_id": trading_order.id})
        if symbol in self._washtrade_lock_symbols:
            # Exactly AccountInterface's wash-trade lock branch: an UNSENT status, no broker id,
            # and the order returned as a TRUTHY result — so the funded loop counts it as
            # submitted and it waits to be retried by _check_all_washtrade_locked_orders.
            from ba2_common.core.db import update_instance
            trading_order.account_id = self.id
            trading_order.status = OrderStatus.WASHTRADE_LOCKED
            update_instance(trading_order)
            return trading_order
        # Explicitly the INTERFACE implementation: MockAccount overrides submit_order with a
        # narrower canned double, and this test needs the real transaction creation /
        # order update / wash-trade gate to run.
        from ba2_common.core.interfaces.AccountInterface import AccountInterface
        return AccountInterface.submit_order(
            self, trading_order, tp_price=tp_price, sl_price=sl_price,
            is_closing_order=is_closing_order)

    @staticmethod
    def _read_row(order_id):
        """Read (quantity, stop_price) straight from the DB on an INDEPENDENT connection."""
        if not order_id:
            return None
        with Session(_pkg_db.get_engine()) as s:
            got = s.exec(select(TradingOrder.quantity, TradingOrder.stop_price)
                         .where(TradingOrder.id == order_id)).first()
        return (got[0], got[1]) if got is not None else None

    # -- broker double ------------------------------------------------------------------ #
    def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                           is_closing_order=False, use_complex_order=False):
        if trading_order.symbol in self._fail_symbols:
            if self._broker_id_before_failure:
                # The DANGEROUS inverse: the order DID reach the broker, and only the
                # bookkeeping afterwards blew up.
                trading_order.broker_order_id = f"broker-{trading_order.id}"
                trading_order.status = OrderStatus.NEW
                from ba2_common.core.db import update_instance
                update_instance(trading_order)
            raise self._fail_exc or RuntimeError("broker refused")
        trading_order.status = OrderStatus.NEW
        trading_order.broker_order_id = f"broker-{trading_order.id}"
        # Real broker impls persist the accepted order themselves (AccountInterface does not).
        from ba2_common.core.db import update_instance
        update_instance(trading_order)
        return trading_order

    # -- protective-leg staging (what AlpacaAccount.adjust_* does for real) ------------- #
    def adjust_sl(self, transaction, new_sl_price, source=""):
        return self._stage_leg(transaction, stop_price=new_sl_price)

    def adjust_tp(self, transaction, new_tp_price, source=""):
        return self._stage_leg(transaction, limit_price=new_tp_price)

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        return self._stage_leg(transaction, limit_price=new_tp_price, stop_price=new_sl_price)

    def _stage_leg(self, transaction, limit_price=None, stop_price=None):
        with get_db() as session:
            entry = session.exec(
                select(TradingOrder)
                .where(TradingOrder.transaction_id == transaction.id,
                       TradingOrder.order_type == OrderType.MARKET)
                .order_by(TradingOrder.id)
            ).first()
            if entry is None:
                return False
            entry_id, entry_side, entry_qty, entry_symbol = (
                entry.id, entry.side, entry.quantity, entry.symbol)
        leg_side = (OrderDirection.SELL if entry_side == OrderDirection.BUY
                    else OrderDirection.BUY)
        add_instance(TradingOrder(
            account_id=self.id, symbol=entry_symbol, quantity=entry_qty, side=leg_side,
            order_type=OrderType.SELL_STOP if leg_side == OrderDirection.SELL else OrderType.BUY_STOP,
            limit_price=limit_price, stop_price=stop_price,
            transaction_id=transaction.id, status=OrderStatus.WAITING_TRIGGER,
            depends_on_order=entry_id, depends_order_status_trigger=OrderStatus.FILLED,
            open_type=OrderOpenType.AUTOMATIC, created_at=FROZEN_NOW,
        ))
        return True

    # -- inert broker surface ----------------------------------------------------------- #
    def refresh_orders(self, fetch_all=False):
        return None

    def get_orders(self, status=None):
        return []

    def get_instrument_current_price(self, symbol_or_list, price_type="bid"):
        if isinstance(symbol_or_list, (list, tuple, set)):
            return {s: self._prices.get(s, 100.0) for s in symbol_or_list}
        return self._prices.get(symbol_or_list, 100.0)


# --------------------------------------------------------------------------------------- #
# Scenario helpers
# --------------------------------------------------------------------------------------- #
def _seed_enter_ruleset(with_stop_loss=True):
    """bullish & confidence>=60 -> buy (+ an SL adjustment so ``execute()`` stages a leg)."""
    rs = factories.create_ruleset(name="enter-funded", subtype=AnalysisUseCase.ENTER_MARKET)
    actions = {"action_0": {"action_type": ExpertActionType.BUY.value}}
    if with_stop_loss:
        actions["action_1"] = {"action_type": ExpertActionType.ADJUST_STOP_LOSS.value,
                               "value": -5.0, "reference_value": "current_price"}
    ea = factories.create_event_action(
        name="buy-tier", subtype=AnalysisUseCase.ENTER_MARKET,
        triggers={"trigger_0": {"event_type": "bullish"},
                  "trigger_1": {"event_type": "confidence", "operator": ">=", "value": 60.0}},
        actions=actions,
    )
    factories.link_rule_to_ruleset(rs.id, ea.id, 0)
    return rs.id


def _resolver_for(expert, account):
    class _R:
        def get_expert_instance(self, expert_id):
            return expert

        def get_account_instance(self, account_id):
            return account

        def get_account_instance_from_transaction(self, transaction):
            return account
    return _R()


def _make_scenario(symbols, with_stop_loss=True, per_instrument_pct=40.0):
    """Seed account + expert + ruleset + one BUY recommendation per symbol."""
    acct_def = factories.create_account_definition(provider="MockAccount")
    ruleset_id = _seed_enter_ruleset(with_stop_loss=with_stop_loss)
    inst = factories.create_expert_instance(
        account_id=acct_def.id, expert="_EnterExpert", virtual_equity_pct=100.0,
        enter_market_ruleset_id=ruleset_id)
    expert = _EnterExpert(inst.id)
    expert.save_settings({
        "allow_automated_trade_opening": (True, "bool"),
        "enable_buy": (True, "bool"),
        "enable_sell": (False, "bool"),
        "max_virtual_equity_per_instrument_percent": (per_instrument_pct, "float"),
    })
    profit = 30.0
    for sym in symbols:
        factories.create_recommendation(
            instance_id=inst.id, symbol=sym, recommended_action=OrderRecommendation.BUY,
            expected_profit_percent=profit, price_at_date=100.0, confidence=90.0,
            created_at=FROZEN_NOW)
        profit -= 5.0
    return acct_def, inst, expert


def _run_enter(expert, account, expert_instance_id):
    """Drive the real funded-entry loop with a frozen clock."""
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver
    from ba2_trade_platform.core import TradeManager as _tm_mod
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    try:
        prev_resolver = get_instance_resolver()
    except Exception:  # noqa: BLE001
        prev_resolver = None
    set_instance_resolver(_resolver_for(expert, account))
    try:
        with patch.object(_tm_mod, "datetime", _FrozenDatetime), \
             patch.object(_tm_mod.TradeManager, "_ENTRY_SUBMIT_BACKOFF_S", 0.0), \
             patch("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                   return_value=expert), \
             patch("ba2_trade_platform.modules.accounts.get_account_class",
                   return_value=(lambda _id: account)), \
             patch.object(type(get_trade_manager()), "_has_pending_analysis_jobs",
                          return_value=False):
            return get_trade_manager().process_expert_recommendations_after_analysis(
                expert_instance_id, lookback_days=7)
    finally:
        if prev_resolver is not None:
            set_instance_resolver(prev_resolver)


def _orders(symbol=None):
    with get_db() as session:
        stmt = select(TradingOrder).order_by(TradingOrder.id)
        if symbol:
            stmt = stmt.where(TradingOrder.symbol == symbol)
        return list(session.exec(stmt).all())


def _transactions(symbol=None):
    with get_db() as session:
        stmt = select(Transaction).order_by(Transaction.id)
        if symbol:
            stmt = stmt.where(Transaction.symbol == symbol)
        return list(session.exec(stmt).all())


# ========================================================================================= #
# F3 — a submit that never reached the broker must release the symbol
# ========================================================================================= #
def _locked_error():
    from sqlalchemy.exc import OperationalError
    return OperationalError("UPDATE tradingorder SET quantity=?", {},
                            Exception("database is locked"))


def test_failed_submit_releases_the_symbol(file_db):
    """THE LEAK: the else-branch used to only log. The entry stayed PENDING, its protective leg
    stayed WAITING_TRIGGER and its transaction stayed WAITING — and a WAITING transaction is
    exactly what the SAFETY CHECK refuses to trade behind, forever, with nothing to sweep it."""
    _acct, inst, expert = _make_scenario(["AAPL"])
    account = _FundedEntryAccount(_acct.id, probe=file_db, fail_symbols={"AAPL"},
                                  fail_exc=_locked_error())

    _run_enter(expert, account, inst.id)

    entries = [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET]
    legs = [o for o in _orders("AAPL") if o.depends_on_order is not None]
    txns = _transactions("AAPL")
    assert len(entries) == 1 and len(txns) == 1, (entries, txns)
    assert legs, "the scenario must stage a protective leg for this test to mean anything"

    assert entries[0].status == OrderStatus.CANCELED, (
        f"a funded entry that never reached the broker must be cancelled, not left "
        f"{entries[0].status}")
    assert all(leg.status == OrderStatus.CANCELED for leg in legs), (
        f"protective legs of a cancelled entry must be cancelled too: "
        f"{[(l.id, l.status) for l in legs]}")
    assert txns[0].status == TransactionStatus.FAILED, (
        f"the transaction must be FAILED, not left {txns[0].status} to block the symbol")


def test_a_failed_submit_does_not_block_the_next_run_for_the_same_symbol(file_db):
    """The consequence that actually cost money: with the transaction left WAITING, the very
    next enter_market pass skips the symbol at the SAFETY CHECK and never tries again."""
    _acct, inst, expert = _make_scenario(["AAPL"])
    account = _FundedEntryAccount(_acct.id, probe=file_db, fail_symbols={"AAPL"},
                                  fail_exc=_locked_error())
    _run_enter(expert, account, inst.id)
    first_pass_submits = len(account.submits)

    # Second pass, same symbol + expert, broker now healthy.
    account._fail_symbols = set()
    _run_enter(expert, account, inst.id)

    assert len(account.submits) > first_pass_submits, (
        "the SAFETY CHECK is still blocking the symbol behind the stranded transaction")
    assert any(o.status == OrderStatus.NEW for o in _orders("AAPL")), \
        "the retry should have produced a live order"


def test_a_submit_that_reached_the_broker_is_never_compensated(file_db):
    """THE DANGEROUS INVERSE. The broker accepted the order and only the bookkeeping after it
    blew up. Writing CANCELED here would hide a LIVE order — a position the platform can no
    longer see. The compensation must refuse."""
    _acct, inst, expert = _make_scenario(["AAPL"])
    account = _FundedEntryAccount(_acct.id, probe=file_db, fail_symbols={"AAPL"},
                                  fail_exc=RuntimeError("bookkeeping blew up after acceptance"),
                                  broker_id_before_failure=True)

    _run_enter(expert, account, inst.id)

    entries = [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET]
    txns = _transactions("AAPL")
    assert len(entries) == 1
    assert entries[0].broker_order_id, "scenario precondition: the order reached the broker"
    assert entries[0].status != OrderStatus.CANCELED, (
        "an order carrying a broker id must NEVER be cancelled in the database")
    assert txns[0].status != TransactionStatus.FAILED, (
        "the transaction of a live broker order must not be failed")


def test_compensation_refuses_an_order_the_broker_already_holds(file_db):
    """Unit-level guard: NEW is not an unsent status, so ``_fail_unsent_entry`` is a no-op even
    when asked directly."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.WAITING)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.NEW,
        transaction_id=txn.id, broker_order_id="alpaca-abc")
    leg = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, stop_price=90.0, status=OrderStatus.WAITING_TRIGGER,
        transaction_id=txn.id, depends_on_order=order.id,
        depends_order_status_trigger=OrderStatus.FILLED)

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False

    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.NEW
        assert s.get(TradingOrder, leg.id).status == OrderStatus.WAITING_TRIGGER
        assert s.get(Transaction, txn.id).status == TransactionStatus.WAITING


def test_compensation_refuses_on_a_broker_id_alone(file_db):
    """A PENDING status with a broker id is still broker contact — the id wins."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING,
        broker_order_id="alpaca-abc")

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False
    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.PENDING


def test_compensation_leaves_an_opened_transaction_alone(file_db):
    """The ``status == WAITING`` guard. An OPENED transaction has other filled orders behind it
    and a real position at the broker; the unsent order was an add-on, not the position."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.OPENED)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING,
        transaction_id=txn.id)

    assert get_trade_manager()._fail_unsent_entry(order, "test") is True
    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.CANCELED, \
            "the add-on order itself is still cancelled"
        assert s.get(Transaction, txn.id).status == TransactionStatus.OPENED, \
            "an OPENED transaction with a real position must never be marked FAILED"


def test_compensation_fails_a_transaction_the_order_row_lost(file_db):
    """The orphan case. ``submit_order`` creates the Transaction and stamps
    ``trading_order.transaction_id``, then persists it with a SEPARATE ``update_instance``. When
    THAT write is the one that lost to the lock, the row on disk has no transaction_id while a
    WAITING transaction very much exists — and that orphan is what keeps blocking the symbol."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.WAITING)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING,
        transaction_id=None)
    order.transaction_id = txn.id      # known in memory only, never persisted

    assert get_trade_manager()._fail_unsent_entry(order, "test") is True
    with get_db() as s:
        assert s.get(Transaction, txn.id).status == TransactionStatus.FAILED


def test_compensation_of_a_deleted_row_is_a_no_op(file_db):
    """``get_instance`` raises InstanceNotFound rather than returning None; a vanished row must
    be an answer, not a crash."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING)
    with get_db() as s:
        s.delete(s.get(TradingOrder, order.id))
        s.commit()

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False


def test_a_raised_submit_also_releases_the_symbol(file_db):
    """The OTHER exit from the funded loop. ``_submit_funded_entry_with_retry`` re-raises
    anything that is not a DB lock, so a plain broker rejection lands in the except handler —
    which used to log and move on, leaking exactly the same stranded WAITING transaction."""
    _acct, inst, expert = _make_scenario(["AAPL"])
    account = _FundedEntryAccount(
        _acct.id, probe=file_db, fail_symbols={"AAPL"},
        fail_exc=ValueError('{"code":40310000,"message":"potential wash trade detected"}'))

    _run_enter(expert, account, inst.id)

    entries = [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET]
    txns = _transactions("AAPL")
    assert len(account.submits) == 1, "a broker rejection must not be retried"
    assert entries[0].status == OrderStatus.CANCELED, entries[0].status
    assert txns[0].status == TransactionStatus.FAILED, txns[0].status


def test_compensation_refuses_an_errored_order_even_without_a_broker_id(file_db):
    """The status guard on its own. ERROR is what the submit path stamps when the broker call
    itself failed; the platform cannot prove the order never landed, so it is left alone even
    though no broker id was ever recorded."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.WAITING)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.ERROR,
        transaction_id=txn.id)
    assert order.broker_order_id is None, "precondition: no broker id at all"

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False
    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.ERROR
        assert s.get(Transaction, txn.id).status == TransactionStatus.WAITING


def test_compensation_refuses_when_only_the_IN_MEMORY_object_shows_broker_contact(file_db):
    """The row on disk is stale because the write that would have recorded the acceptance is
    the very thing that failed. Only the in-memory object knows — and it must be believed."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING)
    order.status = OrderStatus.NEW                     # in memory only
    order.broker_order_id = "alpaca-only-in-memory"    # in memory only

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False
    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.PENDING, \
            "nothing may be written for a refused compensation"


def test_compensation_refuses_when_only_the_ON_DISK_row_shows_broker_contact(file_db):
    """The mirror image: the caller is holding a stale copy from before the submit, and the
    committed row is the one that knows the broker took it."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager
    from ba2_common.core.db import update_instance

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING)
    fresh = get_trade_manager()._row_or_none(TradingOrder, order.id)
    fresh.status = OrderStatus.NEW
    fresh.broker_order_id = "alpaca-committed"
    update_instance(fresh)
    # `order` is the caller's STALE copy: still PENDING, still no broker id.
    assert order.status == OrderStatus.PENDING and order.broker_order_id is None

    assert get_trade_manager()._fail_unsent_entry(order, "test") is False
    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.NEW


def test_a_failure_on_one_candidate_never_compensates_a_previous_candidates_order(file_db):
    """``order`` is a loop-local name. If it is not reset per iteration, a candidate whose
    ``execute()`` blows up compensates whatever the PREVIOUS candidate left bound — here a
    perfectly healthy WASHTRADE_LOCKED entry that is waiting to be retried, on a different
    symbol, which would be cancelled for a failure that has nothing to do with it."""
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    _acct, inst, expert = _make_scenario(["AAPL", "MSFT"])
    account = _FundedEntryAccount(_acct.id, probe=file_db, washtrade_lock_symbols={"AAPL"})

    real_execute = TradeActionEvaluator.execute

    def _execute(self, *a, **kw):
        if self.instrument_name == "MSFT":
            raise RuntimeError("execute() blew up for MSFT")
        return real_execute(self, *a, **kw)

    with patch.object(TradeActionEvaluator, "execute", _execute):
        _run_enter(expert, account, inst.id)

    aapl = [o for o in _orders("AAPL") if o.order_type == OrderType.MARKET]
    assert len(aapl) == 1
    assert aapl[0].status == OrderStatus.WASHTRADE_LOCKED, (
        f"AAPL's locked entry was collateral damage from MSFT's failure: {aapl[0].status}")


def test_only_WAITING_TRIGGER_dependents_are_cancelled(file_db):
    """A dependent that is FILLED (or working at the broker) is not waiting on anything — it is
    a real order, possibly a real position. Only the legs still parked behind the dead parent
    may be cancelled."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING)
    waiting = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, stop_price=90.0, status=OrderStatus.WAITING_TRIGGER,
        depends_on_order=order.id, depends_order_status_trigger=OrderStatus.FILLED)
    filled = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_LIMIT, limit_price=120.0, status=OrderStatus.FILLED,
        depends_on_order=order.id, depends_order_status_trigger=OrderStatus.FILLED)

    assert get_trade_manager()._fail_unsent_entry(order, "test") is True
    with get_db() as s:
        assert s.get(TradingOrder, waiting.id).status == OrderStatus.CANCELED
        assert s.get(TradingOrder, filled.id).status == OrderStatus.FILLED, \
            "a FILLED dependent is a real order and must never be cancelled"


def test_the_transaction_is_found_when_only_the_ROW_carries_its_id(file_db):
    """The mirror of the orphan case: the caller's copy predates the link, the committed row
    has it. Reading only the in-memory value would leave the transaction stranded."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.WAITING)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.PENDING,
        transaction_id=txn.id)
    order.transaction_id = None      # the caller's stale copy predates the link

    assert get_trade_manager()._fail_unsent_entry(order, "test") is True
    with get_db() as s:
        assert s.get(Transaction, txn.id).status == TransactionStatus.FAILED


def test_a_washtrade_expiry_still_cancels_order_legs_and_transaction(file_db):
    """The other caller of the extracted tail — behaviour must be unchanged."""
    from ba2_trade_platform.core.TradeManager import get_trade_manager

    acct = factories.create_account_definition(provider="MockAccount")
    txn = factories.create_transaction(symbol="WSC", status=TransactionStatus.WAITING)
    order = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, status=OrderStatus.WASHTRADE_LOCKED,
        transaction_id=txn.id, created_at=FROZEN_NOW - timedelta(hours=48))
    leg = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, stop_price=90.0, status=OrderStatus.WAITING_TRIGGER,
        transaction_id=txn.id, depends_on_order=order.id,
        depends_order_status_trigger=OrderStatus.FILLED)
    blocker = factories.create_trading_order(
        account_id=acct.id, symbol="WSC", quantity=7.0, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.NEW)

    get_trade_manager()._expire_washtrade_locked_order(order, blocker, 48.0)

    with get_db() as s:
        assert s.get(TradingOrder, order.id).status == OrderStatus.CANCELED
        assert s.get(TradingOrder, leg.id).status == OrderStatus.CANCELED
        assert s.get(Transaction, txn.id).status == TransactionStatus.FAILED
