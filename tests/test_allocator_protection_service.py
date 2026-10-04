"""Lifecycle of the allocator TP/SL protection against the FAKE TastyTrade complex-order API.

Real ``TastyTradeAccount`` methods, real DB (conftest's in-memory SQLite), a fake broker. The
scenarios the operator cares about: place, partial TP fill -> hold, SL fill -> hold, expiry or a
cancel on the broker's site -> loud UNPROTECTED alert, rebalance cancel/re-place, held symbols
skipped, an unconfirmed cancel blocking a trade. Nothing contacts a broker.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlmodel import select
from tastytrade.order import OrderStatus as TTOrderStatus
from tastytrade.utils import TastytradeError

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtection, AllocatorProtectionOrder, SLICE_CANCELLED_BY_US, SLICE_CANCELLING,
    SLICE_FILLED_SL, SLICE_FILLED_TP, SLICE_LIVE, SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED,
    SLICE_UNKNOWN,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    ActivityLogSeverity, OrderDirection, OrderOpenType, OrderStatus, OrderType,
)
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity

T = ap.TpTarget
THIRDS = [T(60, 1 / 3), T(65, 1 / 3), T(70, 1 / 3)]


@pytest.fixture
def broker():
    b = FakeTastyBroker()
    b.positions["ABC"] = Decimal(10)
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


def _codes(activity):
    return [c["data"].get("code") for c in activity]


def _save(acct, targets=None, sl=45.0, symbol="ABC"):
    return aps.save_protection(acct, symbol, sl, targets or THIRDS)


def _slices(symbol="ABC", account_id=1):
    p = aps.get_protection(account_id, symbol)
    return aps.get_slices(p.id)


def _status(acct, broker, symbol="ABC"):
    p = aps.get_protection(acct.id, symbol)
    return aps.status_for(p, float(broker.positions.get(symbol, 0)))


# ============================================================================ save / place

def test_save_places_three_ocos_covering_the_whole_position(acct, broker, activity):
    result = _save(acct)
    assert result.ok, result
    slices = _slices()
    assert [s.quantity for s in slices] == [4, 3, 3] or sorted(s.quantity for s in slices) == [3, 3, 4]
    assert sum(s.quantity for s in slices) == 10
    assert {s.state for s in slices} == {SLICE_LIVE}
    assert all(s.complex_order_id and s.tp_order_id and s.sl_order_id for s in slices)
    assert [s.tp_price for s in slices] == [60.0, 65.0, 70.0]
    assert {s.sl_price for s in slices} == {45.0}                       # one stop, split across OCOs
    live_calls = [o for dry, o in broker.place_calls if not dry]
    assert len(live_calls) == 3
    assert [dry for dry, _ in broker.place_calls] == [True, False] * 3   # dry run before each
    p = aps.get_protection(1, "ABC")
    assert p.enabled and p.protected_quantity == 10 and p.held_at is None and p.alert_code is None
    assert _status(acct, broker).code == ap.STATUS_PROTECTED
    assert "PLACED" in _codes(activity)


def test_orders_are_tagged_for_traceability(acct, broker):
    _save(acct)
    p = aps.get_protection(1, "ABC")
    tags = [s.external_tag for s in _slices()]
    assert tags == [f"ba2prot:{p.id}:{i}" for i in range(3)]


def test_save_validation_failure_writes_nothing_and_sends_nothing(acct, broker):
    result = aps.save_protection(acct, "ABC", 55.0, THIRDS)       # SL above the price
    assert not result.ok and any("BELOW the current price" in e for e in result.errors)
    assert broker.place_calls == []
    assert aps.get_protection(1, "ABC") is None


def test_save_on_a_short_position_is_refused(acct, broker):
    broker.positions["ABC"] = Decimal(-10)
    result = _save(acct)
    assert not result.ok and "short" in result.message
    assert broker.place_calls == []


def test_save_with_an_unreadable_position_is_refused_not_assumed(acct, broker):
    broker.positions_fail = True
    acct.get_positions = lambda: None
    result = _save(acct)
    assert not result.ok and "Cannot validate" in result.message
    assert broker.place_calls == []


def test_a_fractional_position_protects_only_the_whole_shares(acct, broker):
    broker.positions["ABC"] = Decimal("10.4")
    result = _save(acct, [T(60, 0.5), T(65, 0.5)])
    assert result.ok
    assert sum(s.quantity for s in _slices()) == 10
    assert any("fractional" in n for n in result.notes)
    status = _status(acct, broker)
    assert status.code == ap.STATUS_PROTECTED and "fractional" in status.tooltip


def test_a_sub_one_share_position_cannot_be_protected(acct, broker):
    broker.positions["ABC"] = Decimal("0.6")
    result = _save(acct)
    assert not result.ok and any("no whole share" in e for e in result.errors)
    assert broker.place_calls == []


def test_fewer_shares_than_targets_places_what_it_can_and_says_so(acct, broker):
    broker.positions["ABC"] = Decimal(2)
    result = _save(acct)
    assert result.ok and result.notes
    assert sum(s.quantity for s in _slices()) == 2 and len(_slices()) == 2


def test_a_refused_second_slice_keeps_the_first_and_alerts_loudly(acct, broker, activity):
    original = broker.place_complex_order
    state = {"live": 0}

    async def flaky(session, order, dry_run=True):
        if not dry_run:
            state["live"] += 1
            if state["live"] == 2:
                raise TastytradeError("insufficient_quantity: refused")
        return await original(session, order, dry_run)
    broker.place_complex_order = flaky
    result = _save(acct)
    assert not result.ok and any("insufficient_quantity" in e for e in result.errors)
    slices = _slices()
    assert [s.state for s in slices].count(SLICE_LIVE) == 2          # the third was still tried
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_PLACEMENT_REFUSED and "insufficient_quantity" in p.last_error
    status = _status(acct, broker)
    assert status.code == ap.STATUS_UNPROTECTED and status.alarm
    assert ap.CODE_PLACEMENT_REFUSED in _codes(activity)
    assert any(c["severity"] == ActivityLogSeverity.FAILURE for c in activity)


def test_a_refused_placement_is_never_silent_and_leaves_the_symbol_unprotected(acct, broker, activity):
    broker.raise_on_place = TastytradeError("tif_invalid: no")
    result = _save(acct)
    assert not result.ok
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_PLACEMENT_REFUSED
    assert _status(acct, broker).code == ap.STATUS_UNPROTECTED
    assert aps.open_alerts(1) and aps.open_alerts(1)[0].symbol == "ABC"


def test_saving_again_replaces_the_existing_orders(acct, broker):
    _save(acct)
    first = [s.complex_order_id for s in _slices()]
    result = _save(acct, [T(61, 0.5), T(66, 0.5)], sl=44.0)
    assert result.ok
    assert sorted(broker.delete_calls) == sorted(first)             # old ones cancelled (confirmed)
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert sum(s.quantity for s in live) == 10 and len(live) == 2
    assert {s.sl_price for s in live} == {44.0}


def test_saving_is_refused_while_a_rebalance_replacement_is_pending(acct, broker):
    _save(acct)
    p = aps.get_protection(1, "ABC")
    p.pending_replace = True
    update_instance(p)
    result = _save(acct)
    assert not result.ok and "being re-placed" in result.message


# ============================================================================ reconcile: fills -> hold

def test_a_full_tp_fill_holds_the_symbol_and_the_other_slices_keep_resting(acct, broker, activity):
    _save(acct)
    first = _slices()[0]
    broker.fill(first.complex_order_id, "TP")
    report = aps.reconcile_account(acct)
    assert report.new_holds == ["ABC"]
    p = aps.get_protection(1, "ABC")
    assert p.held_at is not None and "take-profit filled" in p.held_reason
    states = {s.slice_index: s.state for s in _slices()}
    assert states[0] == SLICE_FILLED_TP and states[1] == states[2] == SLICE_LIVE
    status = _status(acct, broker)
    assert status.code == ap.STATUS_HELD and "Held after TP/SL fill on" in status.label
    assert "still covered" in status.tooltip
    assert ap.CODE_HELD_FILL in _codes(activity)
    assert "ABC" in aps.held_protections(1)


def test_a_partial_tp_fill_holds_and_keeps_the_remainder_reserved(acct, broker):
    _save(acct)
    s0 = _slices()[0]
    broker.fill(s0.complex_order_id, "TP", qty=1)
    aps.reconcile_account(acct)
    p = aps.get_protection(1, "ABC")
    assert p.held_at is not None
    s0 = _slices()[0]
    assert s0.state == SLICE_FILLED_TP and s0.filled_qty == 1 and s0.closed_at is None
    assert aps.covered_quantity(_slices()) == (s0.quantity - 1) + sum(
        s.quantity for s in _slices()[1:])


def test_an_sl_fill_holds_the_symbol(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "SL")
    aps.reconcile_account(acct)
    p = aps.get_protection(1, "ABC")
    assert p.held_at is not None and "stop-loss filled" in p.held_reason
    assert _slices()[0].state == SLICE_FILLED_SL and _slices()[0].fill_price == 45.0


def test_the_hold_is_set_once_and_the_first_reason_is_kept(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    reason = aps.get_protection(1, "ABC").held_reason
    broker.fill(_slices()[1].complex_order_id, "TP")
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").held_reason == reason


def test_a_held_symbol_is_still_reconciled_and_a_lost_remaining_slice_alarms(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    broker.expire(_slices()[1].complex_order_id)
    aps.reconcile_account(acct)
    status = _status(acct, broker)
    assert status.code == ap.STATUS_HELD and status.alarm


def test_reconcile_with_nothing_changed_is_a_no_op(acct, broker, activity):
    _save(acct)
    before = len(activity)
    report = aps.reconcile_account(acct)
    assert report.new_holds == [] and report.alarms == [] and report.failed_symbols == []
    assert len(activity) == before
    assert _status(acct, broker).code == ap.STATUS_PROTECTED


# ============================================================================ reconcile: lost orders

def test_an_expired_order_alarms_loudly_and_is_not_silently_replaced(acct, broker, activity):
    _save(acct)
    placed_before = len(broker.complex)
    broker.expire(_slices()[0].complex_order_id)
    report = aps.reconcile_account(acct)
    assert "ABC" in report.alarms
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_LOST_EXPIRED and "UNPROTECTED" in p.alert_message
    assert _slices()[0].state == SLICE_LOST_EXPIRED
    status = _status(acct, broker)
    assert status.code == ap.STATUS_UNPROTECTED and status.alarm
    assert len(broker.complex) == placed_before                  # nothing placed behind the operator's back
    assert any(c["severity"] == ActivityLogSeverity.FAILURE for c in activity)


def test_the_same_alert_is_not_written_to_the_activity_log_every_refresh(acct, broker, activity):
    _save(acct)
    broker.expire(_slices()[0].complex_order_id)
    aps.reconcile_account(acct)
    count = _codes(activity).count(ap.CODE_LOST_EXPIRED)
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)
    assert _codes(activity).count(ap.CODE_LOST_EXPIRED) == count == 1
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_EXPIRED     # still shown


def test_a_cancel_on_the_brokers_site_is_detected(acct, broker):
    _save(acct)
    broker.external_cancel(_slices()[1].complex_order_id)
    aps.reconcile_account(acct)
    assert _slices()[1].state == SLICE_LOST_CANCELLED
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED
    assert _status(acct, broker).code == ap.STATUS_UNPROTECTED


def test_an_unreadable_state_is_reported_and_changes_nothing(acct, broker, activity):
    _save(acct)
    broker.raise_on_read = TastytradeError("api down")
    report = aps.reconcile_account(acct)
    assert report.failed_symbols == ["ABC"]
    assert {s.state for s in _slices()} == {SLICE_LIVE}                # NOT flipped to lost or filled
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_RECONCILE_FETCH_FAILED
    assert any(c["severity"] == ActivityLogSeverity.WARNING for c in activity)
    broker.raise_on_read = None
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code is None            # recovered


def test_a_mixed_unreadable_member_state_is_an_alarm_not_live(acct, broker):
    _save(acct)
    cid = _slices()[0].complex_order_id
    broker.set_member_status(cid, "SL", TTOrderStatus.CANCELLED)
    aps.reconcile_account(acct)
    assert _slices()[0].state == SLICE_UNKNOWN
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_UNKNOWN_STATE
    assert _status(acct, broker).code == ap.STATUS_UNPROTECTED


def test_a_growing_position_is_flagged_partial_not_ignored(acct, broker):
    _save(acct)
    broker.positions["ABC"] = Decimal(14)
    aps.reconcile_account(acct)
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_QUANTITY_MISMATCH
    assert _status(acct, broker).code == ap.STATUS_PARTIAL
    broker.positions["ABC"] = Decimal(10)
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code is None


def test_a_position_sold_elsewhere_while_protected_is_flagged(acct, broker):
    _save(acct)
    broker.positions["ABC"] = Decimal(4)
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_QUANTITY_MISMATCH


def test_gtc_expiry_is_warned_once(acct, broker, activity):
    _save(acct)
    soon = (datetime.utcnow() + timedelta(days=3)).date()
    for m in broker.complex[_slices()[0].complex_order_id]["members"]:
        m.gtc_date = soon                                             # the broker says: ends in 3 days
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)
    assert _codes(activity).count(ap.CODE_GTC_EXPIRING) == 1
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_GTC_EXPIRING
    assert aps.open_alerts(1) == []                                 # a warning, not the red banner


def test_a_missing_broker_gtc_date_is_assumed_and_flagged(acct, broker, monkeypatch):
    real = acct.place_protective_oco

    def no_gtc(**kw):
        r = real(**kw)
        r.gtc_date = None
        return r
    acct.place_protective_oco = no_gtc
    _save(acct)
    s = _slices()[0]
    assert s.gtc_date_assumed is True
    assert (s.gtc_date - datetime.utcnow()).days in (ap.ASSUMED_GTC_LIFETIME_DAYS - 1,
                                                     ap.ASSUMED_GTC_LIFETIME_DAYS)


def test_a_stale_placing_row_is_an_alarm(acct, broker):
    p = aps._save(AllocatorProtection(account_id=1, symbol="ABC", enabled=True, sl_price=45.0,
                                      tp_targets=[T(60, 1.0).to_dict()]))
    aps._save(AllocatorProtectionOrder(protection_id=p.id, quantity=10, tp_price=60, sl_price=45,
                                       state="PLACING",
                                       placed_at=datetime.utcnow() - timedelta(minutes=5)))
    aps.reconcile_account(acct)
    assert aps.get_slices(p.id)[0].state == SLICE_UNKNOWN
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_UNKNOWN_STATE


# ============================================================================ disable / delete

def test_disable_cancels_everything_confirmed_and_is_not_an_alarm(acct, broker):
    _save(acct)
    ids = [s.complex_order_id for s in _slices()]
    result = aps.disable_protection(acct, "ABC")
    assert result.ok
    assert sorted(broker.delete_calls) == sorted(ids)
    assert {s.state for s in _slices()} == {SLICE_CANCELLED_BY_US}
    p = aps.get_protection(1, "ABC")
    assert not p.enabled and p.alert_code is None
    assert _status(acct, broker).code == ap.STATUS_OFF
    aps.reconcile_account(acct)                                    # and stays quiet
    assert aps.get_protection(1, "ABC").alert_code is None


def test_disable_with_an_unconfirmed_cancel_says_so_loudly(acct, broker):
    _save(acct)
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    result = aps.disable_protection(acct, "ABC")
    assert not result.ok and "NOT confirmed" in result.message
    assert {s.state for s in _slices()} == {SLICE_CANCELLING}
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_CANCEL_UNCONFIRMED


def test_an_unconfirmed_cancel_that_later_lands_resolves_without_an_alarm(acct, broker):
    _save(acct)
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    aps.disable_protection(acct, "ABC")
    broker.never_confirm_cancel = False
    broker.cancel_polls = 0
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)                                       # the broker confirms on a later read
    assert {s.state for s in _slices()} == {SLICE_CANCELLED_BY_US}
    assert aps.get_protection(1, "ABC").alert_code is None


def test_disable_of_an_unknown_symbol_is_a_no_op(acct):
    assert aps.disable_protection(acct, "NOPE").ok


def test_delete_removes_the_configuration_after_a_confirmed_cancel(acct, broker):
    _save(acct)
    assert aps.delete_protection(acct, "ABC").ok
    assert aps.get_protection(1, "ABC") is None
    with get_db() as session:
        assert len(session.exec(select(AllocatorProtectionOrder)).all()) == 0


def test_delete_with_an_unconfirmed_cancel_keeps_the_rows(acct, broker):
    _save(acct)
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    assert not aps.delete_protection(acct, "ABC").ok
    assert aps.get_protection(1, "ABC") is not None


# ============================================================================ re-enable

def test_reenable_after_a_tp_fill_drops_the_consumed_target_and_keeps_the_rest(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    result = aps.reenable_symbol(acct, "ABC")
    assert result.ok
    p = aps.get_protection(1, "ABC")
    assert p.held_at is None and p.enabled
    assert [t["price"] for t in p.tp_targets] == [65, 70]
    assert sum(t["fraction"] for t in p.tp_targets) == pytest.approx(1.0)
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert sorted(s.target_index for s in live) == [0, 1]           # remapped onto the shortened list
    aps.reconcile_account(acct)                                     # the reduced position still matches
    assert aps.get_protection(1, "ABC").alert_code is None
    assert _status(acct, broker).code == ap.STATUS_PROTECTED


def test_reenable_after_an_sl_fill_switches_protection_off_and_cancels_leftovers(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "SL")
    aps.reconcile_account(acct)
    result = aps.reenable_symbol(acct, "ABC")
    assert result.ok and "stop had fired" in result.message
    p = aps.get_protection(1, "ABC")
    assert p.held_at is None and not p.enabled
    assert all(s.state != SLICE_LIVE for s in _slices())            # leftovers cancelled
    assert _status(acct, broker).code == ap.STATUS_OFF


def test_reenable_when_every_target_was_hit_switches_off(acct, broker):
    _save(acct, [T(60, 1.0)])
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    result = aps.reenable_symbol(acct, "ABC")
    assert result.ok and "Every take-profit target was hit" in result.message
    assert not aps.get_protection(1, "ABC").enabled


def test_reenable_of_a_symbol_that_is_not_held_is_refused(acct, broker):
    _save(acct)
    assert not aps.reenable_symbol(acct, "ABC").ok
    assert not aps.reenable_symbol(acct, "NOPE").ok


def test_saving_is_refused_while_held(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    result = _save(acct)
    assert not result.ok and "held" in result.message


# ============================================================================ replace

def test_replace_cancels_and_places_fresh_orders(acct, broker):
    _save(acct)
    old = [s.complex_order_id for s in _slices()]
    result = aps.replace_protection(acct, "ABC")
    assert result.ok
    assert sorted(broker.delete_calls) == sorted(old)
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert sum(s.quantity for s in live) == 10 and not set(old) & {s.complex_order_id for s in live}


def test_replace_repairs_a_lost_slice_and_clears_the_alarm(acct, broker):
    _save(acct)
    broker.expire(_slices()[0].complex_order_id)
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_EXPIRED
    assert aps.replace_protection(acct, "ABC").ok
    assert aps.get_protection(1, "ABC").alert_code is None
    assert _status(acct, broker).code == ap.STATUS_PROTECTED


def test_replace_refuses_a_price_that_moved_through_the_stop(acct, broker):
    _save(acct)
    broker.prices["ABC"] = 44.0                                      # below the 45 stop
    result = aps.replace_protection(acct, "ABC")
    assert not result.ok and any("BELOW the current price" in e for e in result.errors)
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_PLACEMENT_REFUSED
    assert _status(acct, broker).code == ap.STATUS_UNPROTECTED


# ============================================================================ the allocator boundary

def test_prepare_cancels_confirmed_and_marks_the_replacement_pending(acct, broker):
    _save(acct)
    prep = aps.prepare_for_trade(acct, ["ABC", "OTHER"])
    assert prep.pending == ["ABC"] and not prep.held and not prep.blocked
    assert len(broker.delete_calls) == 3
    p = aps.get_protection(1, "ABC")
    assert p.pending_replace and p.pending_replace_since is not None
    assert aps.covered_quantity(_slices()) == 0
    assert _status(acct, broker).code == ap.STATUS_REPLACING


def test_prepare_sets_pending_before_the_first_cancel(acct, broker):
    """A crash between the cancel and the re-placement must leave a durable marker."""
    _save(acct)
    seen = {}
    original = acct.cancel_complex_order

    def spy(cid):
        seen.setdefault("pending", aps.get_protection(1, "ABC").pending_replace)
        return original(cid)
    acct.cancel_complex_order = spy
    aps.prepare_for_trade(acct, ["ABC"])
    assert seen["pending"] is True


def test_prepare_discovers_a_fill_and_reports_the_symbol_held(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "SL")                 # not reconciled yet
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.held and prep.pending == []
    assert aps.get_protection(1, "ABC").held_at is not None


def test_prepare_reports_a_held_symbol_without_touching_its_orders(acct, broker):
    _save(acct)
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    deletes = len(broker.delete_calls)
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.held and len(broker.delete_calls) == deletes


def test_prepare_blocks_the_row_when_a_cancel_cannot_be_confirmed(acct, broker):
    _save(acct)
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.blocked and "reserved" in prep.blocked["ABC"] and prep.pending == []
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_CANCEL_UNCONFIRMED


def test_prepare_leaves_a_symbol_with_protection_off_alone(acct, broker):
    _save(acct)
    aps.disable_protection(acct, "ABC")
    deletes = len(broker.delete_calls)
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert prep.pending == [] and len(broker.delete_calls) == deletes


def test_an_armed_symbol_with_no_position_is_marked_pending_so_the_run_arms_it(acct, broker):
    broker.positions["ABC"] = Decimal(0)
    p = aps._save(AllocatorProtection(account_id=1, symbol="ABC", enabled=True, sl_price=45.0,
                                      tp_targets=[t.to_dict() for t in THIRDS]))
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert prep.pending == ["ABC"]
    broker.positions["ABC"] = Decimal(9)                              # the run bought 9
    assert aps.resume_protection(acct, ["ABC"], working_symbols=set()) == ["ABC"]
    assert sum(s.quantity for s in _slices()) == 9


def test_resume_replaces_the_orders_for_the_new_position_size(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(5)                             # the rebalance sold 5
    resumed = aps.resume_protection(acct, ["ABC"], working_symbols=set())
    assert resumed == ["ABC"]
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert sum(s.quantity for s in live) == 5
    p = aps.get_protection(1, "ABC")
    assert not p.pending_replace and p.alert_code is None
    assert _status(acct, broker).code == ap.STATUS_PROTECTED


def test_resume_waits_while_an_order_is_still_working(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    assert aps.resume_protection(acct, ["ABC"], working_symbols={"ABC"}) == []
    assert aps.get_protection(1, "ABC").pending_replace
    assert _status(acct, broker).code == ap.STATUS_REPLACING


def test_resume_after_selling_everything_just_waits_for_the_next_buy(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(0)
    assert aps.resume_protection(acct, ["ABC"], working_symbols=set()) == []
    p = aps.get_protection(1, "ABC")
    assert not p.pending_replace and p.alert_code is None and p.enabled
    assert _status(acct, broker).code == ap.STATUS_NO_POSITION


def test_a_failed_replacement_alerts_and_stops_retrying(acct, broker, activity):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.raise_on_place = TastytradeError("rejected: nope")
    broker.positions["ABC"] = Decimal(5)
    assert aps.resume_protection(acct, ["ABC"], working_symbols=set()) == []
    p = aps.get_protection(1, "ABC")
    assert not p.pending_replace and p.alert_code == ap.CODE_REPLACE_FAILED
    assert _status(acct, broker).code == ap.STATUS_UNPROTECTED
    assert ap.CODE_REPLACE_FAILED in _codes(activity)


def test_resume_never_raises_even_when_the_broker_blows_up(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])

    def boom():
        raise RuntimeError("positions exploded")
    acct.get_positions = boom
    assert aps.resume_protection(acct, ["ABC"], working_symbols=set()) == []


def test_resume_refuses_a_price_that_moved_through_the_stop_while_orders_were_off(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.prices["ABC"] = 40.0
    assert aps.resume_protection(acct, ["ABC"], working_symbols=set()) == []
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_REPLACE_FAILED and "BELOW the current price" in p.alert_message


def test_background_reconcile_completes_a_pending_replacement_when_no_run_is_active(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(6)
    report = aps.reconcile_account(acct)
    assert report.resumed == ["ABC"]
    assert sum(s.quantity for s in _slices() if s.state == SLICE_LIVE) == 6


def test_background_reconcile_leaves_the_replacement_to_a_run_in_flight(acct, broker):
    from ba2_trade_platform.core.portfolio_allocation_service import _submission_lock
    import threading
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(6)
    held = threading.Event()
    release = threading.Event()

    def hold_the_run_lock():
        with _submission_lock(acct.id):
            held.set()
            release.wait(5)
    t = threading.Thread(target=hold_the_run_lock)
    t.start()
    held.wait(5)
    try:
        report = aps.reconcile_account(acct)
    finally:
        release.set()
        t.join()
    assert report.resumed == []
    assert aps.get_protection(1, "ABC").pending_replace


def test_background_reconcile_does_not_resume_while_an_order_is_working(acct, broker):
    _save(acct)
    aps.prepare_for_trade(acct, ["ABC"])
    add_instance(TradingOrder(account_id=1, symbol="ABC", quantity=4, side=OrderDirection.SELL,
                              order_type=OrderType.MARKET, good_for="day",
                              status=OrderStatus.ACCEPTED, open_type=OrderOpenType.MANUAL))
    broker.positions["ABC"] = Decimal(6)
    assert aps.reconcile_account(acct).resumed == []
    assert aps.get_protection(1, "ABC").pending_replace


# ============================================================================ misc

def test_an_account_without_the_capability_is_never_touched():
    class Other:
        id = 5
    assert aps.reconcile_account(Other()).checked == 0
    assert aps.prepare_for_trade(Other(), ["ABC"]).pending == []
    assert aps.resume_protection(Other(), ["ABC"]) == []
    assert aps.has_protection(Other()) is False


def test_a_mock_account_does_not_pass_the_capability_check():
    from unittest.mock import MagicMock
    assert aps.has_protection(MagicMock()) is False


def test_protections_are_scoped_per_account(acct, broker):
    _save(acct)
    assert aps.get_protection(2, "ABC") is None
    assert aps.list_protections(2) == []
    assert aps.held_protections(2) == {}


def test_status_for_a_symbol_without_a_row_is_off():
    s = aps.status_for(None, 5.0)
    assert s.code == ap.STATUS_OFF


def test_one_symbols_reconcile_failure_does_not_cost_the_others(acct, broker, monkeypatch):
    broker.positions["XYZ"] = Decimal(10)
    broker.prices["XYZ"] = 20.0
    _save(acct)
    aps.save_protection(acct, "XYZ", 15.0, [T(30, 1.0)])
    real = aps._reconcile_one

    def flaky(account, p, report, position):
        if p.symbol == "ABC":
            raise RuntimeError("boom")
        return real(account, p, report, position)
    monkeypatch.setattr(aps, "_reconcile_one", flaky)
    broker.expire(_slices("XYZ")[0].complex_order_id)
    report = aps.reconcile_account(acct)
    assert "ABC" in report.failed_symbols
    assert aps.get_protection(1, "XYZ").alert_code == ap.CODE_LOST_EXPIRED


def test_reconcile_never_raises_when_the_position_read_fails(acct, broker):
    _save(acct)

    def boom():
        raise RuntimeError("down")
    acct.get_positions = boom
    assert aps.reconcile_account(acct).checked == 3        # slices are still read; no raise
