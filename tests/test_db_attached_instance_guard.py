"""``update_instance``/``add_instance`` must never be handed an object that a live session of
the CALLER's own still owns -- and the DB diagnostics must not lie about why we are stuck.

PROD 2026-08-10, and again 2026-08-24 after a partial fix: TradeManager held a long-lived
session, an autoflush dirtied a ``tradingorder`` row (which takes sqlite's single write lock on
THAT connection), and the same thread then called ``update_instance(order)``. That helper opens
a SECOND connection (``with Session(get_engine()) as new_session``) whose COMMIT blocks on the
write lock its own caller is holding. One thread, two connections, nobody left to release it:
the process performed no writes for 15 minutes and unblocked 15 ms after the outer session
closed. A funded trade was lost both times.

These tests need a FILE-BACKED sqlite database. ``tests/conftest.py``'s session-wide
``sqlite:///:memory:`` engine cannot exhibit cross-connection locking at all, so the entire bug
class is invisible there. ``busy_timeout`` is 200 ms so a regression surfaces in a fifth of a
second instead of the 30 s the live engine waits.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import object_session
from sqlmodel import Session, SQLModel, create_engine, select

import ba2_common.core.db as db
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType


# The incident timestamp. Frozen: nothing here may depend on the wall clock.
_T0 = datetime(2026, 8, 10, 14, 32, 11, tzinfo=timezone.utc)

_BUSY_TIMEOUT_MS = 200


def _order(**kw) -> TradingOrder:
    base = dict(
        account_id=1, symbol="CVS", quantity=1.0,
        side=OrderDirection.BUY, order_type=OrderType.MARKET,
        good_for=None, status=OrderStatus.PENDING, filled_qty=0.0,
        comment=None, transaction_id=None, created_at=_T0,
    )
    base.update(kw)
    return TradingOrder(**base)


@contextmanager
def _file_db(tmp_path):
    """A real on-disk sqlite engine, WAL, 200 ms busy_timeout, installed as ba2_common's engine.

    Restored explicitly (not via monkeypatch) because the autouse ``patch_db_engine`` fixture
    also owns ``db._engine`` and fixture/monkeypatch teardown ordering is not something this
    test should have to reason about.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'lockprobe.db'}",
        connect_args={"check_same_thread": False, "timeout": _BUSY_TIMEOUT_MS / 1000.0},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cur.close()

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(_order(id=1))
        s.commit()

    saved = db._engine
    db._engine = engine
    try:
        yield engine
    finally:
        db._engine = saved
        engine.dispose()


def _capture(monkeypatch, level="error"):
    """Collect ``(message, kwargs)`` logged by ba2_common.core.db at ``level``. NOT caplog.

    ``ba2_common.logger`` installs its own handler with ``propagate = False``, so caplog's root
    handler never sees these records.
    """
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(db.logger, level,
                        lambda msg, *a, **kw: records.append((str(msg), kw)))
    return records


def _quiet(monkeypatch):
    """Silence the retry decorator's per-attempt warnings so the pre-fix output is readable."""
    monkeypatch.setattr(db.logger, "warning", lambda *a, **kw: None)


class _NoSleep:
    """``db.time`` with a no-op ``sleep`` -- makes ``retry_on_lock``'s 7 s of backoff instant."""
    perf_counter = staticmethod(time.perf_counter)
    slept: list[float] = []

    @staticmethod
    def sleep(seconds):
        _NoSleep.slept.append(seconds)


@pytest.fixture
def no_sleep(monkeypatch):
    _NoSleep.slept = []
    monkeypatch.setattr(db, "time", _NoSleep)
    return _NoSleep.slept


# ------------------------------------------------------------------------------------------- #
# F5 -- the self-deadlock itself
# ------------------------------------------------------------------------------------------- #

def test_update_instance_on_attached_dirty_instance_does_not_deadlock(
        tmp_path, monkeypatch, no_sleep):
    """THE 2026-08-10 / 2026-08-24 INCIDENT, 20 lines and no TradeManager.

    Pre-fix: ``OperationalError: database is locked``, raised by the second connection's COMMIT
    against a write lock this very thread holds.
    """
    _quiet(monkeypatch)
    with _file_db(tmp_path) as engine:
        with Session(engine) as outer:
            order = outer.get(TradingOrder, 1)
            order.filled_qty = 7.0
            # Any query autoflushes the dirty row -> this connection now holds the write lock.
            outer.exec(select(TradingOrder).where(TradingOrder.symbol == "CVS")).all()

            assert db.update_instance(order) is True

        with Session(engine) as check:
            assert check.get(TradingOrder, 1).filled_qty == 7.0


def test_add_instance_on_attached_instance_does_not_explode(tmp_path, monkeypatch, no_sleep):
    """The ``add_instance`` twin: a row the caller's session already owns.

    Pre-fix SQLAlchemy refuses outright -- ``InvalidRequestError: Object ... is already attached
    to session`` -- because ``add_instance`` tries to ``add()`` it to a second session.
    """
    _quiet(monkeypatch)
    with _file_db(tmp_path) as engine:
        with Session(engine) as outer:
            pending = _order(symbol="WBA", quantity=3.0)
            outer.add(pending)          # attached (pending) to the caller's session

            new_id = db.add_instance(pending)
            assert new_id is not None

        with Session(engine) as check:
            rows = check.exec(select(TradingOrder).where(TradingOrder.symbol == "WBA")).all()
            assert len(rows) == 1, "the row must be written exactly once"


def test_the_detector_names_the_type_the_primary_key_and_the_owning_session(
        tmp_path, monkeypatch, no_sleep):
    """A wait line nobody can attribute is unactionable. The alarm must identify the row."""
    _quiet(monkeypatch)
    with _file_db(tmp_path) as engine:
        with Session(engine) as outer:
            order = outer.get(TradingOrder, 1)
            order.filled_qty = 2.0
            outer.exec(select(TradingOrder).where(TradingOrder.symbol == "CVS")).all()

            errors = _capture(monkeypatch)
            db.update_instance(order)

    assert len(errors) == 1, f"expected exactly one alarm, got {errors}"
    msg, kwargs = errors[0]
    # Anchored, not a bare "id=1" substring: ``id(session)`` is a big int that very often
    # STARTS with a 1, so a loose check silently passes even with the row's pk removed.
    assert "update_instance(TradingOrder id=1)" in msg, \
        f"the row (type + primary key) is not identified: {msg}"
    assert f"session id={id(outer)} still owns" in msg, \
        f"the owning session is not identified: {msg}"
    assert "(no session argument)" in msg, \
        f"the two shapes of this bug must be distinguishable in the log: {msg}"
    assert kwargs.get("stack_info") is True, "without a stack the call site cannot be found"


def test_the_detector_does_not_fire_on_the_normal_detached_path(tmp_path, monkeypatch, no_sleep):
    """The false-positive guard matters as much as the detector: the overwhelming majority of
    callers hand over a DETACHED object, and they must stay silent and unchanged."""
    with _file_db(tmp_path) as engine:
        with Session(engine) as loader:
            order = loader.get(TradingOrder, 1)
            order.filled_qty = 5.0
            loader.commit()
            loader.refresh(order)   # commit expired every attribute; load them before detaching
            loader.expunge(order)

        errors = _capture(monkeypatch)
        assert object_session(order) is None
        assert db.update_instance(order) is True
        assert db.add_instance(_order(symbol="TRANSIENT")) is not None
        assert errors == [], f"detector fired on a detached instance: {errors}"

        with Session(engine) as check:
            assert check.get(TradingOrder, 1).filled_qty == 5.0


def test_the_detector_does_not_fire_when_the_caller_passes_the_owning_session(
        tmp_path, monkeypatch, no_sleep):
    """``ExtendableSettingsInterface.set_setting`` and the whole settings UI do exactly this:
    load a row from ``session`` and call ``update_instance(row, session)``. That is CORRECT --
    no second connection is opened -- so it must not be flagged, and the row must stay attached
    (the caller keeps using it afterwards)."""
    with _file_db(tmp_path) as engine:
        errors = _capture(monkeypatch)
        with Session(engine) as owner:
            order = owner.get(TradingOrder, 1)
            order.filled_qty = 9.0
            assert db.update_instance(order, owner) is True
            assert object_session(order) is owner, "a legitimate caller's row was detached"

            fresh = _order(symbol="KR")
            owner.add(fresh)
            assert db.add_instance(fresh, owner) is not None
            assert object_session(fresh) is owner

        assert errors == [], f"detector fired on the legitimate same-session path: {errors}"


def test_a_row_owned_by_a_session_other_than_the_one_passed_in_is_flagged_and_rerouted(
        tmp_path, monkeypatch, no_sleep):
    """The rarer half of the same bug: the caller passes session A but the row belongs to a
    still-open session B. A's commit blocks on whatever B is holding, exactly as before -- and
    B is where the object actually lives, so B is the only connection that can write it."""
    _quiet(monkeypatch)
    with _file_db(tmp_path) as engine:
        with Session(engine) as owner, Session(engine) as other:
            order = owner.get(TradingOrder, 1)
            order.filled_qty = 4.0
            owner.exec(select(TradingOrder).where(TradingOrder.symbol == "CVS")).all()

            errors = _capture(monkeypatch)
            assert db.update_instance(order, other) is True
            assert len(errors) == 1, "a cross-session write must still raise the alarm"
            msg = errors[0][0]
            assert "update_instance(TradingOrder id=1)" in msg, msg
            assert f"session id={id(owner)} still owns" in msg, msg
            assert f"caller asked for session id={id(other)}" in msg, msg

        with Session(engine) as check:
            assert check.get(TradingOrder, 1).filled_qty == 4.0


# ------------------------------------------------------------------------------------------- #
# F8 -- the diagnostics that cost investigators days
# ------------------------------------------------------------------------------------------- #

def test_lock_retry_exhaustion_reports_the_real_attempts_wait_and_cap(monkeypatch, no_sleep):
    """``Database locked after 4 attempts with up to 30.0s delays`` is false twice over: the
    backoff is ``min(1.0 * 2**attempt, 30.0)`` over ``attempt in {0,1,2}`` = 1+2+4 ~ 7 s, and the
    30 s cap is mathematically unreachable. Investigators budgeted 2 minutes of waiting that
    never happened."""
    errors = _capture(monkeypatch)
    _quiet(monkeypatch)
    # Freeze the +-10% jitter at +10%: delays become exactly 1.10 / 2.20 / 4.40 = 7.70s. Without
    # this the real total is random and lands on the nominal 7.00 about once in 140 runs -- which
    # is exactly often enough to let "hardcode the nominal total" slip through unnoticed.
    import random
    monkeypatch.setattr(random, "random", lambda: 1.0)

    attempts = []

    @db.retry_on_lock
    def _always_locked():
        attempts.append(1)
        raise RuntimeError("(sqlite3.OperationalError) database is locked")

    with pytest.raises(RuntimeError):
        _always_locked()

    assert len(attempts) == 4
    assert len(errors) == 1
    msg = errors[0][0]

    assert "30.0s delays" not in msg and "up to 30" not in msg, \
        "the message still claims a 30 s per-delay budget that cannot happen"
    assert "4 attempts" in msg, "the attempt count must survive"
    # The real, measured wait -- 1 + 2 + 4, each +10% by the frozen jitter above.
    total = sum(no_sleep)
    assert no_sleep == pytest.approx([1.10, 2.20, 4.40]), \
        f"sanity: the backoff schedule is not what the message must describe: {no_sleep}"
    assert f"{total:.2f}s" in msg, f"the real elapsed wait ({total:.2f}s) is not in: {msg}"
    assert "7.00s" not in msg, "the nominal 1+2+4 is being reported instead of what was slept"
    # The real cap: the largest delay this decorator can ever reach is 4 s, not 30 s.
    assert "4.0s" in msg, f"the real reachable cap (4.0s) is not in: {msg}"


def test_the_lock_wait_line_names_the_in_process_mutex_and_its_holder(monkeypatch):
    """``update_instance() waited for write lock - 29500.51ms`` was read by every investigator
    (and by the incident report) as SQLite contention. It is not: it is ``_db_write_lock``, a
    plain in-process ``threading.Lock``. And without naming the HOLDER the line is unactionable
    -- you know somebody waited, never on whom."""
    monkeypatch.setattr(db, "DB_PERF_LOG_THRESHOLD_MS", 1)
    infos = _capture(monkeypatch, "info")
    _capture(monkeypatch, "warning")

    lock = db._TimedWriteLock()
    holding = threading.Event()
    release = threading.Event()

    def _the_guilty_writer():
        with lock:
            holding.set()
            release.wait(5.0)

    t = threading.Thread(target=_the_guilty_writer, name="ActivityLogWorker")
    t.start()
    holding.wait(5.0)

    def _the_blocked_caller():
        # Release only once we are queued behind the holder, so the wait is real.
        threading.Timer(0.05, release.set).start()
        with lock:
            pass

    _the_blocked_caller()
    t.join(5.0)

    line = " | ".join(m for m, _ in infos)
    assert "in-process" in line and "mutex" in line, \
        f"still reads as a SQLite lock wait: {line}"
    assert "NOT a SQLite lock" in line, \
        f"the disclaimer that misled every investigator of 2026-08 is gone: {line}"
    assert "write lock -" not in line, "the old ambiguous wording survives"
    # THE HOLDER -- without it the line says only that somebody waited, never on whom.
    assert "ActivityLogWorker" in line, f"the holder's thread is not named: {line}"
    assert "_the_guilty_writer" in line, f"the holder's function is not named: {line}"
    # ...beside the WAITER, in the SAME line, or you cannot pair them up.
    assert "MainThread" in line, f"the waiter's thread is not named: {line}"
    assert "_the_blocked_caller" in line, f"the waiter's function is not named: {line}"
