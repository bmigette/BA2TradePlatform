"""Review round 5 (final verification of c0250fed): M1, M2a-c, S1-S3. Each test FAILED before its fix."""
import functools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import AllocatorProtectionOrder, SLICE_LIVE, SLICE_UNKNOWN
from ba2_trade_platform.core.db import get_db, update_instance
from tests.test_allocator_protection_ui import nicegui_client  # noqa: F401
from tests.test_allocator_protection_round3 import (  # noqa: F401 -- fixtures + helpers reused
    _age, _live_oco, _live_qty, _slices, _timeout_after_acceptance, activity, acct, broker,
)

T = ap.TpTarget


def _targets():
    return aps.get_protection(1, "ABC").tp_targets


# ======================================================================== M1
def test_m1_s1_a_growth_lot_filling_does_not_mark_the_whole_target_filled(acct, broker, monkeypatch):
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok          # 100 sh @ 60
    broker.positions["ABC"] = broker.positions["ABC"] + Decimal(10)
    aps.reconcile_account(acct)                                              # growth: a 10-share slice
    lot = [s for s in _slices() if s.kind == "OCO" and s.quantity == 10][0]
    broker.fill(lot.complex_order_id, "TP")                                  # only the 10 share lot fills
    aps.reconcile_account(acct)
    assert not _targets()[0].get("filled")                                   # the 100-share slice is untouched
    assert aps.replace_protection(acct, "ABC").ok
    assert _live_oco(broker) == [(100, 60.0)]                                # NOT stop-only


def test_m1_s2_partial_fills_chain_across_a_re_placement(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5), T(70.0, 0.5)]).ok
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP", qty=20)                        # 20 of 50
    aps.reconcile_account(acct)
    assert _targets()[0]["taken"] == pytest.approx(0.4)
    assert aps.replace_protection(acct, "ABC").ok
    assert sorted(_live_oco(broker)) == [(30, 60.0), (50, 70.0)]
    second = [s for s in _slices() if s.state == SLICE_LIVE and s.kind == "OCO" and s.quantity == 30][0]
    broker.fill(second.complex_order_id, "TP", qty=15)                       # 15 of the 30
    aps.reconcile_account(acct)
    assert _targets()[0]["taken"] == pytest.approx(0.7)                       # 0.4 + 0.6 * 0.5, not 0.5
    assert aps.replace_protection(acct, "ABC").ok
    assert sorted(_live_oco(broker)) == [(15, 60.0), (50, 70.0)]


# ======================================================================== M2
def _acct_with_search_stubs(acct, broker, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("the complex-order endpoints must not be read")
    monkeypatch.setattr(broker, "get_complex_order_history", boom)
    monkeypatch.setattr(broker, "get_live_complex_orders", boom)


def test_m2a_a_stop_search_never_reads_the_complex_endpoints(acct, broker, monkeypatch):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _acct_with_search_stubs(acct, broker, monkeypatch)
    found = acct.find_protective_orders_by_tag(_slices()[0].external_tag, kind="STOP",
                                               since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert [f[0] for f in found] == ["STOP"]


def test_m2a_an_oco_is_found_through_its_legs_in_the_plain_history(acct, broker, monkeypatch):
    original = broker.place_complex_order
    state = {"n": 0}

    async def place_then_timeout(session, order, dry_run=True):
        r = await original(session, order, dry_run=dry_run)
        if not dry_run and state["n"] == 0:
            state["n"] += 1
            raise TimeoutError("read timed out")
        return r
    broker.place_complex_order = place_then_timeout
    aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)])
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]
    _acct_with_search_stubs(acct, broker, monkeypatch)
    aps.reconcile_account(acct)
    assert [s.state for s in _slices()] == [SLICE_LIVE]


def test_m2b_a_server_that_ignores_the_sort_is_never_trusted(acct, broker):
    broker.history_ignores_sort = True
    old = datetime.now(timezone.utc) - timedelta(days=3)
    for i in range(60):
        broker.place_foreign_stop("ZZZ", 1, 5.0, "other", received_at=old + timedelta(seconds=i))
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    _age(aps.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    with pytest.raises(RuntimeError):
        acct.find_protective_orders_by_tag(_slices()[0].external_tag, kind="STOP",
                                           since=datetime.now(timezone.utc) - timedelta(hours=1))
    aps.reconcile_account(acct)                                              # and never "never placed"
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]


def test_m2c_start_at_goes_out_as_iso_8601_with_a_T(acct):
    from tastytrade import Account
    sent = []

    class Session:
        async def _paginate(self, model, path, params):
            sent.append(params)
            return []
    real = SimpleNamespace(account_number="5WX00000")

    async def empty(*a, **k):
        return []
    acct._account = SimpleNamespace(
        get_order_history=functools.partial(Account.get_order_history, real),
        get_live_orders=empty, get_live_complex_orders=empty)
    acct._session = Session()
    since = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    acct.find_protective_orders_by_tag("ba2prot:1:0:abcd1234", since=since, kind="STOP")
    assert sent and sent[0]["start-at"] == "2026-10-05T10:00:00+00:00" and sent[0]["sort"] == "Desc"


# ======================================================================== S1
def test_s1_add_only_keeps_the_per_target_totals_of_the_plan():
    plans = ap.plan_add_only(
        total_shares=70, to_cover=40, targets=[T(70.0, 30 / 70)], target_ids=[1],
        existing_by_target={1: 30}, existing_runner=0, sl_price=45.0, last_price=62.0)
    assert [(p.kind, p.quantity) for p in plans] == [("STOP", 40)]            # NOT 17 more shares on TP2


def test_s1_a_repair_places_the_missing_runner_not_more_take_profit(acct, broker, monkeypatch):
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.3), T(70.0, 0.3)]).ok   # 30 + 30 + runner 40
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP")
    aps.reconcile_account(acct)
    runner = [s for s in _slices() if s.kind == "STOP" and s.state == SLICE_LIVE][0]
    broker.external_cancel_single(runner.sl_order_id)
    with get_db() as session:                                                # the runner is lost (cancelled, acknowledged)
        row = session.get(AllocatorProtectionOrder, runner.id)
        row.state, row.closed_at = "CANCELLED_BY_US", datetime.utcnow()
        session.add(row)
        session.commit()
    p = aps.get_protection(1, "ABC")
    aps._place_slices(acct, p)
    assert sorted(_live_oco(broker)) == [(30, 70.0)]                         # TP2 total stays 30
    assert _live_qty(broker) == 70


# ======================================================================== S2
def test_s2_extend_after_buys_respects_an_alarm(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    broker.external_cancel(_slices()[0].complex_order_id)
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED
    broker.positions["ABC"] = broker.positions["ABC"] + Decimal(10)
    placed = len(broker.place_calls)
    aps.extend_after_buys(acct, ["ABC"])
    assert len(broker.place_calls) == placed
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED


# ======================================================================== S3
def test_s3_the_dialog_state_separates_taken_targets_for_re_arming():
    from ba2_trade_platform.ui.pages.allocator_protection_dialog import initial_kept, initial_rows
    p = SimpleNamespace(enabled=True, tp_targets=[{"price": 60.0, "fraction": 0.5, "filled": True},
                                                  {"price": 70.0, "fraction": 0.5}])
    assert initial_rows(p) == [{"price": 70.0, "pct": 50.0}]
    assert initial_kept(p) == [{"price": 60.0, "pct": 50.0}]


def test_s3_save_keeps_the_taken_targets_marked(acct, broker):
    result = aps.save_protection(acct, "ABC", 45.0, [T(70.0, 0.5)], kept_filled=[T(60.0, 0.5)])
    assert result.ok, result
    stored = _targets()
    assert [(t["price"], bool(t.get("filled"))) for t in stored] == [(70.0, False), (60.0, True)]
    assert _live_oco(broker) == [(100, 70.0)]                                # the rest of the position, one target


def test_s3_re_arming_is_just_saving_without_the_kept_target(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(70.0, 0.5), T(60.0, 0.5)]).ok
    assert not any(t.get("filled") for t in _targets())


def test_s3_the_dialog_offers_a_re_arm_control(nicegui_client):
    from tests.test_allocator_protection_ui import _data, _find, _noop
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    from ba2_trade_platform.core.allocator_protection_models import AllocatorProtection
    p = AllocatorProtection(account_id=1, symbol="ABC", enabled=True, sl_price=45.0, tp_targets=[
        {"price": 60.0, "fraction": 0.5, "filled": True}, {"price": 70.0, "fraction": 0.5}])
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0, protection=p), _noop)
    assert _find(nicegui_client, dlg.MARKER_REARM)


# ======================================================================== the read-only probe tool
def test_the_probe_tool_refuses_anything_but_reads():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "tt_protection_probe", pathlib.Path(__file__).resolve().parents[1] / "tools" / "tt_protection_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inner = SimpleNamespace(account_number="X", get_order_history=lambda: "ok", place_order=lambda: "BAD",
                            delete_order=lambda: "BAD", place_complex_order=lambda: "BAD")
    guarded = module.ReadOnly(inner)
    assert guarded.get_order_history() == "ok"
    for name in ("place_order", "delete_order", "place_complex_order", "replace_order"):
        with pytest.raises(PermissionError):
            getattr(guarded, name)
