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


def _sizer(*, balance=20_000.0, cap_pct=10.0, floor=None, sizing=5.0):
    """A BuyCallAction with only what the sizing tail reads. ``cap_pct=None`` means the
    per-instrument cap could not be resolved (``_max_equity_per_instrument_cap`` -> None)."""
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
    assert "min_one_contract" in msg and "per-instrument cap" in msg, msg
    assert "1132.00" in msg and "1000.00" in msg, msg


def test_flag_on_refuses_when_the_cap_cannot_be_resolved():
    """Fail closed: the cap is the ONLY ceiling the floor is allowed to size up to, so an
    unresolvable cap is a refusal, never an uncapped 1 contract."""
    a, submitted = _sizer(floor=True, cap_pct=None)
    res = a._size_and_submit(_TSM)
    assert res["success"] is False and submitted == []
    assert "min_one_contract" in res["message"], res["message"]
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
