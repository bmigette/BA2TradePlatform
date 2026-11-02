"""Review round 3 (Opus verification of 06530d43), items 2-7. Each test FAILED on 06530d43.

No broker is contacted: everything runs through the real ``TastyTradeAccount`` methods over the fake
complex-order API (tests/allocator_protection_fakes.py).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import select
from tastytrade.order import OrderStatus as TTOrderStatus

from ba2_trade_platform.core import allocator_exclusion as aex
from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtectionOrder, AllocatorWeightChange, SLICE_LIVE, SLICE_LOST_REJECTED, SLICE_UNKNOWN,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import PortfolioAllocationSymbol, TradingOrder
from ba2_trade_platform.core.types import (
    ActivityLogSeverity, OrderDirection, OrderOpenType, OrderStatus, OrderType,
)
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity

T = ap.TpTarget


@pytest.fixture
def broker():
    b = FakeTastyBroker()
    b.positions["ABC"] = Decimal(100)
    b.prices["ABC"] = 50.0
    return b


@pytest.fixture
def acct(broker):
    with patch_equity(broker):
        yield make_account(broker)


@pytest.fixture(autouse=True)
def activity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        aps, "log_activity",
        lambda severity, activity_type, description, data=None, source_expert_id=None,
        source_account_id=None: calls.append({"severity": severity, "description": description,
                                              "data": data or {}}))
    return calls


def _slices(symbol="ABC"):
    return aps.get_slices(aps.get_protection(1, symbol).id)


def _age(minutes):
    with get_db() as session:
        for s in session.exec(select(AllocatorProtectionOrder)).all():
            s.placed_at = s.placed_at - timedelta(minutes=minutes)
            session.add(s)
        session.commit()


def _live_qty(b):
    total = 0
    for r in b.complex.values():
        if all(m.status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED) for m in r["members"]):
            total += int(r["members"][0].size)
    for r in b.singles.values():
        if (r["member"].status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED)
                and str(r["member"].order_type.value) != "Market"):
            total += int(r["member"].size)
    return total


def _timeout_after_acceptance(b):
    original = b.place_order
    state = {"n": 0}

    async def place_then_timeout(session, order, dry_run=True):
        r = await original(session, order, dry_run=dry_run)
        if not dry_run and state["n"] == 0:
            state["n"] += 1
            raise TimeoutError("read timed out")
        return r
    b.place_order = place_then_timeout


def _the_single(b):
    (member,) = [r["member"] for r in b.singles.values()]
    return member


def _set_protection(**fields):
    p = aps.get_protection(1, "ABC")
    for k, v in fields.items():
        setattr(p, k, v)
    update_instance(p)


# ======================================================================== item 2
def test_i2_clock_skew_does_not_freeze_our_own_accepted_order(acct, broker, activity):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _the_single(broker).received_at = datetime.now(timezone.utc) - timedelta(minutes=20)   # broker clock 20 min behind
    aps.reconcile_account(acct)
    assert [s.state for s in _slices()] == [SLICE_LIVE]
    assert any(c["severity"] == ActivityLogSeverity.WARNING and "received" in c["description"] for c in activity)


def test_i2_a_non_utc_received_at_is_converted_not_stripped():
    plus5 = timezone(timedelta(hours=5))
    assert aps._naive(datetime(2026, 10, 5, 12, 0, tzinfo=plus5)) == datetime(2026, 10, 5, 7, 0)
    assert aps._naive(datetime(2026, 10, 5, 12, 0)) == datetime(2026, 10, 5, 12, 0)   # naive means UTC


def test_i2_a_non_utc_received_at_still_adopts(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _the_single(broker).received_at = datetime.now(timezone(timedelta(hours=-7)))
    aps.reconcile_account(acct)
    assert [s.state for s in _slices()] == [SLICE_LIVE]


def test_i2_a_size_mismatch_warns_and_still_adopts(acct, broker, activity):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _the_single(broker).size = Decimal(7)                  # the broker says 7, the slice says 100
    aps.reconcile_account(acct)
    assert _slices()[0].sl_order_id is not None
    assert any(c["severity"] == ActivityLogSeverity.WARNING and "quantity" in c["description"] for c in activity)


def test_i2_an_order_cancelled_on_the_site_is_an_alarm_not_a_freeze_and_not_re_placed(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.external_cancel_single(next(iter(broker.singles)))       # the operator cancelled it on TastyTrade
    placed = broker.single_place_count()
    aps.reconcile_account(acct)
    (s,) = _slices()
    assert s.state == "LOST_CANCELLED" and s.closed_at is None       # item 6: respected, never auto re-placed
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED
    assert broker.single_place_count() == placed
    assert aps.disable_protection(acct, "ABC").ok                    # and not frozen


def test_i2_forget_closes_an_unknown_slice_with_a_log_entry(acct, broker, activity):
    broker.single_raise_on_place = TimeoutError("connect timeout")
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.single_raise_on_place = None
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]
    _age(aps.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    broker.history_fails = True                                      # the search itself cannot be completed
    result = aps.forget_unknown_slices(acct, "ABC")
    assert result.ok
    assert all(s.closed_at is not None and s.state != SLICE_UNKNOWN for s in _slices())
    assert any(c["data"].get("code") == "UNKNOWN_FORGOTTEN" for c in activity)
    assert aps.disable_protection(acct, "ABC").ok


def test_i2_forget_refuses_a_slice_that_has_a_broker_id(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [])
    result = aps.forget_unknown_slices(acct, "ABC")
    assert not result.ok and [s.state for s in _slices()] == [SLICE_LIVE]


# ======================================================================== item 3
def _counting_lock(monkeypatch):
    taken = []
    real = aps.protection_lock

    def counting(account_id):
        taken.append(account_id)
        return real(account_id)
    monkeypatch.setattr(aps, "protection_lock", counting)
    return taken


def test_i3_no_row_takes_no_lock(acct, broker, monkeypatch):
    taken = _counting_lock(monkeypatch)
    assert aps.before_sale(acct, "ABC", 10) is None and taken == []


def test_i3_disabled_with_nothing_resting_takes_no_lock(acct, broker, monkeypatch):
    aps.save_protection(acct, "ABC", 45.0, [])
    aps.disable_protection(acct, "ABC")
    taken = _counting_lock(monkeypatch)
    assert aps.before_sale(acct, "ABC", 10) is None and taken == []


def _hide_slices():
    """The window between a re-placement's reads and its PLACING row: enabled, nothing visibly resting."""
    with get_db() as session:
        for s in session.exec(select(AllocatorProtectionOrder)).all():
            s.state, s.closed_at = "CANCELLED_BY_US", datetime.utcnow()
            session.add(s)
        session.commit()


def test_i3_an_enabled_protection_always_takes_the_lock(acct, broker, monkeypatch):
    aps.save_protection(acct, "ABC", 45.0, [])
    _hide_slices()
    taken = _counting_lock(monkeypatch)
    aps.before_sale(acct, "ABC", 10)
    assert taken                                             # even if it looked as if nothing rested


def test_i3_a_sale_cannot_pass_the_lock_a_re_placement_holds(acct, broker, monkeypatch):
    """The lock is entered BEFORE anything is cancelled or decided, for an enabled protection."""
    aps.save_protection(acct, "ABC", 45.0, [])
    _hide_slices()

    class Held:
        def __enter__(self):
            raise RuntimeError("blocked on the lock")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(aps, "protection_lock", lambda account_id: Held())
    with pytest.raises(RuntimeError, match="blocked on the lock"):
        aps.before_sale(acct, "ABC", 10)
    assert aps.get_protection(1, "ABC").enabled


# ======================================================================== item 4
def _world(monkeypatch, members=("ABC", "OTHER")):
    from ba2_trade_platform.core import portfolio_allocation_store as store
    from ba2_trade_platform.core import utils as core_utils
    monkeypatch.setattr(store, "get_managed_labels", lambda a: [SimpleNamespace(label="L")])
    monkeypatch.setattr(core_utils, "get_symbols_by_label", lambda labels: {"L": list(members)})


def _tp_half_fill(acct, broker):
    broker.positions["OTHER"] = Decimal(100)
    broker.prices["OTHER"] = 50.0
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.fill(oco.complex_order_id, "TP")
    aps.reconcile_account(acct)


def _rows():
    with get_db() as session:
        return {(r.label, r.symbol): r.weight_pct for r in session.exec(select(PortfolioAllocationSymbol)).all()}


def test_i4_market_mode_pins_the_other_members_at_their_prefill_share(acct, broker, monkeypatch):
    _world(monkeypatch)
    _tp_half_fill(acct, broker)
    assert _rows() == {("L", "ABC"): 25.0, ("L", "OTHER"): 50.0}      # the label total drops by the freed 25
    with get_db() as session:
        pinned = session.exec(select(AllocatorWeightChange).where(AllocatorWeightChange.symbol == "OTHER")).all()
    assert [(c.reason, c.before_pct, c.after_pct) for c in pinned] == [("pinned", 50.0, 50.0)]


def test_i4_cost_mode_is_not_cut_twice(acct, broker, monkeypatch):
    from ba2_trade_platform.core.portfolio_allocation_store import set_allocation_config
    set_allocation_config(1, valuation_mode="cost")
    _world(monkeypatch)
    _tp_half_fill(acct, broker)
    assert _rows() == {("L", "ABC"): 25.0, ("L", "OTHER"): 50.0}


def test_i4_a_missing_price_writes_a_loud_failure_and_claims_nothing(acct, broker, monkeypatch, activity):
    _world(monkeypatch)
    broker.positions["OTHER"] = Decimal(100)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.prices.pop("OTHER", None)                                    # no quote for OTHER
    broker.fill(oco.complex_order_id, "TP")
    aps.reconcile_account(acct)
    failures = [c for c in activity if c["data"].get("code") == "WEIGHT_FAILED"]
    assert failures and failures[0]["severity"] == ActivityLogSeverity.FAILURE
    assert "OTHER" in failures[0]["description"] and "L" in failures[0]["description"]
    fills = [c for c in activity if c["data"].get("code") == ap.CODE_FILL]
    assert fills and all("stays unallocated" not in c["description"] for c in fills)
    assert _rows() == {}                                                # nothing guessed


def test_i4_a_stored_member_set_needs_no_pin_for_a_second_fill(acct, broker, monkeypatch):
    _world(monkeypatch)
    _tp_half_fill(acct, broker)
    before = _rows()
    aex.reduce_symbol_weights(1, "ABC", 0.5, reason="tp_fill", detail="again", implicit={})
    assert _rows() == {("L", "ABC"): before[("L", "ABC")] * 0.5, ("L", "OTHER"): 50.0}


# ======================================================================== item 5
TARGETS = [T(60.0, 0.5), T(70.0, 0.5)]


def _tp1_filled(acct, broker, price):
    assert aps.save_protection(acct, "ABC", 45.0, TARGETS).ok
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP")
    aps.reconcile_account(acct)
    broker.prices["ABC"] = price


def _live_oco(b):
    out = []
    for r in b.complex.values():
        if all(m.status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED) for m in r["members"]):
            tp = [m for m in r["members"] if str(m.order_type.value) == "Limit"][0]
            out.append((int(tp.size), float(tp.price)))
    return out


def test_i5_the_filled_target_is_recorded_per_target(acct, broker):
    _tp1_filled(acct, broker, 62.0)
    flags = [bool(t.get("filled")) for t in aps.get_protection(1, "ABC").tp_targets]
    assert flags == [True, False]


def test_i5_price_62_keeps_the_whole_remainder_on_tp2(acct, broker):
    _tp1_filled(acct, broker, 62.0)
    assert aps.replace_protection(acct, "ABC").ok
    assert _live_oco(broker) == [(50, 70.0)]                            # NOT 25 (TP2 shrunk) + 25 stop-only
    assert _live_qty(broker) == 50


def test_i5_price_58_does_not_take_tp1_twice(acct, broker):
    _tp1_filled(acct, broker, 58.0)
    assert aps.replace_protection(acct, "ABC").ok
    assert all(price != 60.0 for _, price in _live_oco(broker))


def test_i5_slices_keep_the_original_target_index(acct, broker):
    _tp1_filled(acct, broker, 62.0)
    aps.replace_protection(acct, "ABC")
    live = [s for s in _slices() if s.state == SLICE_LIVE and s.kind == "OCO"]
    assert [s.target_index for s in live] == [1]


def test_i5_effective_targets_spreads_the_rest_over_the_remaining_shares():
    raw = [{"price": 60.0, "fraction": 0.25, "filled": True}, {"price": 70.0, "fraction": 0.25},
           {"price": 80.0, "fraction": 0.25}]
    eff = ap.effective_targets(raw, 50.0)
    assert [(i, round(t.fraction, 6)) for i, t in zip(eff.usable_index, eff.usable)] == [(1, round(1 / 3, 6)), (2, round(1 / 3, 6))]
    assert eff.filled == [0] and eff.reached == []


def test_i5_only_reached_but_unfilled_targets_fold_into_the_stop():
    raw = [{"price": 60.0, "fraction": 0.5, "filled": True}, {"price": 70.0, "fraction": 0.25},
           {"price": 80.0, "fraction": 0.25}]
    eff = ap.effective_targets(raw, 75.0)                               # 70 reached, 80 not
    assert eff.reached == [1] and eff.usable_index == [2]
    assert round(eff.usable[0].fraction, 6) == 0.5                      # 0.25 / (1 - 0.5): the stop keeps the rest


def test_i5_everything_filled_leaves_only_the_stop():
    eff = ap.effective_targets([{"price": 60.0, "fraction": 1.0, "filled": True}], 50.0)
    assert eff.usable == []


# ======================================================================== item 6
def _expected(v=7.0, pending=True):
    _set_protection(expected_qty=v, pending_replace=pending,
                    pending_replace_since=datetime.utcnow() if pending else None)


def test_i6_before_sale_records_the_expectation(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [])
    aps.before_sale(acct, "ABC", 100)
    assert aps.get_protection(1, "ABC").expected_qty == 0


@pytest.mark.parametrize("path", ["disable", "save", "prepare", "resize", "reconcile"])
def test_i6_a_stale_expectation_is_cleared_wherever_pending_changes(acct, broker, path):
    aps.save_protection(acct, "ABC", 45.0, [])
    _expected(7.0, pending=(path != "save" and path != "resize"))
    if path == "disable":
        aps.disable_protection(acct, "ABC")
    elif path == "save":
        aps.save_protection(acct, "ABC", 45.0, [])
    elif path == "prepare":
        aps.prepare_for_trade(acct, ["ABC"])
    elif path == "resize":
        p = aps.get_protection(1, "ABC")
        aps._resize_now(acct, p, "test resize")
    else:
        aps._reconcile_one(acct, aps.get_protection(1, "ABC"), aps.ReconcileReport(), (100.0, True), True)
    assert aps.get_protection(1, "ABC").expected_qty is None


def _sale(status, filled):
    add_instance(TradingOrder(account_id=1, symbol="ABC", quantity=100.0, filled_qty=filled,
                              side=OrderDirection.SELL, order_type=OrderType.MARKET, status=status,
                              open_type=OrderOpenType.MANUAL, broker_order_id="55"))


def test_i6_no_wait_when_the_sale_order_ended_unfilled(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)])
    aps.before_sale(acct, "ABC", 100)
    _sale(OrderStatus.REJECTED, 0.0)                                    # the sale never happened: position still 100
    aps.resume_protection(acct, ["ABC"])
    assert _live_qty(broker) == 100 and not aps.get_protection(1, "ABC").pending_replace


def test_i6_a_wait_is_logged_once_when_it_starts(acct, broker, activity):
    aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)])
    aps.before_sale(acct, "ABC", 100)
    _sale(OrderStatus.FILLED, 100.0)                                    # filled in the DB, broker read lags
    aps.resume_protection(acct, ["ABC"])
    aps.resume_protection(acct, ["ABC"])
    waits = [c for c in activity if c["data"].get("code") == "SALE_SETTLING"]
    assert len(waits) == 1 and aps.get_protection(1, "ABC").pending_replace


def test_i6_a_wait_that_ends_still_mismatched_alerts(acct, broker, activity):
    aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)])
    aps.before_sale(acct, "ABC", 100)
    _sale(OrderStatus.FILLED, 100.0)
    _set_protection(pending_replace_since=datetime.utcnow() - timedelta(seconds=aps.FILL_SETTLE_SECONDS + 60))
    aps.resume_protection(acct, ["ABC"])
    assert any(c["data"].get("code") == "SALE_UNSETTLED" for c in activity)


# ======================================================================== item 7
def test_i7_a_young_unknown_says_it_is_waiting_not_that_the_search_failed(acct, broker):
    broker.single_raise_on_place = TimeoutError("connect timeout")
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.single_raise_on_place = None
    aps.reconcile_account(acct)
    message = aps.get_protection(1, "ABC").alert_message
    assert "waiting" in message and "could not be completed" not in message


def test_i7_the_tag_search_pages_past_the_newest_fifty(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    oid = next(iter(broker.singles))
    for _ in range(120):                                               # 120 newer foreign orders push it to page 2
        broker.place_foreign_stop("ZZZ", 1, 5.0, "other")
    s = _slices()[0]
    found = acct.find_protective_orders_by_tag(s.external_tag, since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert [f[1] for f in found] == [oid]


def test_i7_a_search_that_cannot_look_back_far_enough_raises(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [])
    for _ in range(120):
        broker.place_foreign_stop("ZZZ", 1, 5.0, "other")
    acct._TAG_SEARCH_MAX_PAGES = 1
    with pytest.raises(RuntimeError):
        acct.find_protective_orders_by_tag("ba2prot:x", since=datetime.now(timezone.utc) - timedelta(days=30))


def test_i5_the_dialog_does_not_offer_a_filled_target_again():
    from ba2_trade_platform.ui.pages.allocator_protection_dialog import initial_rows
    p = SimpleNamespace(enabled=True, tp_targets=[{"price": 60.0, "fraction": 0.5, "filled": True},
                                                  {"price": 70.0, "fraction": 0.5}])
    assert [r["price"] for r in initial_rows(p)] == [70.0]          # the taken one is in the re-arm list


def test_i5_new_shares_added_to_a_resting_protection_get_the_remaining_plan(acct, broker, monkeypatch):
    """Round 4 item 1: the filled mark is honoured EVERYWHERE; new shares get what is left of the plan."""
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    _tp1_filled(acct, broker, 50.0)
    broker.positions["ABC"] = broker.positions["ABC"] + Decimal(10)
    aps.reconcile_account(acct)
    assert sorted(_live_oco(broker)) == [(10, 70.0), (50, 70.0)]
