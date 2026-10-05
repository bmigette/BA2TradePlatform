"""Review round 4 (Opus verification of b3d0f256), items 1-7. Each test FAILED on b3d0f256."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtectionOrder, SLICE_LIVE, SLICE_UNKNOWN,
)
from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import PortfolioAllocationSymbol
from ba2_trade_platform.core.types import ActivityLogSeverity
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity
from tests.test_allocator_protection_round3 import (  # noqa: F401 -- fixtures + helpers reused
    _age, _live_oco, _live_qty, _slices, _the_single, _timeout_after_acceptance, _world, _rows, activity,
    acct, broker,
)

T = ap.TpTarget


# ======================================================================== item 1
def test_r4_1_a_filled_target_is_not_retaken_by_growth_or_repair(acct, broker, monkeypatch):
    """TP1 60@30% fills, the price falls back to 58 (TP1 is valid again), shares are added: the repair
    path must NOT place 60 again."""
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.3)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.fill(oco.complex_order_id, "TP")
    aps.reconcile_account(acct)
    broker.prices["ABC"] = 58.0
    broker.positions["ABC"] = broker.positions["ABC"] + Decimal(12)
    aps.reconcile_account(acct)
    assert all(price != 60.0 for _, price in _live_oco(broker))


# ======================================================================== item 2
def _unknown(acct, broker):
    broker.single_raise_on_place = TimeoutError("connect timeout")
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.single_raise_on_place = None


def test_r4_2_forget_is_refused_while_the_slice_is_young(acct, broker):
    _unknown(acct, broker)
    broker.history_fails = True
    assert not aps.forget_unknown_slices(acct, "ABC").ok
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]


def test_r4_2_forget_is_refused_when_the_search_works(acct, broker):
    """A working search either finds the order (it is adopted) or finds nothing (the automatic
    resolution closes the slice): forgetting by hand could only create a double protection."""
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _age(aps.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    result = aps.forget_unknown_slices(acct, "ABC")
    assert not result.ok
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]


def test_r4_2_forget_is_allowed_when_the_search_itself_fails(acct, broker):
    _unknown(acct, broker)
    _age(aps.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    broker.history_fails = True
    assert aps.forget_unknown_slices(acct, "ABC").ok


# ======================================================================== item 3
def test_r4_3_an_ascending_plain_history_does_not_hide_a_filled_stop(acct, broker):
    broker.history_ascending = True
    old = datetime.now(timezone.utc) - timedelta(days=3)
    for _ in range(60):                                          # 60 OLD foreign orders come first (ascending)
        broker.place_foreign_stop("ZZZ", 1, 5.0, "other", received_at=old)
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    ours = max(broker.singles)
    broker.fill_single(ours)                                     # it FILLED: the position left
    _age(aps.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    aps.reconcile_account(acct)
    (s,) = _slices()
    assert s.sl_order_id == ours and s.state != "LOST_REJECTED"


def test_r4_3_an_ascending_complex_history_is_read_in_full(acct, broker):
    old = datetime.now(timezone.utc) - timedelta(days=3)
    mine = SimpleNamespace(id=9, orders=[SimpleNamespace(
        external_identifier="ba2prot:x", received_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc))])
    fillers = [SimpleNamespace(id=100 + i, orders=[SimpleNamespace(
        external_identifier="other", received_at=old + timedelta(seconds=i),
        updated_at=old + timedelta(seconds=i))]) for i in range(120)]
    rows = fillers + [mine]                                      # oldest first
    calls = []

    async def history(session, per_page=50, page_offset=0):
        calls.append(page_offset)
        return rows[page_offset * per_page:(page_offset + 1) * per_page]
    broker.get_complex_order_history = history
    found = acct.find_protective_orders_by_tag("ba2prot:x", since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert [f[1] for f in found] == [9] and max(calls) == 2


def test_r4_3_history_ordering_is_a_listed_broker_assumption():
    from tests.allocator_protection_fakes import BROKER_ASSUMPTIONS
    assert any(a.startswith("A10") and "ordering" in a for a in BROKER_ASSUMPTIONS)


# ======================================================================== item 4
def test_r4_4_an_adopted_order_of_another_size_resizes_the_slice_and_alerts(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _the_single(broker).size = Decimal(40)
    aps.reconcile_account(acct)
    oid = min(broker.singles)
    adopted = [x for x in _slices() if x.sl_order_id == oid]
    assert [x.quantity for x in adopted] == [40]                  # the broker's figure, not the slice's 100


def test_r4_4_the_mismatch_alert_is_raised(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _the_single(broker).size = Decimal(40)
    p = aps.get_protection(1, "ABC")
    s = _slices()[0]
    aps._resolve_unknown_slice(acct, p, s)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_QUANTITY_MISMATCH


# ======================================================================== item 5
def test_r4_5_pins_are_listed_in_the_note_and_the_log(acct, broker, monkeypatch, activity):
    _world(monkeypatch)
    broker.positions["OTHER"] = Decimal(100)
    broker.prices["OTHER"] = 50.0
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.fill(oco.complex_order_id, "TP")
    aps.reconcile_account(acct)
    assert "pinned OTHER 50%" in aps.get_protection(1, "ABC").last_fill_note
    fills = [c for c in activity if c["data"].get("code") == ap.CODE_FILL]
    assert fills and "pinned OTHER 50%" in fills[0]["description"]


# ======================================================================== item 6
def test_r4_6_a_site_cancel_of_an_unknown_order_is_never_replaced(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.external_cancel_single(next(iter(broker.singles)))
    placed = broker.single_place_count()
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)
    assert broker.single_place_count() == placed and _live_qty(broker) == 0


# ======================================================================== item 7
def test_r4_7_a_partial_tp_fill_keeps_the_rest_of_the_target(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5), T(70.0, 0.5)]).ok
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP", qty=20)             # 20 of 50 sold
    aps.reconcile_account(acct)
    flags = [bool(t.get("filled")) for t in aps.get_protection(1, "ABC").tp_targets]
    assert flags == [False, False]                                # NOT marked filled
    assert aps.replace_protection(acct, "ABC").ok
    assert sorted(_live_oco(broker)) == [(30, 60.0), (50, 70.0)]  # the unfilled 30 keep their TP price


def test_r4_7_a_fully_filled_slice_marks_the_target_filled(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5), T(70.0, 0.5)]).ok
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP")
    aps.reconcile_account(acct)
    assert [bool(t.get("filled")) for t in aps.get_protection(1, "ABC").tp_targets] == [True, False]


def test_r4_7_effective_targets_honours_a_partial_ratio():
    eff = ap.effective_targets([{"price": 60.0, "fraction": 0.5, "taken": 0.4},
                                {"price": 70.0, "fraction": 0.5}], 50.0)
    assert [round(t.fraction, 6) for t in eff.usable] == [0.375, 0.625]
