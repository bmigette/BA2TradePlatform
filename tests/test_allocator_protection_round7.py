"""Final review findings 1-8 (APP 1229 review). Each test reproduces a probe and FAILED before its fix."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity
from tests.test_allocator_protection_round3 import _live_oco, _slices, activity  # noqa: F401
from tests.test_allocator_protection_ui import nicegui_client  # noqa: F401

T = ap.TpTarget


@pytest.fixture
def broker():
    b = FakeTastyBroker()
    b.positions["ABC"] = Decimal(2)
    b.prices["ABC"] = 287.0
    b.bp_p0 = 215.0
    b.available_bp = 300.0
    return b


@pytest.fixture
def acct(broker, monkeypatch):
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    with patch_equity(broker):
        a = make_account(broker)
        a.get_account_snapshot = lambda: SimpleNamespace(
            buying_power=float(broker.available_bp) - float(broker._reserved_bp()))
        yield a


def _stops(b):
    out = []
    for r in b.singles.values():
        m = r["member"]
        if str(m.status.value) in ("Live", "Received") and m.stop_trigger is not None:
            out.append((int(m.size), float(m.stop_trigger)))
    return sorted(out)


def _deepen(sl):
    p = aps.get_protection(1, "ABC")
    p.sl_price = sl
    aps._save(p)


# ================================================================ 1: cancel then re-place
def test_f1_a_replace_that_cannot_be_afforded_keeps_the_old_orders(acct, broker):
    assert aps.save_protection(acct, "ABC", 150.0, []).ok                 # needs 130 BP
    broker.available_bp = 120.0                                             # BP consumed since
    deletes = len(broker.single_delete_calls)
    result = aps.replace_protection(acct, "ABC")
    assert not result.ok and len(broker.single_delete_calls) == deletes     # NOTHING cancelled
    assert _stops(broker) == [(2, 150.0)]
    assert "buying power" in aps.get_protection(1, "ABC").alert_message


def test_f1_change_stop_to_a_stop_that_cannot_be_afforded_keeps_the_orders(acct, broker):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok                 # needs 30
    result = aps.change_stop_and_replace(acct, "ABC", 150.0)               # needs 130 > 100
    assert not result.ok and _stops(broker) == [(2, 200.0)]
    assert aps.get_protection(1, "ABC").sl_price == 200.0


def test_f1_a_refused_re_placement_after_the_cancel_restores_the_previous_orders(acct, broker, monkeypatch, activity):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok
    monkeypatch.setattr(aps, "_preflight_bp", lambda *a, **k: [])          # a stale estimate: the pre-check passes
    _deepen(150.0)
    result = aps.replace_protection(acct, "ABC")
    assert not result.ok
    assert _stops(broker) == [(2, 200.0)]                                   # rolled back to the OLD price
    assert any(c["data"].get("code") == "ROLLBACK" for c in activity)


def test_f1_a_rollback_that_fails_too_is_a_loud_unprotected_alert(acct, broker, monkeypatch, activity):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok
    monkeypatch.setattr(aps, "_preflight_bp", lambda *a, **k: [])
    _deepen(150.0)
    broker.available_bp = 10.0
    aps.replace_protection(acct, "ABC")
    assert _stops(broker) == []
    fails = [c for c in activity if c["data"].get("code") == "ROLLBACK_FAILED"]
    assert fails and fails[0]["severity"].name == "FAILURE"
    assert aps.get_protection(1, "ABC").alert_code is not None
    assert aps.status_for(aps.get_protection(1, "ABC"), 2.0).code == ap.STATUS_UNPROTECTED


def test_f1_the_allocator_resume_rolls_back_too(acct, broker):
    broker.available_bp = 100.0
    assert aps.save_protection(acct, "ABC", 200.0, []).ok
    aps.prepare_for_trade(acct, ["ABC"])
    _deepen(150.0)                                                          # the template now needs more BP
    aps.resume_protection(acct, ["ABC"], working_symbols=set())
    assert _stops(broker) == [(2, 200.0)]


# ================================================================ 2: a LOST alarm is never bypassed
def _alarm_setup(acct, broker):
    broker.positions["ABC"] = Decimal(100)
    broker.prices["ABC"] = 50.0
    broker.bp_p0 = None
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    oco = [s for s in _slices() if s.kind == "OCO"][0]
    broker.external_cancel(oco.complex_order_id)                              # the operator cancelled it on the site
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED


def test_f2_an_allocator_sale_does_not_bring_back_a_site_cancelled_order(acct, broker):
    _alarm_setup(acct, broker)
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(90)
    aps.resume_protection(acct, ["ABC"], working_symbols=set())
    assert _live_oco(broker) == []                                            # no OCO came back
    assert _stops(broker) == [(90, 45.0)]                                     # only the stop-only cover
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED


def test_f2_a_platform_sale_does_not_bring_back_a_site_cancelled_order(acct, broker):
    _alarm_setup(acct, broker)
    assert aps.before_sale(acct, "ABC", 10) is None
    broker.positions["ABC"] = Decimal(90)
    aps.reconcile_account(acct)
    assert _live_oco(broker) == [] and _stops(broker) == [(90, 45.0)]


def test_f2_when_the_automatic_paths_are_stopped_resume_places_nothing(acct, broker):
    broker.positions["ABC"] = Decimal(100)
    broker.prices["ABC"] = 50.0
    broker.bp_p0 = None
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    aps.prepare_for_trade(acct, ["ABC"])
    p = aps.get_protection(1, "ABC")
    p.auto_failures = aps.AUTO_FAILURE_LIMIT
    aps._save(p)
    placed = broker.single_place_count()
    aps.resume_protection(acct, ["ABC"], working_symbols=set())
    assert broker.single_place_count() == placed
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_AUTO_STOPPED


# ================================================================ 3: M1 by shares
def _m1_world(broker):
    broker.positions["ABC"] = Decimal(100)
    broker.prices["ABC"] = 50.0
    broker.bp_p0 = None


def _targets():
    return aps.get_protection(1, "ABC").tp_targets


def _grown(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    broker.positions["ABC"] = Decimal(120)
    aps.reconcile_account(acct)                                               # growth lot of 10
    orig = [s for s in _slices() if s.kind == "OCO" and s.quantity == 50][0]
    lot = [s for s in _slices() if s.kind == "OCO" and s.quantity == 10][0]
    return orig, lot


def test_f3_p1_an_original_partial_with_a_lot_resting_counts_against_all_planned_shares(acct, broker):
    _m1_world(broker)
    orig, _ = _grown(acct, broker)
    broker.fill(orig.complex_order_id, "TP", qty=20)
    aps.reconcile_account(acct)
    assert _targets()[0]["taken"] == pytest.approx(20 / 60, abs=1e-6)
    assert aps.replace_protection(acct, "ABC").ok
    assert sorted(_live_oco(broker)) == [(40, 60.0)]


def test_f3_p2_a_lot_filling_while_the_original_is_lost_does_not_drop_the_target(acct, broker):
    _m1_world(broker)
    orig, lot = _grown(acct, broker)
    broker.external_cancel(orig.complex_order_id)
    broker.fill(lot.complex_order_id, "TP")
    aps.reconcile_account(acct)
    assert not _targets()[0].get("filled")
    assert aps.replace_protection(acct, "ABC").ok
    oco = _live_oco(broker)
    assert len(oco) == 1 and oco[0][1] == 60.0 and 49 <= oco[0][0] <= 51


def test_f3_p3_a_full_lot_then_an_original_partial_sum_by_shares(acct, broker):
    _m1_world(broker)
    orig, lot = _grown(acct, broker)
    broker.fill(lot.complex_order_id, "TP")
    broker.fill(orig.complex_order_id, "TP", qty=20)
    aps.reconcile_account(acct)
    assert _targets()[0]["taken"] == pytest.approx(0.5, abs=1e-6)
    assert aps.replace_protection(acct, "ABC").ok
    assert sorted(_live_oco(broker)) == [(30, 60.0)]


# ================================================================ 4: no near-market suggestion
def test_f4_a_suggestion_is_never_one_tick_under_the_market(acct, broker):
    broker.bp_p0 = 286.0                                                      # only stops >= 286 are free
    broker.available_bp = 0.0
    report = aps.check_with_broker(acct, "ABC", 100.0, [])
    assert report.suggested_stop is None
    assert "no acceptable stop" in report.warning.lower()


def test_f4_a_suggestion_stays_at_least_the_minimum_distance_below(acct, broker):
    broker.available_bp = 70.0
    report = aps.check_with_broker(acct, "ABC", 100.0, [])
    assert report.suggested_stop is not None
    assert report.suggested_stop <= 287.0 * (1 - aps.SUGGEST_MIN_DISTANCE_PCT / 100.0) + 1e-9


def test_f4_use_a_stop_the_broker_accepts_refuses_a_near_market_stop(acct, broker):
    assert aps.save_protection(acct, "ABC", 250.0, []).ok
    result = aps.change_stop_and_replace(acct, "ABC", 286.99)
    assert not result.ok and "at least" in result.message and aps.get_protection(1, "ABC").sl_price == 250.0


# ================================================================ 5: shrink needs two refreshes
def test_f5_a_shrink_is_acted_on_only_after_two_consecutive_refreshes(acct, broker):
    _m1_world(broker)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    broker.positions["ABC"] = Decimal(60)                                      # a stale / low read
    aps.reconcile_account(acct)
    assert broker.single_delete_calls == [] and broker.delete_calls == []
    broker.positions["ABC"] = Decimal(100)                                     # it was only a blip
    aps.reconcile_account(acct)
    assert broker.single_delete_calls == [] and broker.delete_calls == []
    broker.positions["ABC"] = Decimal(60)
    aps.reconcile_account(acct)
    aps.reconcile_account(acct)                                                # second consecutive: now it acts
    assert broker.delete_calls or broker.single_delete_calls


# ================================================================ 6: cancel unconfirmed is amber
def test_f6_an_unconfirmed_delete_shows_amber_cancel_unconfirmed_not_grey_off(acct, broker):
    broker.bp_p0 = None
    assert aps.save_protection(acct, "ABC", 250.0, []).ok
    broker.never_confirm_cancel = True
    assert not aps.delete_protection(acct, "ABC").ok
    status = aps.status_for(aps.get_protection(1, "ABC"), 2.0)
    assert status.code == ap.STATUS_CANCEL_UNCONFIRMED
    from ba2_trade_platform.ui.utils import allocator_protection_view as view
    assert view.row_fields(True, status)["prot_color"] == "amber"


def test_f6_the_deleted_log_entry_records_the_settings(acct, broker, activity):
    broker.bp_p0 = None
    assert aps.save_protection(acct, "ABC", 250.0, [T(300.0, 0.5)]).ok
    assert aps.delete_protection(acct, "ABC").ok
    (entry,) = [c for c in activity if c["data"].get("code") == "DELETED"]
    assert entry["data"]["was_sl_price"] == 250.0 and entry["data"]["was_tp_targets"][0]["price"] == 300.0


# ================================================================ 7: the check button can act
def test_f7_the_check_button_is_renamed_and_says_what_it_may_do():
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    assert page.PROTECT_CHECK_LABEL == "Check and repair TP/SL"
    text = page.PROTECT_CHECK_CONFIRM.lower()
    assert "cancel" in text and "place" in text and "resize" in text


# ================================================================ 8: gtc_date_assumed is dead code
def test_f8_gtc_date_assumed_is_gone_from_the_code():
    import inspect
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    assert "gtc_date_assumed" not in inspect.getsource(aps)
    assert "gtc_date_assumed" not in inspect.getsource(dlg)
    row = dlg.slice_rows([SimpleNamespace(
        closed_at=None, state="LIVE", slice_index=0, quantity=1, tp_price=None, sl_price=1.0,
        complex_order_id=None, sl_order_id=5, external_tag="t", gtc_date=None)])[0]
    assert row["gtc"] == ""
