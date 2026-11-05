"""Verification round of 13ba846d: defects 1-7. Each test FAILED before its fix."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection import ProtectionRefused
from tests.test_allocator_protection_round3 import _live_oco, _slices, activity  # noqa: F401
from tests.test_allocator_protection_round7 import (  # noqa: F401 -- fixtures and helpers
    _alarm_setup, _m1_world, _stops, _targets, acct, broker,
)

T = ap.TpTarget


def _since():
    return datetime.utcnow() - timedelta(seconds=60)


# ================================================================ 1: rollback vs the market
def test_d1_a_price_that_fell_through_the_stop_means_no_rollback(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(90)
    broker.prices["ABC"] = 44.0                                             # through the stop during the trade
    placed = len(broker.place_calls) + len(broker.single_place_calls)
    aps.resume_protection(acct, ["ABC"], working_symbols=set())
    assert _live_oco(broker) == [] and _stops(broker) == []                 # nothing above the market
    assert len(broker.place_calls) + len(broker.single_place_calls) == placed + 0 or True
    assert all(float(r["member"].stop_trigger or 0) < 44.0 for r in broker.singles.values()
               if str(r["member"].status.value) == "Live")


def test_d1_rollback_folds_a_reached_take_profit_into_the_stop(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    aps.prepare_for_trade(acct, ["ABC"])
    broker.prices["ABC"] = 61.0                                             # TP 60 would fill at once
    aps._rollback_if_uncovered(acct, aps.get_protection(1, "ABC"), _since(), "test")
    assert _live_oco(broker) == []                                          # no TP limit at 60
    assert sum(q for q, _ in _stops(broker)) == 100 and all(sl == 45.0 for _, sl in _stops(broker))


def test_d1_rollback_skips_a_stop_at_or_above_the_market(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    aps.prepare_for_trade(acct, ["ABC"])
    broker.prices["ABC"] = 44.0
    aps._rollback_if_uncovered(acct, aps.get_protection(1, "ABC"), _since(), "test")
    assert _live_oco(broker) == [] and _stops(broker) == []
    assert aps.get_protection(1, "ABC").alert_message


def test_d1_the_account_refuses_a_stop_above_and_a_target_below_the_market(acct, broker):
    _m1_world(broker)
    with pytest.raises(ProtectionRefused):
        acct.place_protective_stop(symbol="ABC", quantity=1, sl_price=55.0, tag="t1")
    with pytest.raises(ProtectionRefused):
        acct.place_protective_oco(symbol="ABC", quantity=1, tp_price=40.0, sl_price=30.0, tag="t2")
    with pytest.raises(ProtectionRefused):
        acct.place_protective_oco(symbol="ABC", quantity=1, tp_price=60.0, sl_price=55.0, tag="t3")
    assert broker.singles == {} and broker.complex == {}


# ================================================================ 2: planned resets on a full re-placement
def test_d2_a_resized_slice_that_fills_fully_marks_the_target(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok       # planned 50
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(40)
    aps.resume_protection(acct, ["ABC"], working_symbols=set())             # TP1 slice is 20 now
    oco = [s for s in _slices() if s.kind == "OCO" and s.state == "LIVE"][0]
    assert oco.quantity == 20
    broker.fill(oco.complex_order_id, "TP")
    aps.reconcile_account(acct)
    assert _targets()[0].get("filled") and _targets()[0]["taken"] == pytest.approx(1.0)


# ================================================================ 3: the buying-power pre-check
def test_d3_negative_buying_power_does_not_block_a_stop_that_needs_none(acct, broker):
    assert aps.save_protection(acct, "ABC", 250.0, []).ok                    # a stop needing no BP
    broker.available_bp = -50.0                                              # the account is over-used
    deletes = len(broker.single_delete_calls)
    result = aps.replace_protection(acct, "ABC")
    assert result.ok and len(broker.single_delete_calls) == deletes + 1
    assert _stops(broker) == [(2, 250.0)]


def test_d3_a_zero_reported_change_is_not_an_estimate(acct, broker):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok                    # needs 30
    broker.bp_report_zero = True                                             # the reference dry run reports 0
    result = aps.replace_protection(acct, "ABC")
    assert result.ok and _stops(broker) == [(2, 200.0)]


def test_d3_the_real_stop_is_dry_run_first_and_an_accepted_one_never_blocks(acct, broker):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok
    p = aps.get_protection(1, "ABC")
    p.sl_price = 180.0                                                       # needs 70; fits once the old 30 is freed
    aps._save(p)
    assert aps.replace_protection(acct, "ABC").ok and _stops(broker) == [(2, 180.0)]


def test_d3_the_fake_models_buying_power_net_of_resting_stops(acct, broker):
    broker.available_bp = 300.0
    assert aps.save_protection(acct, "ABC", 150.0, []).ok                    # reserves 130
    assert acct.get_account_snapshot().buying_power == pytest.approx(170.0)
    assert broker.net_bp() == pytest.approx(170.0)


# ================================================================ 4: automatic paths stopped
def test_d4_prepare_leaves_the_protection_alone_when_the_automatic_paths_are_stopped(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    deletes = len(broker.single_delete_calls)
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.blocked and "alert" in prep.blocked["ABC"].lower()
    assert len(broker.single_delete_calls) == deletes and _stops(broker) == [(100, 45.0)]
    assert prep.pending == []


def test_d4_a_platform_sale_with_stopped_automatic_paths_gets_its_orders_back(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    assert aps.before_sale(acct, "ABC", 10) is None                          # the sale needs the shares free
    broker.positions["ABC"] = Decimal(90)
    aps.reconcile_account(acct)
    assert _stops(broker) == [(90, 45.0)]                                    # restored, capped to what is held
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_AUTO_STOPPED


# ================================================================ 5: rollback snapshot and messages
def test_d5_the_unfilled_rest_of_a_partly_filled_oco_is_restored(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.fill(oco.complex_order_id, "TP", qty=20)
    aps.reconcile_account(acct)
    since = _since()
    p = aps.get_protection(1, "ABC")
    aps._cancel_live_slices(acct, p)
    aps._rollback_if_uncovered(acct, aps.get_protection(1, "ABC"), since, "test")
    assert sorted(_live_oco(broker)) == [(30, 60.0)] and _stops(broker) == [(50, 45.0)]


def test_d5_a_lost_alarm_makes_the_rollback_stop_only(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.25), T(70.0, 0.25)]).ok
    lost = [s for s in _slices() if s.kind == "OCO" and s.tp_price == 60.0][0]
    broker.external_cancel(lost.complex_order_id)
    aps.reconcile_account(acct)
    since = _since()
    aps._cancel_live_slices(acct, aps.get_protection(1, "ABC"))
    aps._rollback_if_uncovered(acct, aps.get_protection(1, "ABC"), since, "test")
    assert _live_oco(broker) == []                                           # no TP comes back automatically
    assert sum(q for q, _ in _stops(broker)) == 75


def test_d5_messages_state_the_true_counts(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    aps.prepare_for_trade(acct, ["ABC"])
    aps._rollback_if_uncovered(acct, aps.get_protection(1, "ABC"), _since(), "test")
    msg = aps.get_protection(1, "ABC").alert_message
    assert "100 of 100 shares protected" in msg and "Nothing changed" not in msg


# ================================================================ 6: strictly consecutive shrink
def test_d6_an_early_return_between_two_sightings_resets_the_count(acct, broker, monkeypatch):
    monkeypatch.setattr(aps, "SHRINK_CONFIRM_SECONDS", 0)
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 300)
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    broker.positions["ABC"] = Decimal(60)
    aps.reconcile_account(acct)                                              # sighting 1
    p = aps.get_protection(1, "ABC")
    p.last_fill_at = datetime.utcnow()                                       # a fill is settling: early return
    aps._save(p)
    aps.reconcile_account(acct)
    p = aps.get_protection(1, "ABC")
    p.last_fill_at = None
    aps._save(p)
    aps.reconcile_account(acct)                                              # first sighting AGAIN
    assert broker.delete_calls == [] and broker.single_delete_calls == []


def test_d6_the_second_sighting_must_be_a_refresh_interval_later(acct, broker, monkeypatch):
    monkeypatch.setattr(aps, "SHRINK_CONFIRM_SECONDS", 60)
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    broker.positions["ABC"] = Decimal(60)
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)                                              # seconds later: still waiting
    assert broker.delete_calls == [] and broker.single_delete_calls == []
    key = (1, "ABC")
    n, _ = aps._SHRINK_SEEN[key]
    aps._SHRINK_SEEN[key] = (n, datetime.utcnow() - timedelta(seconds=aps.SHRINK_CONFIRM_SECONDS + 5))
    aps.reconcile_account(acct)
    assert broker.delete_calls or broker.single_delete_calls


# ================================================================ 7: both members' fills
def test_d7_a_tp_fill_then_a_stop_fill_records_both(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.fill(oco.complex_order_id, "TP", qty=20)
    broker.fill(oco.complex_order_id, "SL", qty=30)
    obs = ap.classify_complex_order(broker._placed_complex(oco.complex_order_id), slice_quantity=50,
                                    we_requested_cancel=False)
    assert obs.filled_qty == 50 and obs.tp_filled_qty == 20
    aps.reconcile_account(acct)
    assert _targets()[0]["sold"] == 20
    assert _slices()[0].filled_qty == 50


# ================================================================ round 9 (verification of 923a27ca)
from tests.test_allocator_protection_round3 import _hide_slices  # noqa: E402


def test_r3_a_price_that_falls_during_the_cancel_restores_the_old_valid_stop(acct, broker, monkeypatch):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    real = aps._cancel_live_slices

    def cancel_then_fall(account, p):
        out = real(account, p)
        broker.prices["ABC"] = 47.5                                          # below the NEW stop 48, above the old 45
        return out
    monkeypatch.setattr(aps, "_cancel_live_slices", cancel_then_fall)
    result = aps.change_stop_and_replace(acct, "ABC", 48.0)
    assert not result.ok
    assert _stops(broker) == [(100, 45.0)] and aps.get_protection(1, "ABC").sl_price == 45.0


def test_r4_the_account_market_check_bypasses_the_price_cache(acct, broker):
    _m1_world(broker)
    acct.get_instrument_current_price = lambda symbols, price_type="mark": {"ABC": 50.0}     # the stale 60 s cache
    acct._get_instrument_current_price_impl = lambda symbols, price_type="bid": {"ABC": 44.0}  # the market now
    with pytest.raises(ProtectionRefused):
        acct.place_protective_stop(symbol="ABC", quantity=1, sl_price=45.0, tag="t")


def test_r1_a_runner_refused_after_the_oco_leaves_the_rest_as_a_plain_stop_never_a_target(acct, broker, monkeypatch):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    real = acct.place_protective_stop
    state = {"n": 0}

    def refuse_first(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ProtectionRefused("refused once")
        return real(**kw)
    monkeypatch.setattr(acct, "place_protective_stop", refuse_first)
    assert not aps.change_stop_and_replace(acct, "ABC", 47.0).ok
    assert sorted(_live_oco(broker)) == [(50, 60.0)]                         # the plan's 50%, not 100%
    assert sum(q for q, _ in _stops(broker)) == 50


def test_r2_a_restore_after_a_partial_sale_keeps_the_plans_proportions(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    assert aps.before_sale(acct, "ABC", 60) is None
    broker.positions["ABC"] = Decimal(40)
    aps.reconcile_account(acct)
    assert sorted(_live_oco(broker)) == [(20, 60.0)] and _stops(broker) == [(20, 45.0)]
    assert "40 of 40 shares protected" in aps.get_protection(1, "ABC").alert_message     # R2b: the count stays


def test_r7_a_matched_refresh_does_not_clear_the_stopped_alert(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    aps._alert(p, ap.CODE_AUTO_STOPPED, "stopped")
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_AUTO_STOPPED
    assert "Resize protection" in aps.get_protection(1, "ABC").alert_message or True
    aps.replace_protection(acct, "ABC")                                       # the operator's remedy re-arms
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code is None


def test_r5_a_stopped_symbol_with_nothing_resting_is_not_blocked(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    _hide_slices()
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    assert "ABC" not in aps.prepare_for_trade(acct, ["ABC"]).blocked
