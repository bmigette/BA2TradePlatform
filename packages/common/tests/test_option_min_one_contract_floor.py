"""``min_one_contract``: the optional 1-contract sizing floor for option entries (plan
2026-09-24 option-bt-engine-bug-fixes, Task 8).

WHY. Option sizing is ``floor(virtual_equity * option_sizing% / cost_per_contract)``. With
delta 0.3-0.5 contracts on high-priced names the premium is often $6+, so a 1-10% budget
rounds to ZERO contracts and the entry is refused -- the O_LP diagnosis found 18 of 39 (and
104 of 191) submitted entries refused this way, and live 8082 logged
"Insufficient budget to size long_call for TSM (premium=11.32)" the day this was written.

THE RULE. Off by default (every existing run, expert and live rule reproduces exactly). On,
a size that rounds to 0 becomes exactly 1 contract IF that one contract fits under the
per-instrument cap (``max_virtual_equity_per_instrument_percent``) and the virtual equity.
It changes the QUANTITY only, before every later guard (buying power, assignment capacity,
the option RM), so those still judge the 1 contract -- the floor goes THROUGH them, never
around them. When 1 contract does not fit, the entry keeps refusing with its own message,
extended to say the floor was considered and why it did not apply.

It lives in the SHARED sizer (``_OptionEntryAction._size_by_cost``), which every
cost-sized builder reaches in both live and backtest.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core.TradeActions import BuyCallAction, create_action
from ba2_common.core.types import ExpertActionType

from tests.test_option_assignment_capacity_wiring import (  # noqa: F401
    FakeAccount, _own_db, act, held_short_put,
)


# --------------------------------------------------------------------------- #
# the sizer, driven directly (the live O_LC shape: $20k, TSM long call at 11.32)
# --------------------------------------------------------------------------- #
class _Acct:
    def __init__(self, balance):
        self._balance = balance

    def get_option_tradable_balance(self):
        return self._balance


def _sizer(*, balance=20_000.0, cap_pct=10.0, floor=None, sizing=5.0, committed=0.0):
    """A BuyCallAction with only what the sizing tail reads. ``cap_pct=None`` means no
    per-instrument cap (``_max_equity_per_instrument_cap`` -> None); ``committed`` is what
    this expert already has on the name (``_committed_to_underlying``)."""
    a = BuyCallAction.__new__(BuyCallAction)
    a.instrument_name = "TSM"
    a.account = _Acct(balance)
    a.expert_recommendation = None          # _virtual_equity: no instance -> 100%
    a.existing_order = None
    a.sizing = sizing
    if floor is not None:
        a.min_one_contract = floor
    a._max_equity_per_instrument_cap = (
        (lambda equity: None) if cap_pct is None
        else (lambda equity: equity * cap_pct / 100.0))
    a._committed_to_underlying = lambda: (committed, None)
    results, submitted = [], []
    a._result = lambda success, message, data=None: (
        results.append((success, message)) or {"success": success, "message": message})
    a._submit_option_order = lambda legs, quantity, limit, strategy: (
        submitted.append(quantity) or {"success": True, "quantity": quantity})
    return a, submitted


_TSM = SimpleNamespace(
    cost_per_contract=11.32 * 100.0, option_strategy="long_call", legs=[], limit_price=11.32,
    budget_refusal_message="Insufficient budget to size long_call for TSM (premium=11.32)")


@pytest.mark.parametrize("floor", [None, False])
def test_flag_off_a_zero_size_is_refused_exactly_as_before(floor):
    """$20k x 5% = $1,000 against a $1,132 contract -> 0. Unset (an instance built without
    the ctor, i.e. every existing config) and explicit False both refuse, byte-identically."""
    a, submitted = _sizer(floor=floor)
    assert a._size_by_cost(_TSM.cost_per_contract, 5.0) == 0
    res = a._size_and_submit(_TSM)
    assert res == {"success": False,
                   "message": "Insufficient budget to size long_call for TSM (premium=11.32)"}
    assert submitted == []


def test_flag_on_buys_one_contract_that_fits_under_the_cap():
    """Same entry, floor on, 10% cap = $2,000 >= $1,132 -> exactly 1 contract."""
    a, submitted = _sizer(floor=True, cap_pct=10.0)
    assert a._size_by_cost(_TSM.cost_per_contract, 5.0) == 1
    res = a._size_and_submit(_TSM)
    assert res["success"] is True and submitted == [1]


def test_flag_on_never_changes_a_size_that_was_already_at_least_one():
    """A floor, not a bump: 2 stays 2."""
    a, _ = _sizer(floor=True, cap_pct=50.0, sizing=12.0)   # $2,400 / $1,132 -> 2
    assert a._size_by_cost(_TSM.cost_per_contract, 12.0) == 2


def test_flag_on_refuses_when_one_contract_exceeds_the_per_instrument_cap():
    """5% cap = $1,000 < $1,132: the floor may not go around the cap. The refusal keeps the
    structure's own wording and says the floor was considered and why it did not apply."""
    a, submitted = _sizer(floor=True, cap_pct=5.0)
    assert a._size_by_cost(_TSM.cost_per_contract, 5.0) == 0
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    msg = res["message"]
    assert msg.startswith("Insufficient budget to size long_call for TSM (premium=11.32)"), msg
    assert "min_one_contract" in msg and "per-instrument room" in msg, msg
    assert "1132.00" in msg and "1000.00" in msg, msg


def test_flag_on_uses_the_REMAINING_room_not_the_whole_cap():
    """Review 2026-09-25: against the whole cap every floored ticket on one name would get
    equity x pct again. $2,000 cap with $1,000 already committed to TSM leaves $1,000 < one
    $1,132 contract -> refused; with $800 committed, $1,200 of room fits it."""
    a, submitted = _sizer(floor=True, cap_pct=10.0, committed=1_000.0)
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    msg = res["message"]
    assert "remaining per-instrument room 1000.00" in msg, msg
    assert "cap 2000.00" in msg and "1000.00 already committed to TSM" in msg, msg

    a, submitted = _sizer(floor=True, cap_pct=10.0, committed=800.0)
    assert a._size_and_submit(_TSM)["success"] is True and submitted == [1]


def test_flag_on_refuses_when_the_commitment_cannot_be_measured():
    """Unknown is not zero: an unmeasurable commitment on the name refuses the floor."""
    a, submitted = _sizer(floor=True, cap_pct=50.0)
    a._committed_to_underlying = lambda: (None, "transaction 9 (iron_condor) has no reserve")
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "cannot be measured" in res["message"] and "transaction 9" in res["message"], res


def _patch_resolver(monkeypatch, resolve):
    import ba2_common.core.instance_resolver as ir_mod

    class _R:
        def get_expert_instance(self, instance_id):
            return resolve(instance_id)

    monkeypatch.setattr(ir_mod, "get_instance_resolver", lambda: _R())


def test_flag_on_says_setting_unset_when_the_cap_setting_is_absent(monkeypatch):
    """An unset setting is a configuration to fix, not an incident: say so."""
    a, submitted = _sizer(floor=True, cap_pct=None)
    a.expert_recommendation = SimpleNamespace(instance_id=424242, id=None)
    _patch_resolver(monkeypatch, lambda iid: SimpleNamespace(settings={}))
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "min_one_contract" in res["message"], res["message"]
    assert "setting is unset" in res["message"], res["message"]
    assert "could not be resolved" not in res["message"], res["message"]


def test_flag_on_refuses_when_the_cap_cannot_be_resolved(monkeypatch):
    """Fail closed: the cap is the ONLY ceiling the floor is allowed to size up to, so an
    unresolvable cap is a refusal, never an uncapped 1 contract."""
    a, submitted = _sizer(floor=True, cap_pct=None)
    a.expert_recommendation = SimpleNamespace(instance_id=424242, id=None)

    def _boom(iid):
        raise RuntimeError("resolver down")

    _patch_resolver(monkeypatch, _boom)
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "could not be resolved" in res["message"], res["message"]


def test_flag_on_refuses_when_one_contract_exceeds_the_virtual_equity():
    """A cap above 100% cannot license a contract the account cannot pay for."""
    a, submitted = _sizer(balance=1_000.0, floor=True, cap_pct=150.0)
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "virtual equity" in res["message"], res["message"]


@pytest.mark.parametrize("sizing", [0.0, None])
def test_flag_on_does_not_revive_an_entry_whose_sizing_is_off(sizing):
    """option_sizing <= 0 / unset means 'this rule commits no budget'; the floor is for a
    budget too SMALL for one contract, not for no budget at all. No floor note either."""
    a, submitted = _sizer(floor=True, cap_pct=50.0, sizing=sizing)
    assert a._size_by_cost(_TSM.cost_per_contract, sizing) == 0
    res = a._size_and_submit(_TSM)
    assert res["message"] == "Insufficient budget to size long_call for TSM (premium=11.32)"
    assert submitted == []


def test_a_previous_floor_note_never_leaks_into_the_next_sizing():
    a, _ = _sizer(floor=True, cap_pct=5.0)
    a._size_by_cost(_TSM.cost_per_contract, 5.0)
    assert a._min_one_contract_note is not None
    a._size_by_cost(100.0, 5.0)                    # sizes 10 -- no floor involved
    assert a._min_one_contract_note is None


# --------------------------------------------------------------------------- #
# the REAL builders: the floor goes through every later guard
# --------------------------------------------------------------------------- #
def _floored(acct, action_type, cap_pct, **kw):
    a = act(acct, action_type, min_one_contract=True, **kw)
    a._max_equity_per_instrument_cap = lambda equity: equity * cap_pct / 100.0
    a._committed_to_underlying = lambda: (0.0, None)
    return a


def test_a_real_long_call_builder_opens_one_contract_under_the_floor():
    """FakeAccount chain at spot 100: the ATM call asks ~5.2 ($520). $20k x 1% = $200 -> 0."""
    kw = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
              sizing=1.0)
    off = FakeAccount(spot=100.0, balance=20_000.0)
    res = act(off, "buy_call", **kw).execute()
    assert not res["success"] and "Insufficient budget" in res["message"]

    on = FakeAccount(spot=100.0, balance=20_000.0)
    res = _floored(on, "buy_call", 10.0, **kw).execute()
    assert res["success"], res["message"]
    assert on.submitted[-1]["quantity"] == 1


CSP = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
           sizing=25.0)
# 100-strike cash-secured put reserves $10,000 per contract; $15k x 25% = $3,750 -> 0.


def test_a_real_reserve_sized_builder_opens_one_contract_under_the_floor():
    acct = FakeAccount(spot=100.0, balance=15_000.0)
    res = _floored(acct, "sell_cash_secured_put", 80.0, **CSP).execute()
    assert res["success"], res["message"]
    assert acct.submitted[-1]["quantity"] == 1
    assert res["data"]["option_reserve"] == pytest.approx(10_000.0)


def test_the_floored_contract_still_faces_the_buying_power_guard():
    """$8,000 already reserved by a held short straddle leaves $7,000 of option BP: the floor sizes
    1 contract, and the buying-power guard -- which runs AFTER sizing -- refuses it."""
    acct = FakeAccount(spot=100.0, balance=15_000.0)
    acct.hold(held_short_put(strike=50.0, qty=1, reserve=8_000.0, symbol="OTHER"))
    res = _floored(acct, "sell_cash_secured_put", 80.0, **CSP).execute()
    assert not res["success"]
    assert "Insufficient buying power" in res["message"], res["message"]
    assert acct.submitted == []


def test_a_reserve_sized_refusal_names_the_floor_when_the_cap_blocks_it():
    acct = FakeAccount(spot=100.0, balance=15_000.0)
    res = _floored(acct, "sell_cash_secured_put", 50.0, **CSP).execute()   # cap $7,500
    assert not res["success"]
    assert res["message"].startswith("Insufficient budget to size cash_secured_put"), res
    assert "min_one_contract" in res["message"], res["message"]


# --------------------------------------------------------------------------- #
# the parameter: ctor coercion + registration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [
    (None, False), (False, False), (True, True), (0, False), (1, True),
    ("true", True), ("false", False), ("1", True), ("0", False),
])
def test_the_ctor_reads_every_honest_bool_spelling(raw, want):
    """A GA/deploy bool arrives as 1 or "1" as often as True (memory: bool genes stored as
    "1" read back False). Every such spelling must mean what it says."""
    a = create_action(ExpertActionType.BUY_CALL, "AAPL", SimpleNamespace(), SimpleNamespace(),
                      None, None, min_one_contract=raw)
    assert a.min_one_contract is want


def test_the_ctor_refuses_a_spelling_nothing_can_mean():
    with pytest.raises(ValueError):
        BuyCallAction("AAPL", SimpleNamespace(), SimpleNamespace(), min_one_contract="maybe")


def test_the_evaluator_forwards_it_to_the_ctor():
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    assert "min_one_contract" in _OPTION_ENTRY_PARAM_KEYS


def test_the_rule_builder_maps_the_strategy_key_onto_the_action_key():
    from ba2_common.core.rule_builders import action_from_rule
    cfg = action_from_rule({"action_type": "buy_call", "option_sizing": 5.0,
                            "option_min_one_contract": True})["act"]
    assert cfg["min_one_contract"] is True


def test_an_absent_flag_stays_absent_so_old_rules_convert_byte_identically():
    from ba2_common.core.rule_builders import action_from_rule
    cfg = action_from_rule({"action_type": "buy_call", "option_sizing": 5.0})["act"]
    assert "min_one_contract" not in cfg


def test_the_deploy_converter_carries_it_to_the_live_action():
    """trade_rules_to_live_export is what tools/import_deploy_payload.py writes live rules
    from; a GA-fixed True must reach the live EventAction."""
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    rule = {"id": "enter", "actions": [{"action_type": "buy_call", "option_sizing": 5.0,
                                         "option_min_one_contract": True}]}
    out = live_actions_from_trade_rule(rule)
    assert [v["min_one_contract"] for v in out.values()] == [True]


def test_it_is_offered_for_exactly_the_cost_sized_entries():
    """The two overlays size off HELD SHARES (one contract per round lot) and never reach the
    cost sizer, so a floor there is a knob nothing reads."""
    from ba2_common.core.types import (
        get_min_one_contract_action_values, get_option_entry_action_values,
        uses_min_one_contract,
    )
    share_sized = {ExpertActionType.SELL_COVERED_CALL.value,
                   ExpertActionType.BUY_PROTECTIVE_PUT.value}
    assert set(get_min_one_contract_action_values()) == (
        set(get_option_entry_action_values()) - share_sized)
    assert uses_min_one_contract(ExpertActionType.BUY_CALL.value)
    assert not uses_min_one_contract(ExpertActionType.SELL_COVERED_CALL.value)
    assert not uses_min_one_contract(ExpertActionType.CLOSE_OPTION.value)


# --------------------------------------------------------------------------- #
# _committed_to_underlying: what the remaining room subtracts, measured off real rows
# --------------------------------------------------------------------------- #
def _txn(**kw):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    base = dict(symbol="XYZ", quantity=1, side=OrderDirection.BUY,
                status=TransactionStatus.OPENED, expert_id=7)
    base.update(kw)
    return add_instance(Transaction(**base))


def _order(txn_id, **kw):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

    base = dict(account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.BUY,
                order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED, transaction_id=txn_id)
    base.update(kw)
    add_instance(TradingOrder(**base), expunge_after_flush=True)


def _measuring(instance_id=7, symbol="XYZ"):
    a = BuyCallAction.__new__(BuyCallAction)
    a.instrument_name = symbol
    a.expert_recommendation = SimpleNamespace(instance_id=instance_id, id=None)
    return a


def test_the_commitment_counts_shares_premium_and_reserves_on_the_name_only():
    from ba2_common.core.types import AssetClass, OrderDirection, TransactionStatus

    # shares: 10 @ 100 -> 1,000 (the classic RM's own figure)
    _txn(quantity=10, open_price=100.0, asset_class=AssetClass.EQUITY)
    # a filled long call: 2 contracts @ 5.20 -> 1,040 (premium x 100, as the sizer measures)
    _txn(quantity=2, open_price=5.2, asset_class=AssetClass.OPTION,
         option_strategy="long_call", multiplier=100)
    # a WAITING cash-secured put: its reserve stamp, 10,000
    csp = _txn(quantity=1, open_price=None, side=OrderDirection.SELL,
               status=TransactionStatus.WAITING, asset_class=AssetClass.OPTION,
               option_strategy="cash_secured_put", multiplier=100)
    _order(csp, side=OrderDirection.SELL, limit_price=-2.0, status=OrderStatus_PENDING(),
           data={"option_reserve": 10_000.0})
    # NOT counted: another name, another expert, a CLOSED row
    _txn(symbol="OTHER", quantity=10, open_price=100.0, asset_class=AssetClass.EQUITY)
    _txn(expert_id=8, quantity=10, open_price=100.0, asset_class=AssetClass.EQUITY)
    _txn(quantity=10, open_price=100.0, asset_class=AssetClass.EQUITY,
         status=TransactionStatus.CLOSED)

    committed, why = _measuring()._committed_to_underlying()
    assert why is None
    assert committed == pytest.approx(1_000.0 + 1_040.0 + 10_000.0)


def OrderStatus_PENDING():
    from ba2_common.core.types import OrderStatus
    return OrderStatus.PENDING


def test_a_reserving_structure_without_a_reserve_is_unmeasurable_not_zero():
    from ba2_common.core.types import AssetClass, OrderDirection

    txn = _txn(quantity=1, open_price=-1.5, side=OrderDirection.SELL,
               asset_class=AssetClass.OPTION, option_strategy="iron_condor", multiplier=100)
    _order(txn, data={})
    committed, why = _measuring()._committed_to_underlying()
    assert committed is None and "iron_condor" in why and "option_reserve" in why


def test_a_waiting_debit_without_a_fill_is_measured_off_its_order_price():
    from ba2_common.core.types import AssetClass, TransactionStatus

    txn = _txn(quantity=1, open_price=None, status=TransactionStatus.WAITING,
               asset_class=AssetClass.OPTION, option_strategy="long_call", multiplier=100)
    _order(txn, limit_price=3.0, status=OrderStatus_PENDING())
    committed, why = _measuring()._committed_to_underlying()
    assert why is None and committed == pytest.approx(300.0)


def test_the_room_check_refuses_a_second_floored_ticket_on_the_same_name():
    """End to end off real rows: one floored $1,132 ticket already on TSM, cap $2,000 ->
    the next floored ticket on TSM has $868 of room and is refused."""
    from ba2_common.core.types import AssetClass

    _txn(symbol="TSM", quantity=1, open_price=11.32, asset_class=AssetClass.OPTION,
         option_strategy="long_call", multiplier=100)
    a, submitted = _sizer(floor=True, cap_pct=10.0)
    del a._committed_to_underlying                     # the REAL measurement
    a.expert_recommendation = SimpleNamespace(instance_id=7, id=None)
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "remaining per-instrument room 868.00" in res["message"], res["message"]


# --------------------------------------------------------------------------- #
# the floored contract goes THROUGH the option risk manager and the capacity gate
# --------------------------------------------------------------------------- #
class _RMExpert:
    """A classic_options expert: the option RM's rails + the per-instrument cap setting."""

    def __init__(self, rails):
        self.settings = {"risk_manager_mode": "classic_options",
                         "max_virtual_equity_per_instrument_percent": 10.0}
        self._rails = rails

    def get_setting_with_interface_default(self, key, log_warning=True):
        if key not in self._rails:
            raise ValueError(key)
        return self._rails[key]


class _RMAccount(FakeAccount):
    """The capacity harness account plus the two equity reads the option RM makes."""

    def get_account_info(self):
        return {"equity": self._balance, "cash": self._balance, "balance": self._balance}

    def true_equity(self):
        return self._balance


@pytest.fixture
def option_rm(monkeypatch):
    import ba2_common.core.OptionRiskManagement as rm
    import ba2_common.core.TradeActions as ta
    from ba2_common.core.instance_resolver import get_instance_resolver, set_instance_resolver

    rm.reset_state()
    monkeypatch.setattr(rm, "sleeve_structures", lambda eid: ([], []))
    seen = []
    real = ta.admit_option_entry

    def _spy(**kw):
        verdict = real(**kw)
        seen.append((kw["quantity"], verdict.allowed))
        return verdict

    monkeypatch.setattr(ta, "admit_option_entry", _spy)
    previous = get_instance_resolver()

    def _install(rails):
        expert = _RMExpert(rails)
        set_instance_resolver(SimpleNamespace(
            get_expert_instance=lambda iid: expert,
            get_account_instance=lambda aid: None,
            get_account_instance_from_transaction=lambda t: None))
        return seen

    yield _install
    set_instance_resolver(previous)
    rm.reset_state()


_RAILS = {"max_concurrent_structures": 10, "max_deployment_pct": 40.0,
          "max_notional_leverage": 3.0, "undefined_risk_max_pct": 20.0,
          "circuit_breaker_pct": 20.0}
_LONG_CALL = dict(strike_method="percent_otm", strike_param=0.0, dte_min=10, dte_max=40,
                  sizing=1.0)


def _rm_action(acct, **kw):
    a = act(acct, "buy_call", min_one_contract=True, **kw)
    a.expert_recommendation.instance_id = 7
    return a


def test_the_option_rm_receives_quantity_one_from_a_floored_entry(option_rm):
    """$20k x 1% = $200 < the ~$520 ATM call; the 10% cap ($2,000) fits one -> the RM is
    asked about exactly 1 contract, admits it, and 1 is submitted."""
    seen = option_rm(_RAILS)
    acct = _RMAccount(spot=100.0, balance=20_000.0)
    res = _rm_action(acct, **_LONG_CALL).execute()
    assert res["success"], res["message"]
    assert seen == [(1, True)]
    assert acct.submitted[-1]["quantity"] == 1
    assert res["data"]["min_one_contract_floor"] is True


def test_the_option_rm_can_refuse_the_floored_contract(option_rm):
    """A 1% deployment rail ($200) cannot take a ~$520 contract: the floor sized it, the RM
    refuses it -- the floor goes through the rails, never around them."""
    seen = option_rm(dict(_RAILS, max_deployment_pct=1.0))
    acct = _RMAccount(spot=100.0, balance=20_000.0)
    res = _rm_action(acct, **_LONG_CALL).execute()
    assert not res["success"]
    assert seen == [(1, False)]
    assert res["data"].get("option_rm_rail"), res
    assert acct.submitted == []


def test_the_capacity_gate_takes_a_floored_csp_to_zero():
    """$15k, sizing 25% = $3,750 < the 100-strike put's $10,000 reserve; the 80% cap fits
    one, BUT a held short put already owes $10,000 of delivery, so $15k of cash cannot take
    delivery of another -- _downsize_to_delivery_capacity takes the floored 1 to 0."""
    from ba2_common.core.interfaces.OptionsAccountInterface import ASSIGNMENT_CAPACITY_REFUSAL

    acct = FakeAccount(spot=100.0, balance=15_000.0)
    acct.hold(held_short_put(strike=100.0, qty=1, reserve=4_000.0, symbol="OTHER"))
    res = _floored(acct, "sell_cash_secured_put", 80.0, **CSP).execute()
    assert not res["success"]
    assert ASSIGNMENT_CAPACITY_REFUSAL in res["message"], res["message"]
    assert "downsiz" in res["message"].lower(), res["message"]
    assert acct.submitted == []


def test_every_builder_offered_the_floor_really_sizes_by_cost():
    """The floor lives in ``_size_by_cost``; a builder offered it that sized some other way
    would carry an inert knob. Each must reach the cost sizer: directly (``_size_by_cost`` /
    ``_size_by_reserve``) or by returning a ``ResolvedStructure`` with a ``cost_per_contract``,
    which ``_size_and_submit`` sizes through ``_size_by_cost``. And the two it is NOT offered
    to must really size off held shares -- so the exclusion is a fact, not a guess."""
    import inspect

    import ba2_common.core.TradeActions as TA
    from ba2_common.core.types import get_min_one_contract_action_values

    def own_source(cls):
        # The class and its bases BELOW the shared base -- the backspreads inherit their
        # builder from _BackspreadAction, and the shared base's own sizer would prove nothing.
        return "".join(inspect.getsource(k) for k in cls.__mro__
                       if issubclass(k, TA._OptionEntryAction) and k is not TA._OptionEntryAction)

    assert "self._size_by_cost(resolved.cost_per_contract" in inspect.getsource(
        TA._OptionEntryAction._size_and_submit)
    for value in get_min_one_contract_action_values():
        cls = type(create_action(ExpertActionType(value), "AAPL", SimpleNamespace(),
                                 SimpleNamespace(), None, None))
        src = own_source(cls)
        assert ("_size_by_cost(" in src or "_size_by_reserve(" in src
                or "cost_per_contract=" in src), (value, cls.__name__)
    for value in (ExpertActionType.SELL_COVERED_CALL.value,
                  ExpertActionType.BUY_PROTECTIVE_PUT.value):
        cls = type(create_action(ExpertActionType(value), "AAPL", SimpleNamespace(),
                                 SimpleNamespace(), None, None))
        src = own_source(cls)
        assert "_contracts_coverable_by(" in src, value
        assert "_size_by_cost(" not in src and "_size_by_reserve(" not in src, value
