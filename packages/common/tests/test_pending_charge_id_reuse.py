"""A pending charge must not latch onto an UNRELATED transaction that reused its id (2026-11-07).

SQLite (file-backed backtests AND live) hands a deleted max id to the next insert. An option entry
that never filled has its WAITING transaction deleted; the next transaction -- say a stock buy on
another symbol -- gets the same id. The charge, keyed by id alone, then read as "still in flight"
for as long as that unrelated position stayed open, refusing every later entry on its underlying
(one_per_underlying) and consuming deployment budget. The in-memory GA store never reuses ids, so
the GA never saw it: P50k TOP2 scored 9.12 in the GA and 8.14 when persisted.
"""
import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.option_book import CandidateStructure

EID = 7


def cand(underlying="ZTO"):
    return CandidateStructure(underlying=underlying, strategy="covered_call", max_loss=4700.0,
                              notional=4200.0, short_put_assignment=0.0, is_defined_risk=True)


@pytest.fixture(autouse=True)
def _clean():
    rm.reset_state()
    yield
    rm.reset_state()


def _record(monkeypatch, txn_id, symbol_at_record):
    monkeypatch.setattr(rm, "_transaction_symbol", lambda tid: symbol_at_record)
    rm.record_submitted(EID, txn_id, cand())


def test_a_charge_whose_id_now_names_another_symbol_is_dropped(monkeypatch):
    _record(monkeypatch, 4, "ZTO")
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: {4: "WPM"})   # id 4 reused
    assert rm._prune_pending(EID, []) == []


def test_a_charge_whose_transaction_is_still_its_own_is_kept(monkeypatch):
    _record(monkeypatch, 4, "ZTO")
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: {4: "ZTO"})
    assert rm._prune_pending(EID, []) == [cand()]


def test_a_symbol_unread_at_record_time_falls_back_to_the_id(monkeypatch):
    _record(monkeypatch, 4, None)
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: {4: "WPM"})
    assert rm._prune_pending(EID, []) == [cand()]


def test_a_gone_transaction_still_drops_the_charge(monkeypatch):
    _record(monkeypatch, 4, "ZTO")
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: {})
    assert rm._prune_pending(EID, []) == []


def test_an_unreadable_ledger_still_keeps_every_charge(monkeypatch):
    _record(monkeypatch, 4, "ZTO")
    monkeypatch.setattr(rm, "_live_transactions", lambda eid: None)
    assert rm._prune_pending(EID, []) == [cand()]


def test_record_submitted_stores_the_transactions_real_symbol_in_memory():
    """The real read path (no stub): under the backtest's in-memory store the charge records the
    row's own symbol, and the charge stays owed while that same row is still WAITING."""
    from datetime import datetime, timezone

    from ba2_common.core import trade_store as ts
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    with ts.inmem_trades():
        tid = add_instance(Transaction(symbol="ZTO", quantity=2, side=OrderDirection.SELL,
                                       status=TransactionStatus.WAITING, expert_id=EID,
                                       created_at=datetime.now(timezone.utc)))
        rm.record_submitted(EID, tid, cand())
        [charge] = rm._PENDING[rm._sleeve_key(EID)]
        assert charge.transaction_symbol == "ZTO"
        assert rm._prune_pending(EID, []) == [cand()]          # still its own WAITING row


def test_a_missing_row_at_record_time_falls_back_to_the_id(monkeypatch):
    from ba2_common.core import trade_store as ts

    with ts.inmem_trades():
        rm.record_submitted(EID, 987654, cand())
        [charge] = rm._PENDING[rm._sleeve_key(EID)]
        assert charge.transaction_symbol is None
