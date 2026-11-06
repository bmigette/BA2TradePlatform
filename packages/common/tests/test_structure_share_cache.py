"""Per-member committed capital and share (plan 2026-11-06, Tasks 3-5)."""
from __future__ import annotations

import threading
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.option_book import CandidateStructure
from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
from ba2_common.core.types import OptionRight

EID = 7
CSP, WHEEL, CBS = 11, 14, 19
T1, T2, T3 = (datetime(2026, 11, d, 10, 0) for d in (2, 3, 4))


def _leg(sym, qty, strike, right, premium=2.0):
    return LifecycleLeg(contract_symbol=sym, net_qty=qty, strike=strike, option_type=right,
                        expiry=date(2027, 1, 15), underlying="AAPL", entry_premium=premium)


def short_put(txn_id, strike=100.0, qty=1.0):
    """Cash-secured put: committed = strike * 100 * qty (naked short put)."""
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
                           legs=(_leg(f"AAPL270115P{strike}", -qty, strike, OptionRight.PUT),),
                           quantity=qty, multiplier=100, entry_net_premium=-2.0)


def long_call(txn_id, premium=2.0):
    """Long call: committed = the premium paid = premium * 100."""
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="long_call",
                           legs=(_leg("AAPL270115C100", 1.0, 100.0, OptionRight.CALL),),
                           quantity=1.0, multiplier=100, entry_net_premium=premium)


def covered_call(txn_id, strike=110.0):
    return OptionStructure(transaction_id=txn_id, underlying="AAPL", strategy="covered_call",
                           legs=(_leg(f"AAPL270115C{strike}", -1.0, strike, OptionRight.CALL),),
                           quantity=1.0, multiplier=100, entry_net_premium=-1.0)


def lot(txn_id, price=100.0, qty=100.0):
    return SimpleNamespace(id=txn_id, symbol="AAPL", open_price=price, quantity=qty)


def cand(max_loss, strategy="cash_secured_put"):
    return CandidateStructure(underlying="MSFT", strategy=strategy, max_loss=max_loss,
                              notional=max_loss, short_put_assignment=max_loss,
                              is_defined_risk=True)


class World:
    """The data seams of the share build, as a mutable fixture (the real readers are covered by
    the in-mem-store test in Task 5)."""

    def __init__(self, monkeypatch):
        self.structures, self.unbuildable, self.tags, self.lots = [], [], {}, []
        self.equity = 100_000.0
        self.reads = 0
        monkeypatch.setattr(rm, "sleeve_structures", self._structures)
        monkeypatch.setattr(rm, "transaction_entry_tag", lambda tid: self.tags.get(tid))
        monkeypatch.setattr(rm, "assigned_stock_lots", lambda eid: list(self.lots))
        # None = "ledger unreadable": every in-flight charge is KEPT (the conservative reading)
        monkeypatch.setattr(rm, "_live_transaction_ids", lambda eid: None)

    def _structures(self, eid):
        self.reads += 1
        return list(self.structures), list(self.unbuildable)

    @property
    def account(self):
        return SimpleNamespace(
            get_account_snapshot=lambda: SimpleNamespace(equity=self.equity))


@pytest.fixture
def world(monkeypatch):
    rm.reset_state()
    yield World(monkeypatch)
    rm.reset_state()


def share(world, tag, at=T1):
    return rm.member_share(world.account, EID, tag, at)


def test_committed_is_the_members_own_open_structures(world):
    world.structures = [short_put(1), short_put(2, strike=50.0), long_call(3)]
    world.tags = {1: CSP, 2: CSP, 3: CBS}
    csp = share(world, CSP)
    assert csp.committed == 15_000.0
    assert csp.share_pct == pytest.approx(15.0)
    assert csp.unmeasurable == ()
    assert share(world, CBS).committed == 200.0
    assert share(world, 99).committed == 0.0           # a member with nothing open: measured 0


def test_an_untagged_structure_belongs_to_no_member(world):
    world.structures = [short_put(1)]
    world.tags = {}
    assert share(world, CSP).committed == 0.0


def test_a_covered_call_adds_nothing_the_lots_cost_basis_is_its_capital(world):
    world.structures = [covered_call(2)]
    world.lots = [lot(900)]
    world.tags = {2: WHEEL, 900: WHEEL}
    assert share(world, WHEEL).committed == 10_000.0     # 100 sh * 100, NOT +11_000


def test_the_wheels_share_holds_through_assignment_and_the_covered_call(world):
    world.tags = {1: WHEEL, 900: WHEEL, 2: WHEEL}
    world.structures = [short_put(1)]                    # phase 1: the short put
    assert share(world, WHEEL, T1).committed == 10_000.0
    world.structures, world.lots = [], [lot(900)]        # phase 2: assigned, put gone
    assert share(world, WHEEL, T2).committed == 10_000.0
    world.structures = [covered_call(2)]                 # phase 3: call written on the lot
    assert share(world, WHEEL, T3).committed == 10_000.0


def test_an_untagged_lot_is_nobodys(world):
    world.lots = [lot(900)]
    assert share(world, WHEEL).committed == 0.0


def test_pending_charges_count_until_the_book_sees_them(world):
    rm.record_submitted(EID, 555, cand(8_000.0), entry_tag=CSP)
    assert share(world, CSP, T1).committed == 8_000.0
    world.structures, world.tags = [short_put(555)], {555: CSP}    # now visible: charge pruned
    r = share(world, CSP, T2)
    assert r.committed == 10_000.0                       # the book's number, not 8_000 + 10_000


def test_a_pending_covered_call_adds_nothing(world):
    rm.record_submitted(EID, 556, cand(11_000.0, "covered_call"), entry_tag=WHEEL)
    assert share(world, WHEEL).committed == 0.0


def test_an_unmeasurable_structure_makes_the_members_share_unknown_not_zero(world):
    bad = OptionStructure(transaction_id=1, underlying="AAPL", strategy="cash_secured_put",
                          legs=(_leg("AAPL270115P100", -1.0, None, OptionRight.PUT),),
                          quantity=1.0, multiplier=100, entry_net_premium=-2.0)
    world.structures, world.tags = [bad, long_call(3)], {1: CSP, 3: CBS}
    r = share(world, CSP)
    assert r.share_pct is None and r.committed is None
    assert any("transaction 1" in why for why in r.unmeasurable)
    assert share(world, CBS).share_pct == pytest.approx(0.2)    # other members unaffected


def test_an_unbuildable_tagged_transaction_blinds_its_member(world):
    world.unbuildable, world.tags = [9], {9: CSP}
    r = share(world, CSP)
    assert r.share_pct is None and any("transaction 9" in why for why in r.unmeasurable)


def test_a_lot_without_a_cost_basis_blinds_its_member(world):
    world.lots, world.tags = [lot(900, price=None)], {900: WHEEL}
    r = share(world, WHEEL)
    assert r.share_pct is None and any("transaction 900" in why for why in r.unmeasurable)


@pytest.mark.parametrize("equity", [None, 0.0, -5.0])
def test_unusable_equity_is_unknown_never_a_share(world, equity):
    world.equity = equity
    world.structures, world.tags = [short_put(1)], {1: CSP}
    r = share(world, CSP)
    assert r.share_pct is None and r.equity is None
    assert any("equity" in why for why in r.unmeasurable)


def test_a_decision_time_is_required(world):
    with pytest.raises(ValueError, match="decision time"):
        rm.member_share(world.account, EID, CSP, None)


# ------------------------------------------------------------------ the cache (Task 4)
def test_one_cycle_reads_the_book_once(world):
    world.structures, world.tags = [short_put(1)], {1: CSP}
    share(world, CSP, T1)
    share(world, CBS, T1)
    share(world, CSP, T1)
    assert world.reads == 1


def test_a_new_decision_time_rebuilds_from_the_book(world):
    world.structures, world.tags = [short_put(1)], {1: CSP}
    assert share(world, CSP, T1).committed == 10_000.0
    world.structures.append(short_put(2, strike=50.0))
    world.tags[2] = CSP
    assert share(world, CSP, T1).committed == 10_000.0     # same cycle: cached
    assert share(world, CSP, T2).committed == 15_000.0     # next cycle: rebuilt, no drift
    assert world.reads == 2


def test_an_admitted_entry_is_seen_by_the_next_symbol_of_the_same_cycle(world):
    assert share(world, CSP, T1).committed == 0.0
    rm.record_submitted(EID, 700, cand(5_000.0), entry_tag=CSP)
    r = share(world, CSP, T1)
    assert r.committed == 5_000.0 and r.share_pct == pytest.approx(5.0)
    assert world.reads == 1                                 # incremented, not rebuilt


def test_a_tagged_record_submitted_increments_the_cached_tag_total_untagged_does_not(world):
    key = rm._sleeve_key(EID)
    share(world, CSP, T1)
    assert rm._SHARE_CACHE[key].committed.get(CSP, 0.0) == 0.0
    rm.record_submitted(EID, 700, cand(5_000.0), entry_tag=CSP)
    assert rm._SHARE_CACHE[key].committed[CSP] == 5_000.0          # the cached total moved
    rm.record_submitted(EID, 701, cand(9_000.0))                   # untagged: nothing moves
    assert rm._SHARE_CACHE[key].committed[CSP] == 5_000.0
    assert rm._SHARE_EPOCH[key] == 1


def test_the_increment_goes_to_its_own_tag_only(world):
    share(world, CSP, T1)
    rm.record_submitted(EID, 700, cand(5_000.0), entry_tag=CSP)
    assert share(world, CBS, T1).committed == 0.0


def test_a_submitted_covered_call_does_not_move_the_cache(world):
    share(world, WHEEL, T1)
    rm.record_submitted(EID, 701, cand(11_000.0, "covered_call"), entry_tag=WHEEL)
    assert share(world, WHEEL, T1).committed == 0.0


def test_an_untagged_submission_touches_no_share_state(world):
    rm.record_submitted(EID, 702, cand(5_000.0))
    assert rm._SHARE_CACHE == {} and rm._SHARE_EPOCH == {}


def test_a_submission_that_raced_a_build_is_not_lost(world, monkeypatch):
    """A record_submitted landing AFTER the build read its pending snapshot but BEFORE the
    cache was stored must not vanish: the build is not stored and is redone."""
    real = rm._prune_pending_charges
    fired = []

    def racing(eid, structures):
        out = real(eid, structures)
        if not fired:
            fired.append(1)
            rm.record_submitted(EID, 703, cand(4_000.0), entry_tag=CSP)
        return out

    monkeypatch.setattr(rm, "_prune_pending_charges", racing)
    assert share(world, CSP, T1).committed == 4_000.0
    assert share(world, CSP, T1).committed == 4_000.0       # and it stays
    assert world.reads == 2                                  # first build discarded, second kept


@pytest.mark.parametrize("reset", [rm.reset_state, rm.reset_pending_charges,
                                   rm.reset_thread_state])
def test_the_run_boundary_resets_clear_the_cache(world, monkeypatch, reset):
    # backtest-shaped key so reset_thread_state sees this thread's keys
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    assert rm._SHARE_CACHE
    reset()
    assert rm._SHARE_CACHE == {}


def test_each_backtest_thread_has_its_own_cache(world, monkeypatch):
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    main_keys = set(rm._SHARE_CACHE)
    seen = {}

    def other():
        share(world, CSP, T1)
        seen["both"] = set(rm._SHARE_CACHE)
        rm.reset_thread_state()                 # only THIS thread's key
        seen["after"] = set(rm._SHARE_CACHE)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert len(seen["both"]) == 2 and seen["after"] == main_keys


def test_live_and_backtest_keys_do_not_share_a_cache(world, monkeypatch):
    share(world, CSP, T1)                                   # live key (None, EID)
    assert set(rm._SHARE_CACHE) == {(None, EID)}
    monkeypatch.setattr("ba2_common.core.trade_store.inmem_trades_active", lambda: True)
    share(world, CSP, T1)
    assert len(rm._SHARE_CACHE) == 2
