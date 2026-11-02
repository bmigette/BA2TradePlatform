"""TastyTradeAccount's protective-OCO surface, driven through the REAL methods against the
FAKE complex-order API (tests/allocator_protection_fakes.py). No broker is ever contacted."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tastytrade.order import ComplexOrderType, OrderAction, OrderTimeInForce, OrderType as TTOrderType
from tastytrade.utils import TastytradeError

from ba2_trade_platform.core.allocator_protection import PlacementOutcomeUnknown, ProtectionRefused
from ba2_trade_platform.modules.accounts.TastyTradeAccount import TastyTradeAccount
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity


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


def _place(acct, **over):
    kw = dict(symbol="ABC", quantity=4, tp_price=60.0, sl_price=45.0, tag="ba2prot:1:0")
    kw.update(over)
    return acct.place_protective_oco(**kw)


# ----------------------------------------------------------------- placement

def test_the_expert_paths_are_still_refused(acct):
    assert TastyTradeAccount.supports_protective_legs is False
    with pytest.raises(NotImplementedError):
        acct.adjust_tp_sl(SimpleNamespace(id=1), 1.0, 1.0)
    with pytest.raises(NotImplementedError):
        acct.adjust_sl(SimpleNamespace(id=1), 1.0)
    assert acct.modify_order("1") is None


def test_the_account_declares_the_capability():
    assert TastyTradeAccount.supports_allocator_protection is True


def test_place_sends_a_dry_run_then_a_live_oco_of_two_sell_to_close_legs(acct, broker):
    result = _place(acct)
    assert [dry for dry, _ in broker.place_calls] == [True, False]       # dry run FIRST, then live
    live = broker.place_calls[1][1]
    assert live.type == ComplexOrderType.OCO and live.trigger_order is None
    tp, sl = live.orders
    assert tp.order_type == TTOrderType.LIMIT and tp.price == Decimal("60.0")     # credit: positive
    assert sl.order_type == TTOrderType.STOP and sl.stop_trigger == Decimal("45.0")
    for order in (tp, sl):
        assert order.time_in_force == OrderTimeInForce.GTC
        assert [l.action for l in order.legs] == [OrderAction.SELL_TO_CLOSE]
        assert order.legs[0].quantity == Decimal(4)
        assert order.external_identifier == "ba2prot:1:0"
    assert result.complex_order_id == 9000 and result.quantity == 4
    assert result.status == "LIVE" and result.gtc_date is not None
    assert result.tp_order_id and result.sl_order_id


def test_place_serialises_without_a_price_effect_on_the_stop(acct, broker):
    _place(acct)
    sl = broker.place_calls[1][1].orders[1]
    assert sl.price is None


def test_a_dry_run_error_refuses_and_never_goes_live(acct, broker):
    broker.dry_run_errors = ["tif_invalid: something the broker said"]
    with pytest.raises(ProtectionRefused, match="something the broker said"):
        _place(acct)
    assert [dry for dry, _ in broker.place_calls] == [True]
    assert broker.complex == {}


def test_a_live_placement_exception_refuses_with_the_brokers_words(acct, broker):
    broker.raise_on_place = TastytradeError("insufficient_quantity: nope")
    with pytest.raises(ProtectionRefused, match="insufficient_quantity"):
        _place(acct)


def test_an_empty_broker_error_is_still_explained(acct, broker):
    broker.raise_on_place = TastytradeError("")
    with pytest.raises(ProtectionRefused, match="insufficient scopes"):
        _place(acct)


def test_an_order_accepted_but_not_live_is_refused_and_not_left_behind(acct, broker):
    broker.reject_after_accept = True
    with pytest.raises(ProtectionRefused, match="not live"):
        _place(acct)
    assert broker.delete_calls == []         # nothing to clean up: it was rejected, not resting


def test_a_response_with_errors_cancels_what_was_placed(acct, broker):
    broker.place_errors = ["warning-ish error"]
    with pytest.raises(ProtectionRefused, match="warning-ish error"):
        _place(acct)
    assert broker.delete_calls == [9000]     # the order existed and was cleaned up


def test_an_unreadable_readback_is_UNKNOWN_and_keeps_the_complex_id(acct, broker):
    original = broker.get_complex_order
    state = {"n": 0}

    async def flaky(session, order_id):
        state["n"] += 1
        if state["n"] == 1:
            raise TastytradeError("read failed")
        return await original(session, order_id)
    broker.get_complex_order = flaky
    with pytest.raises(PlacementOutcomeUnknown, match="could not read it back") as info:
        _place(acct)
    assert info.value.broker_id == 9000 and info.value.tag == "ba2prot:1:0"    # the known id is never dropped
    assert broker.delete_calls == []                                           # no blind cleanup


@pytest.mark.parametrize("qty", [0, -3, 2.5, "1.5"])
def test_fractional_zero_or_negative_quantity_is_refused_before_any_call(acct, broker, qty):
    with pytest.raises(ProtectionRefused, match="WHOLE number"):
        _place(acct, quantity=qty)
    assert broker.place_calls == []


def test_a_stop_at_or_above_the_target_is_refused_before_any_call(acct, broker):
    with pytest.raises(ProtectionRefused, match="stop"):
        _place(acct, tp_price=45.0, sl_price=45.0)
    assert broker.place_calls == []


def test_unauthenticated_account_refuses(acct, broker):
    acct._session = None
    with pytest.raises(ProtectionRefused, match="not authenticated"):
        _place(acct)
    assert broker.place_calls == []


def test_a_whole_float_quantity_is_sent_as_a_whole_number(acct, broker):
    _place(acct, quantity=4.0)
    assert broker.place_calls[1][1].orders[0].legs[0].quantity == Decimal(4)


# ----------------------------------------------------------------- reads

def test_get_complex_order_state_reads_one_id(acct, broker):
    result = _place(acct)
    placed = acct.get_complex_order_state(result.complex_order_id)
    assert placed.id == result.complex_order_id and len(placed.orders) == 2


def test_get_complex_order_state_propagates_a_failed_read(acct, broker):
    broker.raise_on_read = TastytradeError("down")
    with pytest.raises(TastytradeError):
        acct.get_complex_order_state(9000)


def test_list_live_complex_orders_is_todays_live_only(acct, broker):
    result = _place(acct)
    assert [c.id for c in acct.list_live_complex_orders()] == [result.complex_order_id]
    broker.expire(result.complex_order_id)
    assert acct.list_live_complex_orders() == []


def test_equity_tick_sizes_reads_the_instrument(acct, broker):
    sizes = acct.equity_tick_sizes("ABC")
    assert sizes == broker.tick_sizes


# ----------------------------------------------------------------- cancel

def test_cancel_waits_for_the_broker_to_confirm(acct, broker):
    cid = _place(acct).complex_order_id
    broker.cancel_polls = 3
    outcome = acct.cancel_complex_order(cid)
    assert outcome.confirmed and not outcome.filled
    assert broker.delete_calls == [cid]
    assert all(str(m.status.value) == "Cancelled" for m in broker.complex[cid]["members"])


def test_cancel_that_never_confirms_is_reported_unconfirmed(acct, broker):
    cid = _place(acct).complex_order_id
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    outcome = acct.cancel_complex_order(cid)
    assert not outcome.confirmed and "not confirmed cancelled" in outcome.detail


def test_cancel_when_the_order_fills_first_reports_the_fill(acct, broker):
    cid = _place(acct).complex_order_id
    broker.fill_on_delete = "TP"
    outcome = acct.cancel_complex_order(cid)
    assert outcome.confirmed and outcome.filled


def test_cancel_of_an_already_finished_order_is_confirmed_by_the_state_read(acct, broker):
    cid = _place(acct).complex_order_id
    broker.expire(cid)                       # delete will raise "not cancellable"
    outcome = acct.cancel_complex_order(cid)
    assert outcome.confirmed and "not cancellable" in outcome.detail


def test_cancel_with_an_unreadable_state_is_unconfirmed_not_assumed(acct, broker):
    cid = _place(acct).complex_order_id
    broker.raise_on_read = TastytradeError("read down")
    outcome = acct.cancel_complex_order(cid)
    assert not outcome.confirmed and "could not read" in outcome.detail


def test_cancel_when_delete_raises_and_the_order_is_still_live_is_unconfirmed(acct, broker):
    cid = _place(acct).complex_order_id
    broker.raise_on_delete = TastytradeError("boom")
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    outcome = acct.cancel_complex_order(cid)
    assert not outcome.confirmed and "boom" in outcome.detail


def test_cancel_unauthenticated_is_unconfirmed(acct, broker):
    acct._session = None
    assert not acct.cancel_complex_order(1).confirmed


# ----------------------------------------------------------------- stop-only (the runner)

def _stop(acct, **over):
    kw = dict(symbol="ABC", quantity=6, sl_price=45.0, tag="ba2prot:1:2")
    kw.update(over)
    return acct.place_protective_stop(**kw)


def test_stop_only_sends_a_dry_run_then_a_live_plain_gtc_stop_sell_to_close(acct, broker):
    result = _stop(acct)
    assert [dry for dry, _ in broker.single_place_calls] == [True, False]
    order = broker.single_place_calls[1][1]
    assert order.order_type == TTOrderType.STOP and order.stop_trigger == Decimal("45.0")
    assert order.time_in_force == OrderTimeInForce.GTC and order.price is None
    assert [l.action for l in order.legs] == [OrderAction.SELL_TO_CLOSE]
    assert order.legs[0].quantity == Decimal(6) and order.external_identifier == "ba2prot:1:2"
    assert broker.complex == {}                                       # NOT a complex order
    assert result.order_id == 70000 and result.quantity == 6 and result.status == "LIVE"
    assert result.gtc_date is not None


def test_stop_only_dry_run_error_refuses_and_never_goes_live(acct, broker):
    broker.single_dry_run_errors = ["stop_invalid: nope"]
    with pytest.raises(ProtectionRefused, match="stop_invalid"):
        _stop(acct)
    assert [dry for dry, _ in broker.single_place_calls] == [True] and broker.singles == {}


def test_stop_only_placement_exception_refuses(acct, broker):
    broker.single_raise_on_place = TastytradeError("insufficient_quantity")
    with pytest.raises(ProtectionRefused, match="insufficient_quantity"):
        _stop(acct)


def test_stop_only_accepted_but_rejected_is_refused(acct, broker):
    broker.single_reject_after_accept = True
    with pytest.raises(ProtectionRefused, match="not live"):
        _stop(acct)


def test_stop_only_unreadable_readback_is_UNKNOWN_and_keeps_the_order_id(acct, broker):
    original = broker.get_order
    state = {"n": 0}

    async def flaky(session, order_id):
        state["n"] += 1
        if state["n"] == 1:
            raise TastytradeError("read failed")
        return await original(session, order_id)
    broker.get_order = flaky
    with pytest.raises(PlacementOutcomeUnknown, match="could not read it back") as info:
        _stop(acct)
    assert info.value.broker_id == 70000 and info.value.kind == "STOP"
    assert broker.single_delete_calls == []


@pytest.mark.parametrize("qty", [0, -1, 2.5])
def test_stop_only_bad_quantity_is_refused_before_any_call(acct, broker, qty):
    with pytest.raises(ProtectionRefused, match="WHOLE number"):
        _stop(acct, quantity=qty)
    assert broker.single_place_calls == []


def test_stop_only_zero_stop_is_refused(acct, broker):
    with pytest.raises(ProtectionRefused, match="must be > 0"):
        _stop(acct, sl_price=0)


def test_stop_only_state_reads_like_a_one_member_complex_order(acct, broker):
    result = _stop(acct)
    state = acct.get_protective_order_state(result.order_id)
    assert state.id == result.order_id and len(state.orders) == 1


def test_stop_only_cancel_waits_for_the_broker(acct, broker):
    oid = _stop(acct).order_id
    broker.cancel_polls = 3
    outcome = acct.cancel_protective_stop(oid)
    assert outcome.confirmed and not outcome.filled and broker.single_delete_calls == [oid]


def test_stop_only_cancel_that_never_confirms_is_unconfirmed(acct, broker):
    oid = _stop(acct).order_id
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    assert not acct.cancel_protective_stop(oid).confirmed


def test_stop_only_cancel_when_it_fills_first_reports_the_fill(acct, broker):
    oid = _stop(acct).order_id
    broker.single_fill_on_delete = True
    outcome = acct.cancel_protective_stop(oid)
    assert outcome.confirmed and outcome.filled


def test_stop_only_cancel_of_a_finished_order_is_confirmed_by_the_state_read(acct, broker):
    oid = _stop(acct).order_id
    broker.expire_single(oid)
    assert acct.cancel_protective_stop(oid).confirmed


# ----------------------------------------------------------------- F1: the live call raising after sending

def test_a_timeout_on_the_live_oco_call_is_unknown_not_refused(acct, broker):
    original = broker.place_complex_order

    async def timeout_after(session, order, dry_run=True):
        r = await original(session, order, dry_run=dry_run)
        if not dry_run:
            raise TimeoutError("read timed out")
        return r
    broker.place_complex_order = timeout_after
    with pytest.raises(PlacementOutcomeUnknown, match="MAY be resting") as info:
        _place(acct)
    assert info.value.kind == "OCO" and info.value.tag == "ba2prot:1:0" and info.value.broker_id is None
    assert len(broker.complex) == 1                                           # the order IS at the broker


def test_a_timeout_on_the_live_stop_call_is_unknown_not_refused(acct, broker):
    original = broker.place_order

    async def timeout_after(session, order, dry_run=True):
        r = await original(session, order, dry_run=dry_run)
        if not dry_run:
            raise ConnectionError("connection reset")
        return r
    broker.place_order = timeout_after
    with pytest.raises(PlacementOutcomeUnknown) as info:
        _stop(acct)
    assert info.value.kind == "STOP" and len(broker.singles) == 1


def test_a_dry_run_exception_is_a_plain_refusal(acct, broker):
    async def boom(session, order, dry_run=True):
        raise TimeoutError("dry run timed out")
    broker.place_complex_order = boom
    with pytest.raises(ProtectionRefused):
        _place(acct)
    assert broker.complex == {}


# ----------------------------------------------------------------- finding an order by its tag

def test_find_by_tag_locates_an_oco_and_a_stop(acct, broker):
    _place(acct, tag="ba2prot:1:0")
    _stop(acct, tag="ba2prot:1:1")
    kinds = {t: acct.find_protective_orders_by_tag(t) for t in ("ba2prot:1:0", "ba2prot:1:1", "ba2prot:9:9")}
    assert [(k, i) for k, i, _ in kinds["ba2prot:1:0"]] == [("OCO", 9000)]
    assert [(k, i) for k, i, _ in kinds["ba2prot:1:1"]] == [("STOP", 70002)] or kinds["ba2prot:1:1"][0][0] == "STOP"
    assert kinds["ba2prot:9:9"] == []


def test_find_by_tag_raises_when_the_search_cannot_be_completed(acct, broker):
    broker.history_fails = True
    with pytest.raises(TastytradeError):
        acct.find_protective_orders_by_tag("ba2prot:1:0")


def test_find_by_tag_sees_a_finished_order_in_history(acct, broker):
    result = _stop(acct, tag="ba2prot:1:3")
    broker.fill_single(result.order_id)
    (found,) = acct.find_protective_orders_by_tag("ba2prot:1:3")
    assert found[0] == "STOP" and found[1] == result.order_id


# ----------------------------------------------------------------- the batch cancel

def test_the_batch_cancels_mixed_kinds_with_one_confirmation_poll(acct, broker):
    oco = _place(acct, tag="ba2prot:1:0").complex_order_id
    stop = _stop(acct, tag="ba2prot:1:1").order_id
    broker.cancel_polls = 2
    out = acct.cancel_protective_batch([("OCO", oco), ("STOP", stop)])
    assert all(o.confirmed for o in out.values()) and set(out) == {("OCO", oco), ("STOP", stop)}
    assert broker.delete_calls == [oco] and broker.single_delete_calls == [stop]


def test_the_batch_reports_a_fill_that_beat_the_cancel(acct, broker):
    oco = _place(acct).complex_order_id
    broker.fill_on_delete = "SL"
    out = acct.cancel_protective_batch([("OCO", oco)])
    assert out[("OCO", oco)].confirmed and out[("OCO", oco)].filled


def test_the_batch_marks_only_the_unconfirmed_ones(acct, broker):
    oco = _place(acct, tag="ba2prot:1:0").complex_order_id
    stop = _stop(acct, tag="ba2prot:1:1").order_id
    broker.never_confirm_cancel = True
    acct._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    broker.expire_single(stop)                                              # this one is already final
    out = acct.cancel_protective_batch([("OCO", oco), ("STOP", stop)])
    assert not out[("OCO", oco)].confirmed and out[("STOP", stop)].confirmed
