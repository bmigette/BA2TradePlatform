"""A transient sqlite write lock must not cost a funded trade.

Measured on PROD 2026-08-10: CVS was screened, passed its ruleset and was FUNDED by the RM
(1 share @ $95.69). submit_order's Transaction insert then hit "database is locked";
db.retry_on_lock burned 4 attempts against a 30s busy_timeout (~2 minutes), re-raised, and the
caller logged and moved on. The trade was lost, leaving a stranded qty-0 PENDING row with no
transaction and no broker id -- and nothing sweeps those (the only code touching PENDING entries
DELETES them). Dev showed 161 lock events in one day and 5 entries lost the same way.

The retry lives in the funded loop because that is the only place the RM-sized quantity and
safeguard stop are still in memory; a stranded row cannot be re-sized afterwards.
"""
import pytest

from ba2_trade_platform.core.TradeManager import TradeManager


class _Order:
    id, symbol, quantity = 460, "CVS", 1.0


def _tm(monkeypatch):
    tm = TradeManager.__new__(TradeManager)
    import logging
    tm.logger = logging.getLogger("t")
    monkeypatch.setattr(TradeManager, "_ENTRY_SUBMIT_BACKOFF_S", 0.0, raising=False)
    return tm


def test_transient_lock_is_retried_and_the_trade_still_opens(monkeypatch):
    """THE REGRESSION: locked once, then succeeds -> the order must reach the broker."""
    calls = []

    class _Acct:
        def submit_order(self, order, sl_price=None):
            calls.append(sl_price)
            if len(calls) == 1:
                raise RuntimeError("(sqlite3.OperationalError) database is locked")
            return order

    tm = _tm(monkeypatch)
    out = tm._submit_funded_entry_with_retry(_Acct(), _Order(), sl_price=82.29)
    assert out is not None, "a transient DB lock must not lose the trade"
    assert len(calls) == 2
    assert calls == [82.29, 82.29], "the RM safeguard stop must survive the retry unchanged"


def test_a_broker_rejection_is_never_re_sent(monkeypatch):
    """A non-lock failure is a real answer. Re-sending risks a DUPLICATE order, which is worse
    than a missed one -- so it must propagate on the first attempt."""
    calls = []

    class _Acct:
        def submit_order(self, order, sl_price=None):
            calls.append(1)
            raise ValueError('{"code":40310000,"message":"potential wash trade detected"}')

    tm = _tm(monkeypatch)
    with pytest.raises(ValueError, match="40310000"):
        tm._submit_funded_entry_with_retry(_Acct(), _Order(), sl_price=None)
    assert len(calls) == 1, "a broker rejection must not be retried"


def test_persistent_lock_gives_up_and_reports_rather_than_raising(monkeypatch):
    """If the lock never clears we return None so the caller records a failed submit for THIS
    symbol and still processes the rest of the funded batch."""
    calls = []

    class _Acct:
        def submit_order(self, order, sl_price=None):
            calls.append(1)
            raise RuntimeError("database is locked")

    tm = _tm(monkeypatch)
    assert tm._submit_funded_entry_with_retry(_Acct(), _Order()) is None
    assert len(calls) == TradeManager._ENTRY_SUBMIT_RETRIES


# --------------------------------------------------------------------------------------------- #
# ROOT CAUSE: the lock was SELF-INFLICTED, not contention.
#
# e976e70 added a leg-stamping query between "order.quantity = fo.quantity" and submit_order().
# `order` is dirty at that point, so the query AUTOFLUSHES it, which takes sqlite's single write
# lock on the outer session. submit_order then inserts the Transaction from a DIFFERENT session
# and blocks on its own caller for the full 30s busy_timeout, 4 times, and the funded trade dies.
#
# Deployed Saturday 2026-08-08 17:59; Sunday the market was closed so no entries ran; Monday
# 2026-08-10 is the ONLY day with lock events -- 161 on dev, 4 on prod, none before.
# --------------------------------------------------------------------------------------------- #

def test_autoflush_before_a_second_session_write_is_what_locks_the_db(tmp_path):
    """Pins the mechanism against a REAL sqlite file: a dirty session + an autoflushing query
    blocks an unrelated session's INSERT; suppressing the autoflush does not."""
    from sqlmodel import Field, Session, SQLModel, create_engine, select

    from typing import Optional

    class _Ord(SQLModel, table=True, extend_existing=True):
        __tablename__ = "lockprobe_ord"
        id: Optional[int] = Field(default=None, primary_key=True)
        qty: float = 0.0
        parent: Optional[int] = None

    eng = create_engine(f"sqlite:///{tmp_path/'p.db'}",
                        connect_args={"check_same_thread": False, "timeout": 1.0})
    SQLModel.metadata.create_all(eng, tables=[_Ord.__table__])
    with Session(eng) as s:
        s.add(_Ord(id=1, qty=0.0)); s.add(_Ord(id=2, qty=0.0, parent=1)); s.commit()

    def _other_session_insert():
        """Stands in for submit_order's Transaction insert."""
        try:
            with Session(eng) as s2:
                s2.add(_Ord(qty=99.0)); s2.commit()
            return True
        except Exception as e:
            assert "database is locked" in str(e).lower()
            return False

    # THE BUG: autoflush takes the write lock, the other session cannot write.
    with Session(eng) as s:
        o = s.get(_Ord, 1)
        o.qty = 1.0
        s.exec(select(_Ord).where(_Ord.parent == 1)).all()   # <- autoflush -> write lock
        assert _other_session_insert() is False, "expected the self-inflicted lock"
        s.rollback()

    # THE FIX: same work, no early flush, so the lock is never held across the call.
    with Session(eng) as s:
        o = s.get(_Ord, 1)
        o.qty = 1.0
        with s.no_autoflush:
            legs = s.exec(select(_Ord).where(_Ord.parent == 1)).all()
        for leg in legs:
            leg.qty = o.qty
        assert _other_session_insert() is True, "no_autoflush must keep the write lock unheld"
        s.rollback()
