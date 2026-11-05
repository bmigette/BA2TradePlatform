"""Per-action capture record for option entries and closes (option selection replay input).

Inside a capture scope each option ``execute`` records ONE ``option_action`` observation: the
inputs that rebuild the action, the DB rows it reads, and the outcome (what it asked the account
to submit, the entry/exit record, the result or refusal). Outside a scope, or in replay mode, it
is a no-op and behaviour is identical.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ba2_common.core import TradeActions
from ba2_common.core.TradeActions import CloseOptionAction, create_action
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.models import (
    ExpertInstance, ExpertRecommendation, ExpertSetting, TradingOrder, Transaction,
)
from ba2_common.core.replay import codec, option_action_record as oar
from ba2_common.core.replay.context import (
    CaptureContext, CaptureHealth, capture_scope, use_capture_context,
)
from ba2_common.core.replay.schemas import ReplayStatus
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OptionCloseReason, OptionRight, OrderDirection,
    OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
)
from tests.test_max_loss_persisted_at_submit import (  # noqa: F401
    BPS, EXPIRY, FakeAccount, _own_db,
)

UTC_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()
        self.submitted = []

    def submit(self, record, objects=None, observations=()):
        self.submitted.append((record, dict(objects or {}), list(observations)))


def _meta():
    return {
        "analysis_id": "a1", "attempt_id": "att-1", "session_id": "s1",
        "expert_class": "TradeManager", "expert_instance_id": 7, "symbol": "XYZ",
        "use_case": ReplayStatus.USE_CASE_ENTER_MARKET,
        "scheduled_at": UTC_NOW, "started_at": UTC_NOW,
    }


def _payload(pending):
    encoded = codec.encode(pending.payload)
    return codec.decode(encoded.kind, encoded.data, encoded.meta)


def _action_obs(store):
    return [o for o in store.submitted[0][2] if o.observation.provider == "option_action"]


def _rec(**over):
    fields = dict(id=1, instance_id=None, symbol="XYZ", data=None, price_at_date=100.0,
                  expected_profit_percent=5.0, recommended_action=OrderRecommendation.BUY)
    fields.update(over)
    return SimpleNamespace(**fields)


def _entry(acct, action_type="open_bull_put_spread", rec=None, **kw):
    kw = kw or dict(BPS)
    a = create_action(ExpertActionType(action_type), "XYZ", acct, OrderRecommendation.BUY,
                      None, rec or _rec(), **kw)
    a.submit_to_broker = True
    return a


def _result_shape(result):
    return (result["success"], result["message"], sorted(result["data"]))


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------
def test_entry_records_inputs_and_the_legs_quantity_limit_the_account_received():
    acct = FakeAccount()
    store = _Store()
    with capture_scope(store, _meta()):
        result = _entry(acct).execute()
    assert result["success"], result["message"]
    (obs,) = _action_obs(store)
    assert obs.observation.method == "OpenBullPutSpreadAction"
    assert obs.observation.request_identity == {
        "action_class": "OpenBullPutSpreadAction", "expert_instance_id": None, "symbol": "XYZ",
        "transaction_id": None, "recommendation_id": 1}
    payload = _payload(obs)
    sub = acct.submitted[-1]
    (recorded,) = payload["outcome"]["submits"]
    assert recorded["method"] == "submit_option_order"
    assert recorded["quantity"] == sub["quantity"]
    assert recorded["limit_price"] == sub["limit_price"]
    assert recorded["option_strategy"] == sub["strategy"]
    assert [l.contract_symbol for l in recorded["legs"]] == [l.contract_symbol for l in sub["legs"]]
    assert [s["contract_symbol"] for s in recorded["leg_summary"]] == \
        [l.contract_symbol for l in sub["legs"]]
    assert payload["outcome"]["status"] == "returned"
    assert payload["outcome"]["success"] is True
    assert payload["outcome"]["entry_record"] == result["data"]["entry_record"]
    assert payload["inputs"]["decision_time"].tzinfo is not None
    assert oar.replay_decision_time(payload) == payload["inputs"]["decision_time"]


def test_a_refused_entry_records_the_refusal_message():
    acct = FakeAccount()
    store = _Store()
    kw = dict(BPS, dte_min=500, dte_max=600)
    with capture_scope(store, _meta()):
        result = _entry(acct, **kw).execute()
    assert not result["success"]
    payload = _payload(_action_obs(store)[0])
    assert payload["outcome"]["success"] is False
    assert payload["outcome"]["message"] == result["message"]
    assert payload["outcome"]["submits"] == []


def test_outside_a_scope_nothing_is_recorded_and_the_result_is_identical():
    plain = FakeAccount()
    unrecorded = _entry(plain).execute()
    assert getattr(oar._tls, "frame", None) is None
    store = _Store()
    recorded_acct = FakeAccount()
    with capture_scope(store, _meta()):
        recorded = _entry(recorded_acct).execute()
    assert _result_shape(unrecorded) == _result_shape(recorded)
    assert plain.submitted == recorded_acct.submitted
    assert len(_action_obs(store)) == 1


def test_replay_mode_records_nothing():
    context = CaptureContext.for_replay(
        analysis_id="r1", clock_reads=[UTC_NOW.isoformat()] * 5)
    acct = FakeAccount()
    with use_capture_context(context):
        assert oar.record_option_action(_entry(acct)) is oar._NOOP_CONTEXT
        result = _entry(acct).execute()
    assert result["success"]
    assert getattr(oar._tls, "frame", None) is None


def test_the_actions_own_exception_propagates_and_is_recorded_as_error(monkeypatch):
    acct = FakeAccount()
    store = _Store()
    action = _entry(acct)

    def boom():
        raise RuntimeError("builder blew up")

    monkeypatch.setattr(action, "_execute_entry", boom)
    with capture_scope(store, _meta()):
        with pytest.raises(RuntimeError, match="builder blew up"):
            action.execute()
    outcome = _payload(_action_obs(store)[0])["outcome"]
    assert outcome["status"] == "error"
    assert "builder blew up" in outcome["error"]
    assert getattr(oar._tls, "frame", None) is None          # slot cleared on the exception


def test_the_submit_slot_does_not_leak_between_consecutive_or_nested_actions():
    acct = FakeAccount()
    store = _Store()
    with capture_scope(store, _meta()):
        first = _entry(acct)
        second = _entry(acct, rec=_rec(id=2))
        first.execute()
        assert getattr(oar._tls, "frame", None) is None
        # nested, DIFFERENT action objects: the inner one gets its own frame, the outer resumes
        with oar.record_option_action(first) as outer:
            oar.note_submit("submit_option_order", legs=[], quantity=1)
            second.execute()
            assert oar._tls.frame.action is first
            assert len(oar._tls.frame.submits) == 1
            outer.result = {"success": True, "message": "outer", "data": {}}
    observations = _action_obs(store)
    assert len(observations) == 3
    by_recommendation = {}
    for obs in observations:
        by_recommendation.setdefault(obs.observation.request_identity["recommendation_id"], []) \
            .append(_payload(obs)["outcome"]["submits"])
    assert [len(s) for s in by_recommendation[2]] == [1]       # the inner entry: its own submit only
    assert sorted(len(s) for s in by_recommendation[1]) == [1, 1]


def test_a_recording_failure_is_counted_and_never_changes_the_result(monkeypatch):
    clean = _entry(FakeAccount()).execute()
    store = _Store()
    acct = FakeAccount()
    monkeypatch.setattr(oar, "_build_inputs", lambda action: (_ for _ in ()).throw(KeyError("x")))
    with capture_scope(store, _meta()):
        result = _entry(acct).execute()
    assert _result_shape(result) == _result_shape(clean)
    assert store.health.other == 1
    assert _action_obs(store) == []


def test_an_outcome_recording_failure_is_counted_and_the_result_is_untouched(monkeypatch):
    store = _Store()
    acct = FakeAccount()
    monkeypatch.setattr(oar, "record_observation",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("disk")))
    with capture_scope(store, _meta()):
        result = _entry(acct).execute()
    assert result["success"]
    assert store.health.other == 1
    assert getattr(oar._tls, "frame", None) is None


def test_covered_call_decline_is_recorded_once_by_the_wrapper():
    acct = FakeAccount()
    store = _Store()
    action = create_action(ExpertActionType.SELL_COVERED_CALL, "XYZ", acct,
                           OrderRecommendation.SELL, None, _rec(),
                           strike_method="percent_otm", strike_param=5.0, dte_min=10, dte_max=40)
    action.submit_to_broker = True
    with capture_scope(store, _meta()):
        result = action.execute()
    assert not result["success"]
    (obs,) = _action_obs(store)
    assert obs.observation.method == "SellCoveredCallAction"
    assert _payload(obs)["outcome"]["message"] == result["message"]


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------
def test_recorded_inputs_rebuild_an_action_with_the_same_config_and_result():
    kw = dict(BPS, min_open_interest=10, max_spread_pct=50.0, wing_width_pct=2.0,
              entry_cross=0.25, w_premium=0.5, min_one_contract=True, entry_tag=3)
    acct = FakeAccount()
    acct.next_order_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.PENDING))
    store = _Store()
    original = _entry(acct, rec=_rec(data={"FMPRating": {"target_consensus": 130.0}}), **kw)
    original.close_percent = 40
    with capture_scope(store, _meta()):
        result = original.execute()
    payload = _payload(_action_obs(store)[0])
    other = FakeAccount()
    other.next_order_id = acct.next_order_id
    rebuilt = oar.rebuild_option_action(payload, other)
    assert type(rebuilt) is type(original)
    assert oar.action_ctor_kwargs(rebuilt) == oar.action_ctor_kwargs(original)
    assert oar.action_ctor_kwargs(rebuilt)["entry_tag"] == 3
    assert rebuilt.selection_policy == original.selection_policy
    assert rebuilt.instrument_name == original.instrument_name
    assert rebuilt.order_recommendation == original.order_recommendation
    assert rebuilt.close_percent == 40
    assert rebuilt.submit_to_broker is True
    for name in ("id", "instance_id", "symbol", "recommended_action", "price_at_date", "data"):
        assert getattr(rebuilt.expert_recommendation, name) == getattr(original.expert_recommendation, name)
    again = rebuilt.execute()
    assert _result_shape(again) == _result_shape(result)
    assert other.submitted == acct.submitted


def test_rebuild_refuses_a_class_outside_the_two_option_families():
    payload = {"inputs": {"action_class": "BuyAction"}}
    with pytest.raises(ValueError, match="not an option entry/close action"):
        oar.rebuild_option_action(payload, FakeAccount())
    assert TradeActions.BuyAction  # the class exists; it is the family check that refuses


# ---------------------------------------------------------------------------
# db rows
# ---------------------------------------------------------------------------
def test_db_rows_hold_the_expert_its_settings_the_recommendation_and_its_transactions():
    account_id = 1
    expert_id = add_instance(ExpertInstance(account_id=account_id, expert="X", virtual_equity_pct=80.0))
    add_instance(ExpertSetting(instance_id=expert_id, key="max_virtual_equity_per_instrument_percent",
                               value_float=12.0, value_str=None))
    add_instance(ExpertSetting(instance_id=expert_id, key="openai_api_key",
                               value_str="sk-secret", value_float=None))
    rec_id = add_instance(ExpertRecommendation(
        instance_id=expert_id, symbol="XYZ", recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=5.0, price_at_date=100.0, details=None, confidence=70.0,
        risk_level="MEDIUM", time_horizon="SHORT_TERM"))
    txn_id = add_instance(Transaction(symbol="XYZ", quantity=1, side=OrderDirection.BUY,
                                      status=TransactionStatus.OPENED, expert_id=expert_id))
    order_id = add_instance(TradingOrder(account_id=1, symbol="XYZ", quantity=1,
                                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                         status=OrderStatus.FILLED, transaction_id=txn_id))
    # Collected directly: executing with an expert id needs the host's instance resolver.
    rows, redacted = oar._collect_db_rows(_entry(FakeAccount(), rec=_rec(id=rec_id, instance_id=expert_id)))
    payload = {"db_rows": rows, "inputs": {"redacted_setting_keys": redacted}}
    models = {(r["model"], r["row"]["id"]) for r in payload["db_rows"]}
    assert ("ExpertInstance", expert_id) in models
    assert ("ExpertRecommendation", rec_id) in models
    assert ("Transaction", txn_id) in models
    assert ("TradingOrder", order_id) in models
    keys = {r["row"]["key"] for r in payload["db_rows"] if r["model"] == "ExpertSetting"}
    assert keys == {"max_virtual_equity_per_instrument_percent"}
    assert payload["inputs"]["redacted_setting_keys"] == ["openai_api_key"]
    instances = oar.db_row_instances(payload)
    assert {type(i).__name__ for i in instances} >= {"ExpertInstance", "TradingOrder"}


# ---------------------------------------------------------------------------
# closes
# ---------------------------------------------------------------------------
def _held_order(txn_id=None):
    return TradingOrder(
        id=11, account_id=1, symbol="AAPL", underlying_symbol="AAPL", quantity=2, filled_qty=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        open_price=1.5, asset_class=AssetClass.OPTION, contract_symbol="AAPL261218P00200000",
        option_type=OptionRight.PUT, strike=200.0, expiry=date(2026, 12, 18),
        transaction_id=txn_id)


def _close_action(txn_id=None):
    account = MagicMock(spec=OptionsAccountInterface)
    account.get_option_quote.return_value = None
    account.close_option_position.return_value = SimpleNamespace(id=99)
    action = CloseOptionAction(
        instrument_name="AAPL", account=account, order_recommendation=OrderRecommendation.SELL,
        existing_order=_held_order(txn_id), expert_recommendation=_rec(id=5, symbol="AAPL"),
        forced_exit=True, close_trigger=OptionCloseReason.MANUAL, rule_id=9, rule_name="r",
        close_target=None)
    action.create_and_save_action_result = lambda **kw: {
        "success": kw["success"], "message": kw["message"], "data": dict(kw["data"])}
    action._stamp_exit_record = lambda *a, **k: oar.note_record("exit_record", {"stub": True})
    return action, account


def test_a_close_records_the_order_the_position_limit_and_transaction():
    action, account = _close_action(txn_id=42)
    store = _Store()
    with capture_scope(store, _meta()):
        result = action.execute()
    assert result["success"], result["message"]
    (obs,) = _action_obs(store)
    assert obs.observation.request_identity["transaction_id"] == 42
    payload = _payload(obs)
    (submit,) = payload["outcome"]["submits"]
    assert submit["method"] == "close_option_position"
    call = account.close_option_position.call_args
    assert submit["position"] == call.args[0]
    assert submit["limit_price"] == call.kwargs["limit_price"]
    assert submit["transaction_id"] == 42
    assert payload["outcome"]["exit_record"] == {"stub": True}
    assert payload["inputs"]["existing_order"]["contract_symbol"] == "AAPL261218P00200000"
    assert {(r["model"], r["row"]["id"]) for r in payload["db_rows"]} >= {("TradingOrder", 11)}
    kwargs = payload["inputs"]["ctor_kwargs"]
    assert kwargs == {"forced_exit": True, "close_target": None,
                      "close_trigger": OptionCloseReason.MANUAL, "rule_id": 9, "rule_name": "r"}
    rebuilt = oar.rebuild_option_action(payload, MagicMock(spec=OptionsAccountInterface))
    assert type(rebuilt) is CloseOptionAction
    assert oar.action_ctor_kwargs(rebuilt) == oar.action_ctor_kwargs(action)
    assert rebuilt.existing_order.contract_symbol == action.existing_order.contract_symbol
    assert rebuilt.forced_exit is True


def test_a_close_outside_a_scope_is_unchanged():
    action, account = _close_action(txn_id=42)
    result = action.execute()
    assert result["success"]
    account.close_option_position.assert_called_once()
    assert getattr(oar._tls, "frame", None) is None
