"""Pure helpers of the allocator TP/SL protection: validation, ticks, slicing, classification."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core.allocator_protection_models import (
    SLICE_CANCELLED_BY_US, SLICE_CANCELLING, SLICE_FILLED_SL, SLICE_FILLED_TP, SLICE_LIVE,
    SLICE_LOST_CANCELLED, SLICE_LOST_EXPIRED, SLICE_LOST_REJECTED, SLICE_UNKNOWN,
)

T = ap.TpTarget


def _targets(*pairs):
    return [T(price=p, fraction=f) for p, f in pairs]


# --------------------------------------------------------------------- whole shares

def test_whole_shares_floors_and_never_rounds_up():
    assert ap.whole_shares(10.4) == 10
    assert ap.whole_shares(10.9999) == 10
    assert ap.whole_shares(0.99) == 0


def test_whole_shares_survives_a_float_artefact():
    assert ap.whole_shares(2.9999999999999) == 3


def test_whole_shares_refuses_an_unknown_position():
    with pytest.raises(ValueError):
        ap.whole_shares(None)


def test_fractional_remainder():
    assert ap.fractional_remainder(10.4) == pytest.approx(0.4)
    assert ap.fractional_remainder(10.0) == 0.0


# --------------------------------------------------------------------- ticks

def test_default_tick_by_price():
    assert ap.default_tick(55.0) == Decimal("0.01")
    assert ap.default_tick(0.5) == Decimal("0.0001")


def test_tick_for_price_reads_the_broker_table_with_thresholds():
    sizes = [SimpleNamespace(value=Decimal("0.0001"), threshold=Decimal("1")),
             SimpleNamespace(value=Decimal("0.01"), threshold=None)]
    assert ap.tick_for_price(0.5, sizes) == Decimal("0.0001")
    assert ap.tick_for_price(1.0, sizes) == Decimal("0.01")
    assert ap.tick_for_price(75, sizes) == Decimal("0.01")


def test_tick_for_price_falls_back_when_the_table_is_missing():
    assert ap.tick_for_price(20, None) == Decimal("0.01")
    assert ap.tick_for_price(20, []) == Decimal("0.01")


def test_round_price_nearest_and_down():
    tick = Decimal("0.01")
    assert ap.round_price_to_tick(61.504, tick, "nearest") == 61.5
    assert ap.round_price_to_tick(61.506, tick, "nearest") == 61.51
    assert ap.round_price_to_tick(55.009, tick, "down") == 55.0   # SL never nearer the market
    assert ap.round_price_to_tick(55.0, tick, "down") == 55.0


def test_round_price_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        ap.round_price_to_tick(1.0, Decimal("0.01"), "up")


# --------------------------------------------------------------------- slicing

def test_split_quantity_three_thirds_of_ten():
    q = ap.split_quantity(10, [1 / 3, 1 / 3, 1 / 3])
    assert sum(q) == 10 and sorted(q) == [3, 3, 4]


@pytest.mark.parametrize("shares", range(0, 40))
def test_split_quantity_always_sums_to_the_position(shares):
    for fractions in ([1.0], [0.5, 0.5], [0.2, 0.3, 0.5], [1 / 3] * 3, [0.1] * 10):
        assert sum(ap.split_quantity(shares, fractions)) == shares


def test_split_quantity_is_deterministic_on_ties():
    assert ap.split_quantity(2, [0.5, 0.5]) == [1, 1]
    assert ap.split_quantity(1, [0.5, 0.5]) == [1, 0]


def test_split_quantity_rejects_nonsense():
    with pytest.raises(ValueError):
        ap.split_quantity(-1, [1.0])
    with pytest.raises(ValueError):
        ap.split_quantity(5, [])


def test_plan_slices_three_targets_ten_shares():
    plans, notes = ap.plan_slices(shares=10, targets=_targets((60, 1 / 3), (65, 1 / 3), (70, 1 / 3)),
                                  sl_price=45.004, last_price=50.0)
    assert sum(p.quantity for p in plans) == 10
    assert [p.tp_price for p in plans] == [60.0, 65.0, 70.0]
    assert {p.sl_price for p in plans} == {45.0}          # rounded DOWN, same stop on every OCO
    assert notes == []


def test_plan_slices_drops_targets_without_a_share_and_resplits():
    plans, notes = ap.plan_slices(shares=2, targets=_targets((60, 0.5), (65, 0.25), (70, 0.25)),
                                  sl_price=45, last_price=50)
    assert sum(p.quantity for p in plans) == 2
    assert len(plans) == 2 and len(notes) == 1 and "gets 0 shares" in notes[0]


def test_plan_slices_one_share_one_oco():
    plans, notes = ap.plan_slices(shares=1, targets=_targets((60, 0.5), (65, 0.5)),
                                  sl_price=45, last_price=50)
    assert [p.quantity for p in plans] == [1] and len(notes) == 1


def test_plan_slices_no_shares_places_nothing():
    plans, notes = ap.plan_slices(shares=0, targets=_targets((60, 1.0)), sl_price=45, last_price=50)
    assert plans == [] and notes


def test_plan_slices_orders_by_price_and_keeps_the_target_index():
    plans, _ = ap.plan_slices(shares=6, targets=_targets((70, 0.5), (60, 0.5)),
                              sl_price=45, last_price=50)
    assert [(p.target_index, p.tp_price) for p in plans] == [(1, 60.0), (0, 70.0)]


def test_remaining_targets_renormalises():
    left = ap.remaining_targets(_targets((60, 0.25), (65, 0.25), (70, 0.5)), [0])
    assert [t.price for t in left] == [65, 70]
    assert sum(t.fraction for t in left) == pytest.approx(1.0)
    assert left[0].fraction == pytest.approx(1 / 3)


def test_remaining_targets_all_consumed_raises():
    with pytest.raises(ValueError):
        ap.remaining_targets(_targets((60, 1.0)), [0])


# --------------------------------------------------------------------- validation

def _valid(**over):
    kw = dict(sl_price=45.0, targets=_targets((60, 0.5), (65, 0.5)), last_price=50.0,
              position_quantity=10.0)
    kw.update(over)
    return ap.validate_protection(**kw)


def test_a_good_request_has_no_errors():
    assert _valid() == []


def test_sl_must_be_below_the_price():
    assert any("BELOW the current price" in e for e in _valid(sl_price=50.0))
    assert any("BELOW the current price" in e for e in _valid(sl_price=51.0))


def test_sl_must_be_positive():
    assert any("greater than 0" in e for e in _valid(sl_price=0))
    assert any("greater than 0" in e for e in _valid(sl_price=None))


def test_tp_must_be_above_the_price():
    errors = _valid(targets=_targets((50.0, 0.5), (65, 0.5)))
    assert any("ABOVE the current price" in e for e in errors)


def test_fractions_must_total_one():
    assert any("must total 100%" in e for e in _valid(targets=_targets((60, 0.5), (65, 0.4))))
    assert any("must total 100%" in e for e in _valid(targets=_targets((60, 0.6), (65, 0.6))))


def test_a_fraction_must_be_positive():
    assert any("above 0%" in e for e in _valid(targets=_targets((60, 0.0), (65, 1.0))))


def test_distinct_tp_prices_after_tick_rounding():
    errors = _valid(targets=_targets((60.001, 0.5), (60.004, 0.5)))
    assert any("same price once rounded" in e for e in errors)


def test_at_least_one_target():
    assert any("at least one take-profit" in e for e in _valid(targets=[]))


def test_unknown_price_or_position_is_an_error_not_a_default():
    assert any("current price is unknown" in e for e in _valid(last_price=None))
    assert any("position is unknown" in e for e in _valid(position_quantity=None))


def test_a_position_below_one_share_cannot_be_protected():
    errors = _valid(position_quantity=0.6)
    assert any("no whole share to protect" in e for e in errors)


def test_a_fractional_position_is_valid_for_its_whole_part():
    assert _valid(position_quantity=10.4) == []


# --------------------------------------------------------------------- preview

def test_preview_lists_orders_and_notes_the_fractional_remainder():
    lines = ap.preview_orders(shares=10, position_quantity=10.4,
                              targets=_targets((60, 0.5), (65, 0.5)), sl_price=45, last_price=50)
    assert lines[0].startswith("OCO 1: sell 5 sh") and "limit 60" in lines[0] and "stop 45" in lines[0]
    assert any("fractional" in l and "UNPROTECTED" in l for l in lines)


# --------------------------------------------------------------------- classification

def _member(kind, status, *, fills=(), gtc=date(2027, 1, 3), mid=1):
    from tastytrade.order import Leg, FillInfo, OrderAction, InstrumentType
    leg = SimpleNamespace(fills=[SimpleNamespace(quantity=Decimal(q), fill_price=Decimal(str(px)))
                                 for q, px in fills])
    return SimpleNamespace(id=mid, status=SimpleNamespace(value=status),
                           order_type=SimpleNamespace(value="Limit" if kind == "TP" else "Stop"),
                           legs=[leg], gtc_date=gtc)


def _cx(*members):
    return SimpleNamespace(orders=list(members))


def _classify(*members, qty=5, ours=False):
    return ap.classify_complex_order(_cx(*members), slice_quantity=qty, we_requested_cancel=ours)


def test_both_members_live_is_live():
    obs = _classify(_member("TP", "Live", mid=1), _member("SL", "Live", mid=2))
    assert obs.state == SLICE_LIVE and obs.remaining_live_qty == 5
    assert (obs.tp_order_id, obs.sl_order_id) == (1, 2) and obs.gtc_date == date(2027, 1, 3)


@pytest.mark.parametrize("status", ["Received", "Contingent", "Routed", "In Flight"])
def test_resting_statuses_are_live(status):
    assert _classify(_member("TP", status), _member("SL", status)).state == SLICE_LIVE


def test_tp_fill_is_a_tp_fill_even_if_the_partner_is_cancelled():
    obs = _classify(_member("TP", "Filled", fills=[(5, 61.5)]), _member("SL", "Cancelled"))
    assert obs.state == SLICE_FILLED_TP and obs.kind == "TP"
    assert obs.filled_qty == 5 and obs.fill_price == 61.5 and obs.remaining_live_qty == 0


def test_sl_fill_is_an_sl_fill():
    obs = _classify(_member("TP", "Cancelled"), _member("SL", "Filled", fills=[(5, 54.9)]))
    assert obs.state == SLICE_FILLED_SL and obs.kind == "SL" and obs.fill_price == 54.9


def test_a_partial_fill_counts_and_keeps_the_remainder_live():
    obs = _classify(_member("TP", "Live", fills=[(2, 61.5)]), _member("SL", "Live"))
    assert obs.state == SLICE_FILLED_TP and obs.filled_qty == 2 and obs.remaining_live_qty == 3
    assert "remainder" in obs.detail


def test_a_fill_wins_over_every_other_signal():
    obs = _classify(_member("TP", "Expired", fills=[(1, 61.0)]), _member("SL", "Cancel Requested"))
    assert obs.state == SLICE_FILLED_TP


def test_cancel_requested_is_cancelling_and_still_reserves_the_shares():
    obs = _classify(_member("TP", "Cancel Requested"), _member("SL", "Cancel Requested"))
    assert obs.state == SLICE_CANCELLING and obs.remaining_live_qty == 5


def test_cancelled_when_we_asked_is_not_an_alarm():
    obs = _classify(_member("TP", "Cancelled"), _member("SL", "Cancelled"), ours=True)
    assert obs.state == SLICE_CANCELLED_BY_US


def test_cancelled_when_we_did_not_ask_is_an_alarm():
    obs = _classify(_member("TP", "Cancelled"), _member("SL", "Removed"), ours=False)
    assert obs.state == SLICE_LOST_CANCELLED and "NOT by this platform" in obs.detail


def test_expired_is_an_alarm():
    assert _classify(_member("TP", "Expired"), _member("SL", "Expired")).state == SLICE_LOST_EXPIRED


def test_rejected_is_an_alarm():
    assert _classify(_member("TP", "Rejected"), _member("SL", "Rejected")).state == SLICE_LOST_REJECTED


def test_a_mixed_live_and_cancelled_state_is_unknown_never_live():
    obs = _classify(_member("TP", "Live"), _member("SL", "Cancelled"))
    assert obs.state == SLICE_UNKNOWN


def test_an_unmapped_status_is_unknown():
    assert _classify(_member("TP", "Weird"), _member("SL", "Live")).state == SLICE_UNKNOWN


def test_no_members_is_unknown():
    assert ap.classify_complex_order(_cx(), slice_quantity=5, we_requested_cancel=False).state == SLICE_UNKNOWN


def test_terminal_detection():
    assert ap.is_complex_order_terminal(_cx(_member("TP", "Cancelled"), _member("SL", "Cancelled")))
    assert not ap.is_complex_order_terminal(_cx(_member("TP", "Cancel Requested"), _member("SL", "Cancelled")))
    assert not ap.is_complex_order_terminal(_cx())


# --------------------------------------------------------------------- status

NOW = datetime(2026, 10, 5, 12, 0)


def _status(**over):
    kw = dict(enabled=True, held_at=None, held_reason=None, pending_replace=False,
              pending_replace_since=None, slice_states=[(SLICE_LIVE, 10.0)],
              position_quantity=10.0, alert_message=None, now=NOW)
    kw.update(over)
    return ap.protection_status(**kw)


def test_status_off_when_disabled():
    s = _status(enabled=False, slice_states=[])
    assert s.code == ap.STATUS_OFF and not s.alarm


def test_status_protected():
    s = _status()
    assert s.code == ap.STATUS_PROTECTED and not s.alarm and s.covered_quantity == 10


def test_status_protected_notes_the_fractional_remainder():
    s = _status(position_quantity=10.4)
    assert s.code == ap.STATUS_PROTECTED and "fractional" in s.tooltip


def test_status_partial_when_the_position_grew():
    s = _status(position_quantity=14.0)
    assert s.code == ap.STATUS_PARTIAL and s.alarm


def test_status_unprotected_when_nothing_is_live():
    s = _status(slice_states=[])
    assert s.code == ap.STATUS_UNPROTECTED and s.alarm


def test_status_unprotected_with_a_lost_slice():
    s = _status(slice_states=[(SLICE_LOST_EXPIRED, 0.0), (SLICE_LIVE, 4.0)], position_quantity=10)
    assert s.code == ap.STATUS_UNPROTECTED and "LOST_EXPIRED" in s.tooltip
    assert s.alarm


def test_status_no_position_is_not_an_alarm():
    s = _status(slice_states=[], position_quantity=0.0)
    assert s.code == ap.STATUS_NO_POSITION and not s.alarm


def test_status_a_fractional_only_position_is_not_an_alarm():
    s = _status(slice_states=[], position_quantity=0.4)
    assert s.code == ap.STATUS_NO_POSITION


def test_status_held_badge_names_the_date():
    s = _status(held_at=datetime(2026, 10, 3, 9, 30), held_reason="take-profit filled",
                slice_states=[(SLICE_LIVE, 4.0)])
    assert s.code == ap.STATUS_HELD
    assert s.label == "Held after TP/SL fill on 2026-10-03" and "4 share(s) are still covered" in s.tooltip


def test_status_held_with_a_lost_remaining_slice_is_flagged():
    s = _status(held_at=NOW, slice_states=[(SLICE_LOST_CANCELLED, 0.0)])
    assert s.code == ap.STATUS_HELD and s.alarm


def test_status_replacing_then_stale():
    s = _status(pending_replace=True, pending_replace_since=NOW - timedelta(seconds=30),
                slice_states=[])
    assert s.code == ap.STATUS_REPLACING and not s.alarm
    s = _status(pending_replace=True, pending_replace_since=NOW - timedelta(hours=1), slice_states=[])
    assert s.code == ap.STATUS_UNPROTECTED and s.alarm


def test_status_unknown_position_is_unprotected_not_fine():
    s = _status(position_quantity=None)
    assert s.code == ap.STATUS_UNPROTECTED and s.alarm


def test_gtc_expiry_due_window():
    today = date(2026, 10, 5)
    assert ap.gtc_expiry_due(datetime(2026, 10, 10), today=today)
    assert ap.gtc_expiry_due(datetime(2026, 10, 12), today=today)
    assert not ap.gtc_expiry_due(datetime(2026, 10, 20), today=today)
    assert not ap.gtc_expiry_due(None, today=today)
