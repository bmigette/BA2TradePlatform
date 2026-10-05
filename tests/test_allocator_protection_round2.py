"""Review round 2 (Opus verification of 198fa71e): N8, N1, N5, N2, N6, N7 and operator answers Q1-Q2.

No broker is contacted: everything runs through the real ``TastyTradeAccount`` methods over the fake
complex-order API (tests/allocator_protection_fakes.py).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import select
from tastytrade.order import OrderStatus as TTOrderStatus

from ba2_trade_platform.core import allocator_exclusion as aex
from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtectionOrder, SLICE_LIVE, SLICE_LOST_REJECTED, SLICE_UNKNOWN,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance
from ba2_trade_platform.core.models import PortfolioAllocationSymbol, TradingOrder, Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
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


# ======================================================================== N8
TARGETS = [T(60.0, 0.5), T(70.0, 0.5)]


def _tp1_filled_price_62(acct, broker):
    """Probe: TP1 60@50%, TP2 70@50%, SL 45; TP1 fills; the price is 62 now."""
    assert aps.save_protection(acct, "ABC", 45.0, TARGETS).ok
    first = [s for s in _slices() if s.kind == "OCO" and s.target_index == 0][0]
    broker.fill(first.complex_order_id, "TP")
    aps.reconcile_account(acct)
    broker.prices["ABC"] = 62.0


def test_n8_drop_reached_targets_folds_the_fraction_into_the_stop():
    usable, reached = ap.drop_reached_targets([T(60.0, 0.5), T(70.0, 0.5)], 62.0)
    assert usable == [T(70.0, 0.5)] and reached == [T(60.0, 0.5)]
    plans, _ = ap.plan_slices(shares=50, targets=usable, sl_price=45.0, last_price=62.0)
    assert sum(x.quantity for x in plans) == 50 and any(x.kind == "STOP" for x in plans)


def test_n8_a_target_exactly_at_the_price_is_reached():
    usable, reached = ap.drop_reached_targets([T(62.0, 0.3)], 62.0)
    assert usable == [] and len(reached) == 1


def test_n8_the_gtc_renewal_after_a_tp_fill_keeps_a_stop(acct, broker):
    _tp1_filled_price_62(acct, broker)
    p = aps.get_protection(1, "ABC")
    result = aps.replace_protection(acct, "ABC")
    assert result.ok, result
    assert _live_qty(broker) == 50                      # the 50 left shares are covered, not 0
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert [s.tp_price for s in live] == [70.0] or {s.tp_price for s in live} == {70.0, None}
    assert all(s.tp_price != 60.0 for s in live)                 # the filled target is NOT re-placed
    assert aps.get_protection(1, "ABC").alert_code is None


def test_n8_resize_after_a_tp_fill_keeps_a_stop(acct, broker):
    _tp1_filled_price_62(acct, broker)
    broker.positions["ABC"] = Decimal(40)               # a manual shrink after the fill
    p = aps.get_protection(1, "ABC")
    p, ok = aps._resize_now(acct, p, "shrink resize")
    assert ok and _live_qty(broker) == 40


def test_n8_a_refused_plan_keeps_the_existing_orders_automatically(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, TARGETS).ok
    broker.positions["ABC"] = Decimal(60)
    broker.prices["ABC"] = 44.0                         # price through the stop: nothing valid
    before = len(broker.delete_calls)
    p = aps.get_protection(1, "ABC")
    p, ok = aps._resize_now(acct, p, "shrink resize")
    assert not ok and len(broker.delete_calls) == before    # NOTHING was cancelled
    assert _live_qty(broker) == 100 and p.alert_code == ap.CODE_PLACEMENT_REFUSED


def test_n8_prepare_and_resume_after_a_tp_fill_never_leave_zero_protection(acct, broker):
    _tp1_filled_price_62(acct, broker)
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert prep.pending == ["ABC"]
    aps.resume_protection(acct, ["ABC"])
    assert _live_qty(broker) == 50


# ======================================================================== N1
def _fresh_unknown(acct, broker, symbol="ABC", sl=45.0):
    broker.single_raise_on_place = TimeoutError("connect timeout")
    aps.save_protection(acct, symbol, sl, [])
    broker.single_raise_on_place = None


def test_n1_a_same_tag_order_for_another_symbol_is_never_adopted(acct, broker):
    broker.positions["XYZ"] = Decimal(20)
    broker.prices["XYZ"] = 100.0
    _fresh_unknown(acct, broker)
    (s,) = _slices()
    # An order for ANOTHER symbol carrying the very same tag (a reused protection id in the old scheme).
    broker.place_foreign_stop("XYZ", 20, 90.0, s.external_tag)
    _age(10)
    aps.reconcile_account(acct)
    (s,) = [x for x in _slices() if x.id == s.id]
    assert s.sl_order_id is None                                     # NOT adopted: another symbol's order
    assert s.state == SLICE_LOST_REJECTED and s.closed_at is not None  # ours never reached the broker


def test_n1_a_matching_order_is_still_adopted(acct, broker):
    broker.single_raise_on_place = None
    import tests.test_allocator_protection_review as rev
    rev._timeout_after_acceptance(broker, "single")
    aps.save_protection(acct, "ABC", 45.0, [])
    (s,) = _slices()
    assert s.state == SLICE_UNKNOWN
    aps.reconcile_account(acct)
    assert [x.state for x in _slices()][0] == SLICE_LIVE


# ======================================================================== N2
def test_n2_a_young_unknown_stays_unknown_and_blocks_a_second_order(acct, broker):
    _fresh_unknown(acct, broker)
    placed = broker.single_place_count()
    aps.reconcile_account(acct)                          # seconds later: the broker may not list it yet
    states = [x.state for x in _slices()]
    assert states == [SLICE_UNKNOWN] and broker.single_place_count() == placed
    aps.reconcile_account(acct)
    assert broker.single_place_count() == placed
    assert len(_slices()) == 1                           # still no second order


def test_n2_an_old_unknown_is_searched_again_and_then_closed(acct, broker):
    _fresh_unknown(acct, broker)
    _age(ap.UNKNOWN_MIN_AGE_SECONDS // 60 + 1)
    aps.reconcile_account(acct)
    assert _slices()[0].state == SLICE_LOST_REJECTED


def test_n2_the_age_floor_is_a_named_constant():
    assert ap.UNKNOWN_MIN_AGE_SECONDS >= 180


# ======================================================================== N5
def test_n5_resume_does_not_disarm_while_an_order_rests(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    prep_p = aps.get_protection(1, "ABC")
    broker.positions["ABC"] = Decimal(0)                 # the position reads as exited
    from ba2_trade_platform.core.db import update_instance
    prep_p.pending_replace = True
    prep_p.pending_replace_since = datetime.utcnow() - timedelta(minutes=30)
    update_instance(prep_p)
    aps.resume_protection(acct, ["ABC"])
    p = aps.get_protection(1, "ABC")
    assert p.enabled and p.disarmed_at is None
    assert p.alert_code == ap.CODE_QUANTITY_MISMATCH and p.alert_message


def test_n5_disarm_keeps_the_alert_text_when_it_refuses(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    p = aps._disarm(aps.get_protection(1, "ABC"), "test")
    assert p.enabled and p.alert_code == ap.CODE_QUANTITY_MISMATCH


def test_n5_disarm_still_works_when_nothing_rests(acct, broker, monkeypatch):
    monkeypatch.setattr(aps, "FILL_SETTLE_SECONDS", 0)
    assert aps.save_protection(acct, "ABC", 45.0, []).ok
    s = _slices()[0]
    broker.fill_single(s.sl_order_id)
    aps.reconcile_account(acct)
    assert not aps.get_protection(1, "ABC").enabled


# ======================================================================== N6
def test_n6_before_sale_without_a_row_never_takes_the_lock(acct, broker, monkeypatch):
    def boom(account_id):
        raise AssertionError("the lock was taken for an unprotected symbol")
    monkeypatch.setattr(aps, "protection_lock", boom)
    assert aps.before_sale(acct, "ABC", 10) is None


# ======================================================================== N7
def _filled_sale():
    from ba2_trade_platform.core.types import OrderOpenType
    add_instance(TradingOrder(account_id=1, symbol="ABC", quantity=100.0, filled_qty=100.0,
                              side=OrderDirection.SELL, order_type=OrderType.MARKET,
                              status=OrderStatus.FILLED, open_type=OrderOpenType.MANUAL, broker_order_id="55"))


def test_n7_a_lagging_position_read_after_a_full_sale_does_not_replace(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    assert aps.before_sale(acct, "ABC", 100) is None     # cancels, marks pending, records the expectation
    p = aps.get_protection(1, "ABC")
    assert p.expected_qty == 0
    _filled_sale()
    # The platform sale filled in the DB, but the broker still reads the OLD position.
    placed = len(broker.place_calls)
    aps.resume_protection(acct, ["ABC"])
    assert len(broker.place_calls) == placed             # waits: nothing placed on a stale 100 shares
    assert aps.get_protection(1, "ABC").pending_replace


def test_n7_once_the_read_agrees_the_symbol_is_disarmed(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    aps.before_sale(acct, "ABC", 100)
    broker.positions["ABC"] = Decimal(0)
    aps.resume_protection(acct, ["ABC"])
    assert not aps.get_protection(1, "ABC").enabled


def test_n7_after_the_settle_window_a_stale_read_is_trusted_no_longer_waited_on(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    aps.before_sale(acct, "ABC", 100)
    _filled_sale()
    from ba2_trade_platform.core.db import update_instance
    p = aps.get_protection(1, "ABC")
    p.pending_replace_since = datetime.utcnow() - timedelta(seconds=aps.FILL_SETTLE_SECONDS + 60)
    update_instance(p)
    aps.resume_protection(acct, ["ABC"])
    assert not aps.get_protection(1, "ABC").pending_replace


# ======================================================================== Q1
def _opened(symbol, qty, price):
    tid = add_instance(Transaction(symbol=symbol, quantity=float(qty), side=OrderDirection.BUY,
                                   status=TransactionStatus.OPENED, open_price=price,
                                   open_date=datetime.now(timezone.utc)))
    add_instance(TradingOrder(account_id=1, symbol=symbol, quantity=float(qty), filled_qty=float(qty),
                              side=OrderDirection.BUY, order_type=OrderType.MARKET,
                              status=OrderStatus.FILLED, transaction_id=tid, open_price=price,
                              broker_order_id="9"))


def _weights(symbol):
    with get_db() as session:
        return {r.label: r.weight_pct for r in session.exec(select(PortfolioAllocationSymbol).where(
            PortfolioAllocationSymbol.symbol == symbol)).all()}


def test_q1_implicit_weight_is_written_explicit_and_reduced(monkeypatch):
    changes = aex.reduce_symbol_weights(1, "ABC", 0.5, reason="tp_fill", detail="TP1", implicit={"L": {"ABC": 20.0}})
    assert _weights("ABC") == {"L": 10.0}
    assert [(c.before_pct, c.after_pct) for c in changes] == [(20.0, 10.0)]


def test_q1_a_stored_row_wins_over_the_implicit_share():
    add_instance(PortfolioAllocationSymbol(account_id=1, label="L", symbol="ABC", weight_pct=8.0))
    aex.reduce_symbol_weights(1, "ABC", 0.5, reason="tp", detail="TP1", implicit={"L": {"ABC": 20.0}})
    assert _weights("ABC") == {"L": 4.0}


def test_q1_a_full_exit_writes_an_explicit_zero():
    aex.reduce_symbol_weights(1, "ABC", 0.0, reason="tp", detail="exit", implicit={"L": {"ABC": 20.0}})
    assert _weights("ABC") == {"L": 0.0}


def test_q1_the_fill_path_writes_the_measured_share_for_an_unstored_symbol(acct, broker, monkeypatch):
    """_implicit_weights measures the label share BEFORE the fill, from positions and prices."""
    broker.positions["OTHER"] = Decimal(100)
    broker.prices["OTHER"] = 50.0                         # ABC 100 x 50 = 5000, OTHER 5000: 50% each
    from ba2_trade_platform.core import utils as core_utils
    from ba2_trade_platform.core import portfolio_allocation_store as store
    from types import SimpleNamespace
    monkeypatch.setattr(store, "get_managed_labels", lambda a: [SimpleNamespace(label="L")])
    monkeypatch.setattr(core_utils, "get_symbols_by_label", lambda labels: {"L": ["ABC", "OTHER"]})
    p = aps.get_protection(1, "ABC") or SimpleNamespace(account_id=1, symbol="ABC")
    out = aps._implicit_weights(acct, p, 100.0)
    assert out == {"L": {"ABC": 50.0, "OTHER": 50.0}}
    add_instance(PortfolioAllocationSymbol(account_id=1, label="L", symbol="ABC", weight_pct=30.0))
    assert aps._implicit_weights(acct, p, 100.0) == {"L": {"OTHER": 50.0}}   # ABC stored: only OTHER still unstored


# ======================================================================== Q2
def test_q2_include_warns_when_the_label_would_exceed_100(monkeypatch):
    from types import SimpleNamespace
    from ba2_trade_platform.core import utils as core_utils
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    monkeypatch.setattr(page, "get_managed_labels", lambda a: [SimpleNamespace(label="L"),
                                                              SimpleNamespace(label="M")])
    monkeypatch.setattr(core_utils, "get_symbols_by_label",
                        lambda labels: {"L": ["ABC", "DEF"], "M": ["ABC", "GHI"]})
    for label, sym, w in (("L", "ABC", 60.0), ("L", "DEF", 55.0), ("M", "ABC", 40.0), ("M", "GHI", 40.0)):
        add_instance(PortfolioAllocationSymbol(account_id=1, label=label, symbol=sym, weight_pct=w))
    assert page.labels_over_allocated_by_include(1, "abc") == [("L", 115.0)]
    assert page.labels_over_allocated_by_include(1, "ZZZ") == []
