"""StructureShareCondition: pass while a member's share of the sleeve is below X (plan 2026-11-06)."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import ba2_common.core.OptionRiskManagement as rm
import ba2_common.core.TradeConditions as TC
from ba2_common.core.TradeConditions import StructureShareCondition

EID, CSP = 7, 11
AT = datetime(2026, 11, 2, 10, 0)


class _World:
    def _structures(self, eid):
        self.reads += 1
        return list(self.structures), []

    def __init__(self, mp):
        self.structures, self.tags, self.equity = [], {}, 100_000.0
        self.reads = 0
        mp.setattr(rm, "sleeve_structures", self._structures)
        mp.setattr(rm, "transaction_entry_tag", lambda tid: self.tags.get(tid))
        mp.setattr(rm, "assigned_stock_lots", lambda eid: [])
        mp.setattr(rm, "_live_transactions", lambda eid: None)
        self.account = SimpleNamespace(
            get_account_snapshot=lambda: SimpleNamespace(equity=self.equity))


@pytest.fixture
def rig(monkeypatch):
    rm.reset_state()
    w = _World(monkeypatch)
    w.engaged = True
    expert = SimpleNamespace(settings={"risk_manager_mode": "classic_options"})
    monkeypatch.setattr(
        "ba2_common.core.instance_resolver.get_instance_resolver",
        lambda: SimpleNamespace(get_expert_instance=lambda i: expert if w.engaged else
                                SimpleNamespace(settings={"risk_manager_mode": "classic"})))
    errors = []
    monkeypatch.setattr(TC.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    w.errors = errors
    yield w
    rm.reset_state()


def _put(txn_id, strike):
    from datetime import date
    from ba2_common.core.option_lifecycle import LifecycleLeg, OptionStructure
    from ba2_common.core.types import OptionRight
    return OptionStructure(
        transaction_id=txn_id, underlying="AAPL", strategy="cash_secured_put",
        legs=(LifecycleLeg(contract_symbol=f"AAPL270115P{strike}", net_qty=-1.0, strike=strike,
                           option_type=OptionRight.PUT, expiry=date(2027, 1, 15),
                           underlying="AAPL", entry_premium=2.0),),
        quantity=1.0, multiplier=100, entry_net_premium=-2.0)


def cond(w, value, tag=CSP, op="<", created_at=AT):
    c = StructureShareCondition(
        account=w.account, instrument_name="AAPL",
        expert_recommendation=SimpleNamespace(instance_id=EID, created_at=created_at),
        operator_str=op, value=value)
    c.rule_entry_tag = tag
    return c


def test_passes_while_the_share_is_below_the_limit(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}       # 20_000 / 100_000 = 20%
    c = cond(rig, 30.0)
    assert c.evaluate() is True
    assert c.get_calculated_value() == pytest.approx(20.0)


def test_fails_at_and_above_the_limit_strictly(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}
    assert cond(rig, 20.0).evaluate() is False                  # '<' is strict
    assert cond(rig, 10.0).evaluate() is False


def test_an_empty_member_always_passes(rig):
    assert cond(rig, 20.0).evaluate() is True


def test_no_member_tag_on_the_rule_is_false_and_loud(rig):
    assert cond(rig, 30.0, tag=None).evaluate() is False
    assert any("entry action" in e for e in rig.errors)


def test_an_expert_not_on_the_option_risk_manager_is_false_and_loud(rig):
    rig.engaged = False
    assert cond(rig, 30.0).evaluate() is False
    assert any("classic_options" in e for e in rig.errors)


def test_a_missing_decision_time_is_false_and_loud(rig):
    assert cond(rig, 30.0, created_at=None).evaluate() is False
    assert any("created_at" in e for e in rig.errors)


def test_an_unmeasurable_member_is_false_and_the_error_names_the_transaction(rig):
    from ba2_common.core.option_lifecycle import OptionStructure
    bad = OptionStructure(transaction_id=42, underlying="AAPL", strategy="cash_secured_put",
                          legs=(), quantity=1.0, multiplier=100, entry_net_premium=-2.0)
    rig.structures, rig.tags = [bad], {42: CSP}
    c = cond(rig, 30.0)
    assert c.evaluate() is False
    assert c.get_calculated_value() is None
    assert any("transaction 42" in e for e in rig.errors)


def test_bind_event_action_takes_the_tag_from_the_rules_entry_action(rig):
    c = StructureShareCondition(rig.account, "AAPL", None, "<", 30.0)
    c.bind_event_action(SimpleNamespace(actions={"a0": {"action_type": "buy_call",
                                                        "entry_tag": 19}}))
    assert c.rule_entry_tag == 19


def test_description_and_display(rig):
    rig.structures, rig.tags = [_put(1, 200.0)], {1: CSP}
    c = cond(rig, 30.0)
    c.evaluate()
    assert "structure" in c.get_description().lower() and "< 30" in c.get_description()
    assert c.get_actual_value_display() == "member 11: 20.0% of sleeve"


def test_one_cycle_with_different_seconds_per_candidate_builds_the_share_once(rig):
    """Live recommendations of one pass carry different wall-clock created_at; the stamp is
    floored to the minute so the per-cycle cache still hits (one build, not one per candidate)."""
    for sec, micro in ((0, 0), (7, 123456), (41, 999999), (59, 1)):
        at = datetime(2026, 11, 2, 10, 0, sec, micro)
        assert cond(rig, 30.0, created_at=at).evaluate() is True
    assert rig.reads == 1
    cond(rig, 30.0, created_at=datetime(2026, 11, 2, 10, 1, 3)).evaluate()   # next minute
    assert rig.reads == 2


def test_a_timezone_aware_decision_time_is_floored_too(rig):
    from datetime import timezone
    a = cond(rig, 30.0, created_at=datetime(2026, 11, 2, 10, 0, 5, tzinfo=timezone.utc))
    b = cond(rig, 30.0, created_at=datetime(2026, 11, 2, 10, 0, 50, tzinfo=timezone.utc))
    assert a.evaluate() is True and b.evaluate() is True

