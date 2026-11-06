"""Pending charges carry the member tag (plan 2026-11-06, Task 2). Inert when untagged."""
from datetime import date
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.TradeActions as ta
from ba2_common.core.option_book import CandidateStructure
from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
from ba2_common.core.types import OptionRight

EID = 7


def cand(max_loss, strategy="cash_secured_put"):
    return CandidateStructure(underlying="MSFT", strategy=strategy, max_loss=max_loss,
                              notional=max_loss, short_put_assignment=max_loss,
                              is_defined_risk=True)


def visible(txn_id):
    return OptionStructure(
        transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
        legs=(LifecycleLeg(contract_symbol="AAPL270115P100", net_qty=-1.0, strike=100.0,
                           option_type=OptionRight.PUT, expiry=date(2027, 1, 15),
                           underlying="AAPL", entry_premium=2.0),),
        quantity=1.0, multiplier=100, entry_net_premium=-2.0)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    rm.reset_state()
    monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: {101, 102})
    yield
    rm.reset_state()


def test_a_charge_carries_the_tag_it_was_given():
    rm.record_submitted(EID, 101, cand(500.0), entry_tag=14)
    (charge,) = rm._PENDING[rm._sleeve_key(EID)]
    assert charge.entry_tag == 14


def test_a_charge_recorded_the_old_way_has_no_tag():
    rm.record_submitted(EID, 101, cand(500.0))
    (charge,) = rm._PENDING[rm._sleeve_key(EID)]
    assert charge.entry_tag is None


def test_prune_pending_still_returns_candidates_and_drops_the_visible_one():
    c1, c2 = cand(500.0), cand(700.0)
    rm.record_submitted(EID, 101, c1, entry_tag=14)
    rm.record_submitted(EID, 102, c2, entry_tag=11)
    kept = rm._prune_pending(EID, [visible(102)])       # 102 is in the book now
    assert kept == [c1]


def test_prune_pending_charges_keeps_the_tags():
    c1, c2 = cand(500.0), cand(700.0)
    rm.record_submitted(EID, 101, c1, entry_tag=14)
    rm.record_submitted(EID, 102, c2, entry_tag=11)
    kept = rm._prune_pending_charges(EID, [visible(102)])
    assert [(c.candidate, c.entry_tag) for c in kept] == [(c1, 14)]


def test_the_action_forwards_the_tag_only_when_it_has_one():
    """An untagged action must call record_submitted EXACTLY as before (existing tests patch
    it with a 3-positional lambda), a tagged one adds the keyword."""
    f = ta._OptionEntryAction._record_submitted_kwargs
    assert f(SimpleNamespace(entry_tag=None)) == {}
    assert f(SimpleNamespace(entry_tag=14)) == {"entry_tag": 14}
