"""Review findings F1-F11 (Opus review of b8005547) and the operator's 2026-10-05 answers, each as a
test that FAILS on the reviewed code. F1-F4 and F6 reproduce review probes P1-P4/P2.

No broker is contacted: everything runs through the real ``TastyTradeAccount`` methods over the fake
complex-order API (tests/allocator_protection_fakes.py).
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tastytrade.order import OrderStatus as TTOrderStatus
from tastytrade.utils import TastytradeError

from ba2_trade_platform.core import allocator_exclusion as aex
from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtection, AllocatorProtectionOrder, SLICE_CANCELLING, SLICE_LIVE,
    SLICE_LOST_REJECTED, SLICE_UNKNOWN,
)
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import PortfolioAllocationSymbol, TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    ActivityLogSeverity, OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus,
)
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity

T = ap.TpTarget


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


def _age_unknown_slices(minutes):
    with get_db() as session:
        for s in session.query(AllocatorProtectionOrder).all():
            s.placed_at = s.placed_at - timedelta(minutes=minutes)
            session.add(s)
        session.commit()


def _slices(symbol="ABC"):
    return aps.get_slices(aps.get_protection(1, symbol).id)


def _live_qty(b):
    """Shares the broker holds reserved by resting protective orders (an OCO counts once)."""
    total = 0
    for r in b.complex.values():
        if all(m.status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED) for m in r["members"]):
            total += int(r["members"][0].size)
    for r in b.singles.values():
        if (r["member"].status in (TTOrderStatus.LIVE, TTOrderStatus.RECEIVED)
                and str(r["member"].order_type.value) != "Market"):
            total += int(r["member"].size)
    return total


def _timeout_after_acceptance(b, which="single"):
    """The broker ACCEPTS the live placement and then the call raises (a read timeout)."""
    attr = "place_order" if which == "single" else "place_complex_order"
    original = getattr(b, attr)
    state = {"n": 0}

    async def place_then_timeout(session, order, dry_run=True):
        r = await original(session, order, dry_run=dry_run)
        if not dry_run and state["n"] == 0:
            state["n"] += 1
            raise TimeoutError("read timed out")
        return r
    setattr(b, attr, place_then_timeout)


# ============================================================================ F1 (P1)

def test_f1_a_timeout_after_acceptance_is_UNKNOWN_with_its_tag_not_rejected(acct, broker):
    _timeout_after_acceptance(broker)
    result = aps.save_protection(acct, "ABC", 45.0, [])                 # stop-only, 10 sh
    assert not result.ok
    (s,) = _slices()
    assert s.state == SLICE_UNKNOWN and s.external_tag and s.state != SLICE_LOST_REJECTED
    assert "MAY be resting" in s.detail
    assert _live_qty(broker) == 10                                       # the ghost really is there


def test_f1_resize_after_the_timeout_does_not_double_the_stops(acct, broker):
    """Probe P1: before the fix Resize superseded the UNKNOWN row and placed 10 more -> 20 live on 10."""
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    result = aps.replace_protection(acct, "ABC")
    assert _live_qty(broker) <= 10, "Resize must never leave more shares reserved than held"
    live = [s for s in _slices() if s.state == SLICE_LIVE]
    assert sum(s.quantity for s in live) == 10 == _live_qty(broker)
    assert result.ok


def test_f1_the_unknown_order_is_found_by_its_tag_and_adopted(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    aps.reconcile_account(acct)
    (s,) = _slices()
    assert s.state == SLICE_LIVE and s.sl_order_id and s.closed_at is None
    assert aps.status_for(aps.get_protection(1, "ABC"), 10.0).code == ap.STATUS_PROTECTED


def test_f1_an_unresolved_unknown_blocks_the_trade_and_new_placements(acct, broker):
    _timeout_after_acceptance(broker)
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.history_fails = True                                          # the tag search cannot complete
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.blocked and prep.pending == []
    placed = aps.replace_protection(acct, "ABC")
    assert not placed.ok and _live_qty(broker) == 10                     # nothing new was sent
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]               # never auto-superseded


def test_f1_an_unknown_that_is_not_at_the_broker_is_closed_as_never_placed(acct, broker):
    broker.single_raise_on_place = TimeoutError("connect timeout")       # raised BEFORE any order existed
    aps.save_protection(acct, "ABC", 45.0, [])
    assert [s.state for s in _slices()] == [SLICE_UNKNOWN]
    broker.single_raise_on_place = None
    _age_unknown_slices(minutes=10)                                      # N2: only an OLD UNKNOWN is concluded
    aps.reconcile_account(acct)
    s = _slices()[0]                                                     # (the growth rule may then place a new one)
    assert s.state == SLICE_LOST_REJECTED and "never reached" in s.detail and s.closed_at is not None


def test_f1_a_readback_failure_keeps_the_known_id(acct, broker):
    original = broker.get_order
    state = {"n": 0}

    async def flaky(session, order_id):
        state["n"] += 1
        if state["n"] == 1:
            raise TastytradeError("read failed")
        return await original(session, order_id)
    broker.get_order = flaky
    aps.save_protection(acct, "ABC", 45.0, [])
    (s,) = _slices()
    assert s.state == SLICE_UNKNOWN and s.sl_order_id == 70000          # the id the broker named is KEPT
    aps.reconcile_account(acct)                                          # read by id: LIVE again
    assert _slices()[0].state == SLICE_LIVE


def test_f1_a_real_broker_rejection_is_still_a_clean_refusal(acct, broker):
    broker.single_raise_on_place = TastytradeError("insufficient_quantity: no")
    aps.save_protection(acct, "ABC", 45.0, [])
    assert [s.state for s in _slices()] == [SLICE_LOST_REJECTED]         # the broker ANSWERED: not unknown


def test_f1_the_oco_path_has_the_same_rule(acct, broker):
    _timeout_after_acceptance(broker, "complex")
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])
    (s,) = _slices()
    assert s.state == SLICE_UNKNOWN
    aps.reconcile_account(acct)
    assert _slices()[0].state == SLICE_LIVE and _slices()[0].complex_order_id


def test_f1_a_placement_stops_at_the_first_unknown(acct, broker):
    _timeout_after_acceptance(broker, "complex")
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5), T(65, 0.5)])
    assert len(_slices()) == 1 and len(broker.complex) == 1              # the second slice was NOT attempted


# ============================================================================ F2 -- see test_allocator_protection_run.py

# ============================================================================ F3 (P3)

def test_f3_a_disabled_protection_with_an_unconfirmed_cancel_still_blocks_the_trade(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    assert not aps.disable_protection(acct, "ABC").ok
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert "ABC" in prep.blocked and prep.pending == []


def test_f3_a_disabled_protection_with_confirmed_cancels_does_not_block(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    assert aps.disable_protection(acct, "ABC").ok
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert prep.blocked == {} and prep.pending == []


def test_f3_a_disabled_protection_whose_orders_are_still_live_is_cancelled_by_prepare(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])
    p = aps.get_protection(1, "ABC")
    p.enabled = False                                                    # off, but its orders rest
    update_instance(p)
    prep = aps.prepare_for_trade(acct, ["ABC"])
    assert prep.blocked == {} and _live_qty(broker) == 0 and prep.pending == []


# ============================================================================ F4 (P4)

def test_f4_resize_is_refused_while_a_replacement_is_pending(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [])
    aps.prepare_for_trade(acct, ["ABC"])                                 # mid-run: orders cancelled, owed
    deletes = len(broker.single_delete_calls)
    result = aps.replace_protection(acct, "ABC")
    assert not result.ok and "being re-placed" in result.message
    assert _live_qty(broker) == 0 and len(broker.single_delete_calls) == deletes


def test_f4_save_and_resize_are_refused_while_a_run_is_in_flight(acct, broker):
    import threading
    from ba2_trade_platform.core.portfolio_allocation_service import _submission_lock
    aps.save_protection(acct, "ABC", 45.0, [])
    held, release = threading.Event(), threading.Event()

    def hold():
        with _submission_lock(acct.id):
            held.set()
            release.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        r1 = aps.replace_protection(acct, "ABC")
        r2 = aps.save_protection(acct, "ABC", 44.0, [])
    finally:
        release.set()
        t.join()
    assert not r1.ok and "run is in flight" in r1.message and not r2.ok and "run is in flight" in r2.message
    assert _live_qty(broker) == 10


# ============================================================================ F5

def _store_weight(pct, symbol="ABC", label="L"):
    add_instance(PortfolioAllocationSymbol(account_id=1, label=label, symbol=symbol, weight_pct=pct))


def _weight(symbol="ABC", label="L"):
    with get_db() as session:
        from sqlmodel import select
        return session.exec(select(PortfolioAllocationSymbol).where(
            PortfolioAllocationSymbol.symbol == symbol)).first().weight_pct


def test_f5_the_weight_follows_the_HELD_shares_not_the_covered_ones(acct, broker):
    """10 held, 5+5 protected, one slice LOST: the other TP fills 5 -> held 10 -> 5 (factor .5),
    not covered 5 -> 0 (factor 0, which would make the next rebalance sell the other 5)."""
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5), T(65, 0.5)])
    _store_weight(8.0)
    slices = _slices()
    broker.external_cancel(slices[1].complex_order_id)                   # slice 2 lost (alarm)
    aps.reconcile_account(acct)
    broker.fill(slices[0].complex_order_id, "TP")                        # slice 1 sells 5
    aps.reconcile_account(acct)
    assert _weight() == pytest.approx(4.0)                               # 8 x 5/10, never 0


def test_f5_uncovered_growth_does_not_distort_the_cut(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])                 # 10 covered
    broker.positions["ABC"] = Decimal(20)                                # 10 uncovered shares appeared
    _store_weight(10.0)
    broker.fill(_slices()[0].complex_order_id, "TP")                     # the 10 covered shares sell
    aps.reconcile_account(acct)
    assert _weight() == pytest.approx(5.0)                               # 10 x 10/20 (held), not 0


def test_f5_a_failed_position_read_falls_back_to_the_covered_quantity(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])
    _store_weight(10.0)
    broker.fill(_slices()[0].complex_order_id, "TP", qty=5)
    real = acct.get_positions
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] >= 2:                                              # the reconcile's own read worked
            raise RuntimeError("positions down")
        return real()
    acct.get_positions = flaky
    aps.reconcile_account(acct)
    assert _weight() == pytest.approx(5.0)                               # covered 10 -> 5


# ============================================================================ F6 (P2)

def _txn(quantity, symbol="ABC", price=48.0):
    tid = add_instance(Transaction(symbol=symbol, quantity=quantity, side=OrderDirection.BUY,
                                   open_price=price, status=TransactionStatus.OPENED,
                                   open_date=datetime.now(timezone.utc)))
    add_instance(TradingOrder(account_id=1, symbol=symbol, quantity=quantity, filled_qty=quantity,
                              side=OrderDirection.BUY, order_type=OrderType.MARKET, good_for="day",
                              status=OrderStatus.FILLED, open_type=OrderOpenType.MANUAL,
                              open_price=price, transaction_id=tid))
    return tid


def test_f6_a_protective_sale_survives_refresh_transactions(acct, broker):
    """Probe P2: the transaction went 10 -> 6 -> 10 because refresh_transactions recomputes the
    quantity from linked FILLED orders. The sale is now a synthetic FILLED SELL order."""
    tid = _txn(10)
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.4), T(65, 0.6)])
    broker.fill(_slices()[0].complex_order_id, "TP")                     # 4 sold
    aps.reconcile_account(acct)
    assert get_instance(Transaction, tid).quantity == 6
    acct.refresh_transactions()
    assert get_instance(Transaction, tid).quantity == 6                  # NOT re-inflated to 10
    acct.refresh_transactions()
    assert get_instance(Transaction, tid).quantity == 6


def test_f6_the_synthetic_order_is_marked_and_linked(acct, broker):
    tid = _txn(10)
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])
    broker.fill(_slices()[0].complex_order_id, "TP", qty=4)
    aps.reconcile_account(acct)
    with get_db() as session:
        from sqlmodel import select
        sales = session.exec(select(TradingOrder).where(
            TradingOrder.side == OrderDirection.SELL, TradingOrder.transaction_id == tid)).all()
    assert len(sales) == 1
    o = sales[0]
    assert (o.status, o.filled_qty, o.open_price) == (OrderStatus.FILLED, 4.0, 60.0)
    assert o.data["source"] == "allocator_protection" and o.data["tag"].startswith("ba2prot:")
    assert "closing" not in (o.comment or "").lower()                    # the duplicate-close guard's word


def test_f6_a_full_exit_closes_the_transaction_and_refresh_keeps_it_closed(acct, broker):
    tid = _txn(10)
    aps.save_protection(acct, "ABC", 45.0, [T(60, 1.0)])
    broker.fill(_slices()[0].complex_order_id, "TP")
    aps.reconcile_account(acct)
    t = get_instance(Transaction, tid)
    assert t.status == TransactionStatus.CLOSED and t.close_price == 60.0
    acct.refresh_transactions()
    assert get_instance(Transaction, tid).status == TransactionStatus.CLOSED


def test_f6_fifo_across_transactions_and_the_sizing_the_allocator_uses(acct, broker):
    t1, t2 = _txn(3), _txn(7)
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5), T(65, 0.5)])
    broker.fill(_slices()[0].complex_order_id, "TP")                     # 5 sold: t1 fully, t2 by 2
    aps.reconcile_account(acct)
    acct.refresh_transactions()
    assert get_instance(Transaction, t1).status == TransactionStatus.CLOSED
    assert get_instance(Transaction, t2).quantity == 5


# ============================================================================ F7 -- the platform's other sells

def _sell_order(symbol="ABC", qty=4):
    return TradingOrder(account_id=1, symbol=symbol, quantity=qty, side=OrderDirection.SELL,
                        order_type=OrderType.MARKET, good_for="day", status=OrderStatus.PENDING,
                        open_type=OrderOpenType.MANUAL)


def _buy_order(symbol="ABC", qty=4):
    return TradingOrder(account_id=1, symbol=symbol, quantity=qty, side=OrderDirection.BUY,
                        order_type=OrderType.MARKET, good_for="day", status=OrderStatus.PENDING,
                        open_type=OrderOpenType.MANUAL)


@pytest.fixture
def sell_ready(acct):
    acct._tradable_quantity = lambda equity, quantity: Decimal(str(quantity))
    return acct


def test_f7_before_sale_cancels_confirmed_and_owes_the_replacement(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    assert aps.before_sale(acct, "ABC") is None
    assert _live_qty(broker) == 0
    p = aps.get_protection(1, "ABC")
    assert p.pending_replace and p.pending_replace_since is not None


def test_f7_before_sale_refuses_when_the_cancel_is_not_confirmed(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    reason = aps.before_sale(acct, "ABC")
    assert reason and "could not be confirmed cancelled" in reason


def test_f7_before_sale_ignores_an_unprotected_symbol_and_a_protection_with_nothing_resting(acct, broker):
    assert aps.before_sale(acct, "ABC") is None
    aps.save_protection(acct, "ABC", 45.0, [])
    aps.prepare_for_trade(acct, ["ABC"])                                 # the allocator already cancelled
    cancels = len(broker.single_delete_calls)
    assert aps.before_sale(acct, "ABC") is None and len(broker.single_delete_calls) == cancels


def test_f7_every_sale_the_adapter_sends_cancels_protection_first(sell_ready, broker):
    """The ONE choke point (``_submit_order_impl``) covers expert exits, Live Trades manual close,
    Smart Risk Manager and the breached-stop force close: they all call submit_order."""
    acct = sell_ready
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    order = _sell_order()
    out = acct._submit_order_impl(order)
    assert out is not None and out.broker_order_id
    assert _live_qty(broker) == 0                                        # the reservation was released
    assert aps.get_protection(1, "ABC").pending_replace                  # ...and re-placement is owed
    market = [o for _, o in broker.single_place_calls if not _ and False] or broker.single_place_calls
    assert any(not dry for dry, _ in broker.single_place_calls)


def test_f7_a_sale_is_REFUSED_when_the_protection_cannot_be_cancelled(sell_ready, broker):
    acct = sell_ready
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    live_before = len([1 for dry, _ in broker.single_place_calls if not dry])
    order = _sell_order()
    out = acct._submit_order_impl(order)
    assert out is None                                                   # never sent blind
    assert len([1 for dry, _ in broker.single_place_calls if not dry]) == live_before
    row = get_instance(TradingOrder, order.id)
    assert row.status == OrderStatus.ERROR and "refused" in (row.comment or "")


def test_f7_a_buy_never_touches_protection(sell_ready, broker):
    acct = sell_ready
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    deletes = len(broker.delete_calls) + len(broker.single_delete_calls)
    assert acct._submit_order_impl(_buy_order()) is not None
    assert len(broker.delete_calls) + len(broker.single_delete_calls) == deletes
    assert not aps.get_protection(1, "ABC").pending_replace


def test_f7_the_background_refresh_re_places_protection_after_the_platform_sale_settles(sell_ready, broker):
    acct = sell_ready
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    order = _sell_order()
    acct._submit_order_impl(order)
    row = get_instance(TradingOrder, order.id)
    row.status = OrderStatus.FILLED
    row.filled_qty = 4
    update_instance(row)
    broker.positions["ABC"] = Decimal(6)
    report = aps.reconcile_account(acct)
    assert report.resumed == ["ABC"] and sum(s.quantity for s in _slices() if s.state == SLICE_LIVE) == 6


def test_f7_a_sale_still_working_keeps_the_replacement_waiting(sell_ready, broker):
    acct = sell_ready
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    acct._submit_order_impl(_sell_order())                               # stays PENDING in the db
    assert aps.reconcile_account(acct).resumed == []
    assert aps.get_protection(1, "ABC").pending_replace


# ============================================================================ F8

def test_f8_a_buy_only_symbol_is_not_cancelled_by_prepare(acct, broker):
    """The run passes only the symbols the plan SELLS to prepare (see test_allocator_protection_run)."""
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    prep = aps.prepare_for_trade(acct, [])
    assert prep.pending == [] and _live_qty(broker) == 10


def test_f8_cancels_share_one_confirmation_poll(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.2), T(65, 0.2), T(70, 0.2)])      # 3 OCOs + a stop
    broker.cancel_polls = 3
    reads = {"n": 0}
    original = broker.get_complex_order

    async def counting(session, order_id):
        reads["n"] += 1
        return await original(session, order_id)
    broker.get_complex_order = counting
    items = [("OCO", s.complex_order_id) for s in _slices() if s.complex_order_id]
    outcomes = acct.cancel_protective_batch(items)
    assert all(o.confirmed for o in outcomes.values())
    # one shared poll: ~ (cancel_polls + 1) reads per order, NOT sequential waits on each in turn
    assert reads["n"] <= len(items) * (broker.cancel_polls + 2)


def test_f8_the_batch_timeout_is_total_not_per_order(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5), T(65, 0.5)])
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    outcomes = acct.cancel_protective_batch([("OCO", s.complex_order_id) for s in _slices()])
    assert len(outcomes) == 2 and not any(o.confirmed for o in outcomes.values())


def test_f8_the_page_payload_is_display_only(monkeypatch):
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    called = []
    monkeypatch.setattr(aps, "reconcile_account", lambda account: called.append(account))
    account = SimpleNamespace(id=1, supports_allocator_protection=True)
    out = page._load_protection_payload(account, 1, {})
    assert out["supported"] is True and called == []                     # NO broker work on page load


def test_f8_an_explicit_check_action_exists_and_reconciles(monkeypatch):
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    assert hasattr(page, "check_protection_now")
    seen = []
    monkeypatch.setattr(aps, "reconcile_account", lambda account: seen.append(account) or aps.ReconcileReport())
    monkeypatch.setattr(aps, "renew_expiring", lambda account: [])
    import ba2_trade_platform.core.utils as core_utils
    monkeypatch.setattr(core_utils, "get_account_instance_from_id",
                        lambda i: SimpleNamespace(id=1, supports_allocator_protection=True))
    page.check_protection_now(1)
    assert len(seen) == 1


# ============================================================================ F9

def _order(status, created, symbol="ABC", comment=None):
    add_instance(TradingOrder(account_id=1, symbol=symbol, quantity=4, side=OrderDirection.SELL,
                              order_type=OrderType.MARKET, good_for="day", status=status,
                              open_type=OrderOpenType.MANUAL, created_at=created, comment=comment))


def test_f9_a_stale_nonterminal_order_does_not_keep_the_symbol_replacing(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    _order(OrderStatus.ACCEPTED, datetime.now(timezone.utc) - timedelta(days=9))      # an old stuck row
    aps.prepare_for_trade(acct, ["ABC"])
    broker.positions["ABC"] = Decimal(6)
    assert aps.resume_protection(acct, ["ABC"]) == ["ABC"]


def test_f9_an_order_created_after_the_cancel_does_count(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    aps.prepare_for_trade(acct, ["ABC"])
    _order(OrderStatus.ACCEPTED, datetime.now(timezone.utc) + timedelta(seconds=1))
    broker.positions["ABC"] = Decimal(6)
    assert aps.resume_protection(acct, ["ABC"]) == []
    assert aps.get_protection(1, "ABC").pending_replace


def test_f9_becoming_unprotected_writes_an_alert_and_an_activity_entry(acct, broker, activity):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    aps.prepare_for_trade(acct, ["ABC"])
    p = aps.get_protection(1, "ABC")
    p.pending_replace_since = datetime.utcnow() - timedelta(hours=1)
    update_instance(p)
    _order(OrderStatus.ACCEPTED, datetime.now(timezone.utc))              # a sale that never settles
    aps.reconcile_account(acct)
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_REPLACE_STALE
    assert any(c["data"].get("code") == ap.CODE_REPLACE_STALE
               and c["severity"] == ActivityLogSeverity.FAILURE for c in activity)
    assert aps.status_for(p, 5.0).code == ap.STATUS_UNPROTECTED


# ============================================================================ F10

def test_f10_a_warning_never_overwrites_a_failure_alert(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    p = aps.get_protection(1, "ABC")
    p = aps._alert(p, ap.CODE_LOST_EXPIRED, "slice expired")
    p = aps._alert(p, ap.CODE_RECONCILE_FETCH_FAILED, "api down", severity=ActivityLogSeverity.WARNING)
    assert p.alert_code == ap.CODE_LOST_EXPIRED and p.alert_message == "slice expired"
    assert aps.open_alerts(1)                                            # still on the banner


def test_f10_a_failure_overwrites_a_warning_and_a_fetch_failure_does_not_hide_a_lost_slice(acct, broker):
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    broker.external_cancel(_slices()[0].complex_order_id)
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED
    broker.raise_on_read = TastytradeError("api down")
    aps.reconcile_account(acct)
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_LOST_CANCELLED


# ============================================================================ F11

def test_f11_the_dialog_rows_show_the_tag_and_the_stop_order_id(acct, broker):
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    aps.save_protection(acct, "ABC", 45.0, [T(60, 0.5)])
    rows = dlg.slice_rows(_slices())
    assert rows[0]["tag"].startswith("ba2prot:") and rows[1]["tag"].startswith("ba2prot:")
    assert rows[1]["stop"] == _slices()[1].sl_order_id and rows[0]["order"] == _slices()[0].complex_order_id


def test_f11_an_unknown_slice_is_listed_with_its_tag(acct, broker):
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    broker.single_raise_on_place = TimeoutError("t")
    aps.save_protection(acct, "ABC", 45.0, [])
    (row,) = dlg.slice_rows(_slices())
    assert row["state"] == SLICE_UNKNOWN and row["tag"].startswith("ba2prot:")


# ============================================================================ exclusion warning + migration docstring

def test_excluding_the_last_enabled_symbol_of_a_funded_label_is_warned(monkeypatch):
    from ba2_trade_platform.ui.pages import portfolio_allocation as page
    from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label
    from ba2_trade_platform.core.utils import add_label_to_instruments
    set_managed_label(1, "L", target_pct=50.0)
    add_label_to_instruments(["AAA", "BBB"], "L")
    aex.exclude_symbol(1, "AAA")
    assert page.labels_left_without_symbols(1, "BBB") == ["L"]
    assert page.labels_left_without_symbols(1, "ZZZ") == []
    aex.include_symbol(1, "AAA")
    assert page.labels_left_without_symbols(1, "BBB") == []


def test_the_migration_docstring_names_four_tables():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "alembic/versions/a7c3e91d5b24_add_allocator_protection_tables.py").read_text(encoding="utf8")
    assert "four tables" in text and "both tables" not in text
