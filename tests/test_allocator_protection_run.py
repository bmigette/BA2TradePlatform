"""The allocator run boundary (``run_allocation``) with TP/SL protection, end to end on fakes.

The account is the allocation suite's ``FakeAccount`` (order submission, DB rows, fills) with the
protection surface delegated to a REAL ``TastyTradeAccount`` over the fake complex-order API, and
the fake broker REFUSES a sell of shares that resting OCOs reserve -- so a run that did not cancel
first would fail here exactly as it would at TastyTrade.
"""
from decimal import Decimal

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.allocator_protection_models import SLICE_LIVE
from ba2_trade_platform.core.db import add_instance, get_db, get_instance, update_instance
from ba2_trade_platform.core.models import PortfolioAllocationRun, TradingOrder, Transaction
from ba2_trade_platform.core.portfolio_allocation import (
    ALLOCATION_MODE_REBALANCE, AllocationPlan, PositionState,
)
from ba2_trade_platform.core.types import ActivityLogSeverity, OrderDirection, OrderStatus
from tastytrade.utils import TastytradeError
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity
from tests.test_portfolio_allocation_submit import (
    FakeAccount, make_base, make_open_transaction, make_row,
)

T = ap.TpTarget
THIRDS = [T(60, 1 / 3), T(65, 1 / 3), T(70, 1 / 3)]


class ProtectedFakeAccount(FakeAccount):
    """FakeAccount + the TastyTrade protection surface over a fake broker."""

    supports_allocator_protection = True

    def __init__(self, broker, tt, account_id=1):
        super().__init__(account_id)
        self.broker = broker
        self.tt = tt

    # reads come from the broker, like the live adapter
    def get_positions(self):
        return self.tt.get_positions()

    def get_instrument_current_price(self, symbol_or_symbols, price_type='bid'):
        return self.tt.get_instrument_current_price(symbol_or_symbols, price_type=price_type)

    # the protection surface
    def place_protective_oco(self, **kw):
        return self.tt.place_protective_oco(**kw)

    def cancel_complex_order(self, cid):
        return self.tt.cancel_complex_order(cid)

    def get_complex_order_state(self, cid):
        return self.tt.get_complex_order_state(cid)

    def equity_tick_sizes(self, symbol):
        return self.tt.equity_tick_sizes(symbol)

    def cancel_protective_batch(self, items):
        return self.tt.cancel_protective_batch(items)

    def find_protective_orders_by_tag(self, tag, since=None):
        return self.tt.find_protective_orders_by_tag(tag, since=since)

    def place_protective_stop(self, **kw):
        return self.tt.place_protective_stop(**kw)

    def cancel_protective_stop(self, oid):
        return self.tt.cancel_protective_stop(oid)

    def get_protective_order_state(self, oid):
        return self.tt.get_protective_order_state(oid)

    def reserved(self, symbol) -> int:
        total = 0
        for record in self.broker.complex.values():
            for m in record["members"][:1]:                       # an OCO reserves its size ONCE
                if m.underlying_symbol == symbol and str(m.status.value) in ("Live", "Received", "Cancel Requested"):
                    total += int(m.size)
        for record in self.broker.singles.values():                      # a stop-only order reserves too
            m = record["member"]
            if m.underlying_symbol == symbol and str(m.status.value) in ("Live", "Received", "Cancel Requested"):
                total += int(m.size)
        return total

    def close_transaction(self, transaction_id):
        """A full close: refused like the broker would if OCOs still reserve the shares."""
        txn = get_instance(Transaction, transaction_id)
        free = int(self.broker.positions.get(txn.symbol, 0)) - self.reserved(txn.symbol)
        if txn.quantity > free:
            return {'success': False, 'message': 'insufficient_qty: shares are reserved by resting orders'}
        out = super().close_transaction(transaction_id)
        if out.get('success'):
            self.broker.positions[txn.symbol] = (self.broker.positions.get(txn.symbol, Decimal(0))
                                                 - Decimal(str(txn.quantity)))
        return out

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order="<not passed>"):
        if trading_order.side == OrderDirection.SELL:
            free = int(self.broker.positions.get(trading_order.symbol, 0)) - self.reserved(trading_order.symbol)
            if trading_order.quantity > free:
                if trading_order.id is None:
                    trading_order.status = OrderStatus.PENDING
                    trading_order.id = add_instance(trading_order, expunge_after_flush=True)
                self.submitted.append((trading_order.symbol, trading_order.side,
                                       trading_order.quantity, trading_order.comment))
                return self._handle_submit_error(
                    trading_order, "[insufficient_qty] shares are reserved by resting orders")
        out = super().submit_order(trading_order, tp_price, sl_price, is_closing_order)
        if out is not None and out.status == OrderStatus.FILLED:
            delta = Decimal(str(out.quantity)) * (1 if out.side == OrderDirection.BUY else -1)
            self.broker.positions[out.symbol] = self.broker.positions.get(out.symbol, Decimal(0)) + delta
        return out


@pytest.fixture(autouse=True)
def activity_calls(monkeypatch):
    calls = []
    for mod in (svc, aps):
        monkeypatch.setattr(
            mod, "log_activity",
            lambda severity, activity_type, description, data=None, source_expert_id=None,
            source_account_id=None: calls.append({"severity": severity, "description": description,
                                                  "data": data or {}}))
    return calls


@pytest.fixture
def world(activity_calls):
    broker = FakeTastyBroker()
    broker.positions["ABC"] = Decimal(30)
    broker.prices["ABC"] = 50.0
    broker.positions["XYZ"] = Decimal(20)
    broker.prices["XYZ"] = 20.0
    with patch_equity(broker):
        tt = make_account(broker)
        account = ProtectedFakeAccount(broker, tt)
        account.log = activity_calls
        yield broker, account


def _protect(account, symbol="ABC", sl=45.0, targets=None):
    result = aps.save_protection(account, symbol, sl, targets or THIRDS)
    assert result.ok, result
    return result


def _state(account, symbol, qty, txn):
    return PositionState(symbol=symbol, quantity=qty, price=account.broker.prices[symbol],
                         transaction_ids=[txn])


def _trim(symbol, delta, target, price):
    row = make_row(symbol, OrderDirection.SELL if delta < 0 else OrderDirection.BUY, delta,
                   abs(delta) * price, 0.0 if delta < 0 else abs(delta) * price, price=price)
    row.target_quantity = target
    return row


def _run(account, rows, current):
    plan = AllocationPlan(rows=rows, available_buying_power=10_000.0)
    return svc.run_allocation(account, plan, current, make_base(),
                              mode=ALLOCATION_MODE_REBALANCE, scope_label=None)


def _live(symbol="ABC"):
    p = aps.get_protection(1, symbol)
    return [s for s in aps.get_slices(p.id) if s.state == SLICE_LIVE]


def test_a_trim_cancels_the_ocos_first_sells_and_replaces_them_for_the_new_size(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    assert sum(s.quantity for s in _live()) == 30
    old_ids = {s.complex_order_id for s in _live()}
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}

    result = _run(account, [_trim("ABC", -25.0, 5.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})

    assert not result["blocked"], result["blocked_reason"]
    assert [(s[0], s[1], s[2]) for s in account.submitted] == [("ABC", OrderDirection.SELL, 25.0)]
    outcome = result["outcomes"][0]
    assert outcome.status == svc.OUTCOME_SUBMITTED                  # NOT refused for reserved shares
    assert set(broker.delete_calls) == old_ids                      # the OCOs were cancelled first
    assert broker.positions["ABC"] == Decimal(5)
    live = _live()
    assert sum(s.quantity for s in live) == 5 and not old_ids & {s.complex_order_id for s in live}
    p = aps.get_protection(1, "ABC")
    assert not p.pending_replace and p.alert_code is None
    status = aps.status_for(p, 5.0)
    assert status.code == ap.STATUS_PROTECTED
    summary = [c for c in account.log if "allocation run" in c["description"]]
    assert summary and "TP/SL: re-placed 1 of 1" in summary[-1]["description"]


def test_without_the_cancel_the_fake_broker_would_have_refused_the_sell(world):
    """Guards the fake: the reservation rule is what makes the test above meaningful."""
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    from ba2_trade_platform.core.models import TradingOrder as TO
    order = TO(account_id=1, symbol="ABC", quantity=25.0, side=OrderDirection.SELL,
               order_type="market", good_for="day", status=OrderStatus.NEW)
    from ba2_trade_platform.core.types import OrderType, OrderOpenType
    order.order_type = OrderType.MARKET
    order.open_type = OrderOpenType.MANUAL
    assert account.submit_order(order) is None
    assert broker.positions["ABC"] == Decimal(30)


def test_a_buy_resizes_the_protection_upward(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    result = _run(account, [_trim("ABC", 10.0, 40.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED
    assert broker.positions["ABC"] == Decimal(40)
    assert sum(s.quantity for s in _live()) == 40


def test_a_fill_then_a_rebalance_buys_back_and_the_new_shares_get_the_same_template(world):
    """After a TP fill the symbol is NOT excluded. A BUY needs no cancel (it adds shares): the
    remaining orders keep protecting the old shares and the NEW shares get orders from the same
    template (add-only growth, no unprotected window)."""
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)                                                  # 10 + 10 + 10
    broker.fill(_live()[0].complex_order_id, "TP")                     # 10 sold by protection
    aps.reconcile_account(account.tt)
    assert broker.positions["ABC"] == Decimal(20)
    deletes = len(broker.delete_calls)
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    result = _run(account, [_trim("ABC", 10.0, 30.0, 50.0)], {"ABC": _state(account, "ABC", 20.0, txn)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED
    assert broker.positions["ABC"] == Decimal(30)
    assert len(broker.delete_calls) == deletes                          # a buy cancels NOTHING
    live = _live()
    assert sum(s.quantity for s in live) == 30                          # 20 old + 10 added
    assert {s.sl_price for s in live} == {45.0} and set(s.tp_price for s in live) == {60.0, 65.0, 70.0}


def test_a_rebalance_resizes_oco_and_stop_only_slices_with_the_same_proportions(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account, targets=[T(60, 0.5)])                            # OCO 15 + stop-only 15
    assert sorted((s.kind, s.quantity) for s in _live()) == [("OCO", 15), ("STOP", 15)]
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    result = _run(account, [_trim("ABC", -24.0, 6.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED       # not refused for reserved shares
    assert len(broker.delete_calls) == 1 and len(broker.single_delete_calls) == 1   # BOTH kinds cancelled
    assert broker.positions["ABC"] == Decimal(6)
    assert sorted((s.kind, s.quantity) for s in _live()) == [("OCO", 3), ("STOP", 3)]
    assert {s.sl_price for s in _live()} == {45.0}
    assert aps.status_for(aps.get_protection(1, "ABC"), 6.0).code == ap.STATUS_PROTECTED


def test_selling_the_whole_position_disarms_protection_and_keeps_a_history_record(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    result = _run(account, [_trim("ABC", -30.0, 0.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED
    assert broker.positions["ABC"] == Decimal(0)
    assert _live() == []
    p = aps.get_protection(1, "ABC")
    assert not p.enabled and p.tp_targets == [] and p.sl_price == 0.0 and not p.pending_replace
    assert p.disarmed_at is not None and "SL 45" in p.disarmed_note and "TP 60@33%" in p.disarmed_note
    status = aps.status_for(p, 0.0)
    assert status.code == ap.STATUS_OFF and "Disarmed after the position was exited" in status.tooltip
    # a later buy (by hand) is NOT auto-protected: the operator sets TP/SL again on re-entry
    broker.positions["ABC"] = Decimal(9)
    assert aps.reconcile_account(account).extended == []
    assert _live() == []


def test_a_failed_re_placement_after_the_rebalance_flags_the_symbol_unprotected_loudly(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    broker.raise_on_place = TastytradeError("tif_invalid: rejected")      # the re-placement is refused
    result = _run(account, [_trim("ABC", -25.0, 5.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED         # the trade itself went through
    assert broker.positions["ABC"] == Decimal(5)
    p = aps.get_protection(1, "ABC")
    assert p.alert_code == ap.CODE_REPLACE_FAILED and "tif_invalid" in p.alert_message
    assert not p.pending_replace
    assert aps.status_for(p, 5.0).code == ap.STATUS_UNPROTECTED
    failures = [c for c in account.log if c["severity"] == ActivityLogSeverity.FAILURE]
    assert failures and any(ap.CODE_REPLACE_FAILED == c["data"].get("code") for c in failures)
    assert [a.symbol for a in aps.open_alerts(1)] == ["ABC"]              # the page banner lists it


def test_a_fill_observed_while_preparing_drops_the_row_and_restores_protection(world, monkeypatch):
    """F2: a fill that lands after the stale-plan gate and before the cancel means the reviewed plan
    was sized on the PRE-fill position. The row is dropped (SKIPPED, 're-run the dry run'), the fill
    is applied (weight, note, books) and protection is re-placed on what is left."""
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    target = _live()[0].complex_order_id
    original = aps.prepare_for_trade

    def fill_then_prepare(acct, symbols):
        broker.fill(target, "SL")
        return original(acct, symbols)
    monkeypatch.setattr(aps, "prepare_for_trade", fill_then_prepare)
    account.fills = {"ABC": (OrderStatus.FILLED, None, 50.0)}
    result = _run(account, [_trim("ABC", -10.0, 20.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert account.submitted == []                                      # the pre-fill trim was NOT sent
    outcome = result["outcomes"][0]
    assert outcome.status == svc.OUTCOME_SKIPPED and "re-run the dry run" in outcome.message
    assert aps.get_protection(1, "ABC").last_fill_note.startswith("SL hit")
    assert sum(s.quantity for s in _live()) == broker.positions["ABC"]  # protection restored on what is left


def test_a_full_stop_out_during_preparation_does_not_buy_the_symbol_back(world, monkeypatch):
    """F2, the dangerous half: the stop sold everything (weight -> 0) and the reviewed plan wanted to
    BUY it back (or trim from 30). Neither may be sent."""
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    ids = [s.complex_order_id for s in _live()]
    original = aps.prepare_for_trade

    def stop_out_then_prepare(acct, symbols):
        for cid in ids:
            broker.fill(cid, "SL")
        return original(acct, symbols)
    monkeypatch.setattr(aps, "prepare_for_trade", stop_out_then_prepare)
    result = _run(account, [_trim("ABC", -10.0, 20.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert account.submitted == [] and result["outcomes"][0].status == svc.OUTCOME_SKIPPED
    assert broker.positions["ABC"] == Decimal(0)


def test_a_stale_dialog_after_a_fill_is_refused_by_the_existing_position_gate(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    broker.fill(_live()[0].complex_order_id, "SL")                    # the position moved too
    result = _run(account, [_trim("ABC", -10.0, 20.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert result["blocked"] and account.submitted == []


def test_an_unconfirmed_cancel_drops_the_row_and_nothing_is_sold(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    broker.never_confirm_cancel = True
    account.tt._PROTECTION_CANCEL_TIMEOUT_SECONDS = 0.0
    result = _run(account, [_trim("ABC", -10.0, 20.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert account.submitted == []                                    # never sell reserved shares
    outcome = result["outcomes"][0]
    assert outcome.status == svc.OUTCOME_FAILED and "could not be confirmed cancelled" in outcome.message
    assert aps.get_protection(1, "ABC").alert_code == ap.CODE_CANCEL_UNCONFIRMED


def test_symbols_the_run_does_not_trade_keep_their_protection(world):
    broker, account = world
    txn_a = make_open_transaction(1, "ABC", 30.0)
    txn_x = make_open_transaction(1, "XYZ", 20.0)
    _protect(account)
    deletes = len(broker.delete_calls)
    _run(account, [_trim("XYZ", -5.0, 15.0, 20.0)],
         {"ABC": _state(account, "ABC", 30.0, txn_a), "XYZ": _state(account, "XYZ", 20.0, txn_x)})
    assert len(broker.delete_calls) == deletes and sum(s.quantity for s in _live()) == 30


def test_a_symbol_without_protection_is_unaffected(world):
    broker, account = world
    txn_x = make_open_transaction(1, "XYZ", 20.0)
    result = _run(account, [_trim("XYZ", -5.0, 15.0, 20.0)], {"XYZ": _state(account, "XYZ", 20.0, txn_x)})
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED and broker.delete_calls == []


def test_a_raise_mid_submission_still_re_protects_the_position(world, monkeypatch):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)

    def boom(*a, **k):
        raise RuntimeError("submission exploded")
    monkeypatch.setattr(svc, "submit_plan", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        _run(account, [_trim("ABC", -25.0, 5.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    assert broker.positions["ABC"] == Decimal(30)                     # nothing was sold
    assert sum(s.quantity for s in _live()) == 30                     # ...and it is protected again
    assert not aps.get_protection(1, "ABC").pending_replace


def test_an_order_still_working_leaves_the_symbol_replacing_until_the_refresh_settles_it(world):
    broker, account = world
    txn = make_open_transaction(1, "ABC", 30.0)
    _protect(account)
    account.accepted_symbols.add("ABC")                               # accepted, not filled
    _run(account, [_trim("ABC", -25.0, 5.0, 50.0)], {"ABC": _state(account, "ABC", 30.0, txn)})
    p = aps.get_protection(1, "ABC")
    assert p.pending_replace
    assert aps.status_for(p, 30.0).code == ap.STATUS_REPLACING
    assert _live() == []                                              # nothing resting on reserved shares

    # the sell fills at the broker; the next refresh completes the re-placement
    with get_db() as session:
        order = session.exec(select(TradingOrder).where(TradingOrder.symbol == "ABC")).all()[-1]
    order.status = OrderStatus.FILLED
    order.filled_qty = order.quantity
    update_instance(order)
    broker.positions["ABC"] = Decimal(5)
    report = aps.reconcile_account(account.tt.__class__ and account)   # the account (has the capability)
    assert report.resumed == ["ABC"]
    assert sum(s.quantity for s in _live()) == 5 and not aps.get_protection(1, "ABC").pending_replace


def test_an_account_without_the_capability_runs_exactly_as_before():
    account = FakeAccount(account_id=77)
    account.positions = []
    account.prices = {"AAPL": 160.0}
    row = make_row("AAPL", OrderDirection.BUY, 1.0, 160.0, 160.0, price=160.0)
    row.target_quantity = 1.0
    result = svc.run_allocation(account, AllocationPlan(rows=[row], available_buying_power=10_000.0),
                                {}, make_base(), mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert result["outcomes"][0].status == svc.OUTCOME_SUBMITTED
