"""The account EFFECT taps: one decision is one effect, however the broker nests its calls.

``adjust_tp`` -> ``adjust_tp_sl`` (Alpaca), ``close_transaction`` -> ``submit_order`` and
``submit_order`` -> ``adjust_tp_sl`` (the base bracket set-up) are nested effect calls; only the
OUTERMOST is recorded. Taps record only inside a capture scope AND into a frame that accepts account
effects; everywhere else they are a passthrough that changes nothing.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.models import TradingOrder, Transaction
from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_common.core.replay import action_record as core, codec
from ba2_common.core.replay.account_taps import EFFECT_METHODS, effect_boundary
from ba2_common.core.replay.context import (
    CaptureContext, capture_scope, current_capture, use_capture_context,
)
from ba2_common.core.replay.schemas import ReplayStatus
from tests._account_tap_doubles import RecordingStore, StubAccount

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


def _meta():
    return {
        "analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "X", "expert_instance_id": 7, "symbol": "XYZ",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW, "started_at": UTC_NOW,
    }


def _txn(txn_id):
    return Transaction(id=txn_id, symbol="XYZ", quantity=1.0, side=OrderDirection.BUY,
                       status=TransactionStatus.OPENED)


def _order(order_id):
    return TradingOrder(id=order_id, account_id=1, symbol="XYZ", quantity=1.0, side=OrderDirection.BUY,
                        order_type=OrderType.MARKET, status=OrderStatus.PENDING)


class _Nesting(StubAccount):
    """adjust_tp / adjust_sl delegate to adjust_tp_sl (as AlpacaAccount does); submit_order sets the
    bracket through adjust_tp_sl; close_transaction submits an order."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        self.calls.append("submit_order")
        if tp_price is not None:
            self.adjust_tp_sl(_txn(9), tp_price, sl_price, source="initial_setup")
        return trading_order

    def close_transaction(self, transaction_id):
        self.calls.append("close_transaction")
        self.submit_order(_order(2), is_closing_order=True)
        return {"success": True}

    def reduce_transaction(self, transaction_id, quantity):
        self.calls.append("reduce_transaction")
        return {"success": True, "quantity": quantity}

    def adjust_tp(self, transaction, new_tp_price, source=""):
        self.calls.append("adjust_tp")
        return self.adjust_tp_sl(transaction, new_tp_price=new_tp_price, new_sl_price=None, source=source)

    def adjust_sl(self, transaction, new_sl_price, source=""):
        self.calls.append("adjust_sl")
        return self.adjust_tp_sl(transaction, new_tp_price=None, new_sl_price=new_sl_price, source=source)

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        self.calls.append("adjust_tp_sl")
        if new_tp_price == -1:
            raise RuntimeError("broker refused")
        return True


class _Step(core.FrameRecorder):
    provider = "toy_step"
    version = 1
    family = "toy step"
    accepts_account_effects = True

    def method_name(self): return "toy"
    def identity(self): return {}
    def build_inputs(self): return {}
    def collect_db_rows(self): return [], []


class _OptionLikeStep(_Step):
    accepts_account_effects = False


def _effects(store):
    (obs,) = [o for o in store.submitted[0][2] if o.observation.provider == "toy_step"]
    encoded = codec.encode(obs.payload)
    return codec.decode(encoded.kind, encoded.data, encoded.meta)["outcome"]["submits"]


def _run(body, step_class=_Step):
    store = RecordingStore()
    with capture_scope(store, _meta()):
        with step_class(SimpleNamespace(), current_capture()):
            body()
    return store


def test_every_effect_method_of_the_stub_account_is_tapped():
    for name in EFFECT_METHODS:
        assert effect_boundary(getattr(_Nesting, name)) == name, name


def test_adjust_tp_through_adjust_tp_sl_records_one_effect():
    account = _Nesting()
    store = _run(lambda: account.adjust_tp(_txn(5), 12.5, source="ruleset"))
    assert account.calls == ["adjust_tp", "adjust_tp_sl"]          # the nested call still ran
    (effect,) = _effects(store)
    assert effect["method"] == "adjust_tp"
    assert effect["call"] == {"transaction": {"transaction_id": 5}, "new_tp_price": 12.5,
                              "source": "ruleset"}
    assert effect["returned"] is True


def test_adjust_sl_and_a_direct_adjust_tp_sl_each_record_one_effect():
    account = _Nesting()

    def body():
        account.adjust_sl(_txn(5), 8.0, source="ruleset")
        account.adjust_tp_sl(_txn(5), 12.0, 8.0, source="ruleset")

    store = _run(body)
    assert [e["method"] for e in _effects(store)] == ["adjust_sl", "adjust_tp_sl"]


def test_close_transaction_that_submits_an_order_records_only_the_close():
    account = _Nesting()
    store = _run(lambda: account.close_transaction(41))
    assert account.calls == ["close_transaction", "submit_order"]
    (effect,) = _effects(store)
    assert effect["method"] == "close_transaction"
    assert effect["call"] == {"transaction_id": 41}
    assert effect["returned"] == {"success": True}


def test_submit_order_with_a_bracket_records_only_the_submit_and_snapshots_its_arguments():
    account = _Nesting()
    store = _run(lambda: account.submit_order(_order(3), tp_price=11.0, sl_price=9.0))
    assert account.calls == ["submit_order", "adjust_tp_sl"]
    (effect,) = _effects(store)
    assert effect["method"] == "submit_order"
    assert (effect["call"]["tp_price"], effect["call"]["sl_price"], effect["call"]["is_closing_order"]) == (
        11.0, 9.0, False)


def test_reduce_transaction_records_its_arguments_and_return():
    account = _Nesting()
    store = _run(lambda: account.reduce_transaction(7, 2.5))
    (effect,) = _effects(store)
    assert effect == {"method": "reduce_transaction", "call": {"transaction_id": 7, "quantity": 2.5},
                      "returned": {"success": True, "quantity": 2.5}}


def test_a_refusal_is_recorded_with_its_error_and_reraised_unchanged():
    account = _Nesting()

    def body():
        with pytest.raises(RuntimeError, match="broker refused"):
            account.adjust_tp(_txn(5), -1, source="ruleset")
        # the guard flag is released: the next top-level effect is recorded again
        account.reduce_transaction(7, 1.0)

    store = _run(body)
    first, second = _effects(store)
    assert first["method"] == "adjust_tp" and "broker refused" in first["error"] and "returned" not in first
    assert second["method"] == "reduce_transaction"


def test_effects_outside_a_capture_scope_are_a_pure_passthrough():
    account = _Nesting()
    assert account.adjust_tp(_txn(5), 12.5) is True
    assert account.close_transaction(1) == {"success": True}
    assert account.calls == ["adjust_tp", "adjust_tp_sl", "close_transaction", "submit_order"]
    assert getattr(core._tls, "frame", None) is None


def test_effects_in_a_scope_without_an_accepting_frame_record_nothing():
    account = _Nesting()
    store = _run(lambda: account.adjust_tp(_txn(5), 12.5), step_class=_OptionLikeStep)
    assert _effects(store) == []

    store = RecordingStore()                       # no frame at all
    with capture_scope(store, _meta()):
        assert account.adjust_tp(_txn(5), 1.0) is True
    assert all(o.observation.provider != "toy_step" for o in store.submitted[0][2])


def test_replay_mode_never_records_effects():
    account = _Nesting()
    context = CaptureContext.for_replay(analysis_id="a1", clock_reads=[])
    with use_capture_context(context):
        with core.replay_effects(SimpleNamespace(), accepts_account_effects=True) as frame:
            assert account.adjust_tp(_txn(5), 12.5) is True
    assert frame.submits == []


def test_an_unrecordable_argument_is_counted_and_never_changes_the_call():
    class Boom:
        id = 1

        def model_dump(self):
            raise RuntimeError("dump failed")

    account = _Nesting()
    store = _run(lambda: account.submit_order(Boom()))
    assert account.calls == ["submit_order"]
    assert store.health.other >= 1
