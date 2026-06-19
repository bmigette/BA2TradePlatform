"""Regression tests for the backtest ENTER_MARKET ruleset seeding (``seed_ruleset_from_tree``).

Locks in two properties the optimizer's S1/entry path depends on:

  1. **No Adjust actions in the entry rule.** At enter_market time the BUY/SELL only stages a
     PENDING order — there is no transaction yet for an Adjust to attach an OCO leg to. Emitting
     an Adjust here sets the transaction's tp/sl field with no working leg AND suppresses the
     engine's ``_apply_initial_brackets`` fallback, so nothing ever closes (100% win rate,
     ``with_TPSL_exit=0``). The entry rule must therefore carry ONLY the open action; the engine
     applies the optimizable initial bracket at transaction-open.
  2. **``enable_short`` adds a symmetric SELL/short rule** (bearish + flat + the SAME gates) so a
     strategy can short — gated downstream by the RM's ``enable_sell`` permission.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_entry_ruleset_seed.py -v
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.services.backtest.seam_wiring import wire_backtest_seams
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest import default_rulesets as dr
from ba2_common.core.models import Ruleset
import ba2_common.core.db as cdb

# A minimal AND tree with one numeric gate (confidence > 50).
_TREE = {"id": "root", "type": "AND", "conditions": [
    {"id": "g1", "field": "confidence", "op": ">", "value": 50},
]}

# Keys that would indicate a TP/SL Adjust action leaked into the entry rule.
_BRACKET_KEYS = {"reference_value", "value", "action_value"}


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("entry-seed-test"):
        yield


def _entry_rules(ruleset_id: int):
    with Session(cdb.get_engine()) as s:
        rs = s.get(Ruleset, ruleset_id)
        return rs.subtype, list(rs.event_actions)


def test_long_only_entry_has_no_adjust_action(_trading_db):
    rid = dr.seed_ruleset_from_tree(_TREE, name="long-only", enable_short=False)
    subtype, eas = _entry_rules(rid)
    assert "ENTER_MARKET" in str(subtype)
    assert len(eas) == 1, "long-only must seed exactly one (BUY) entry rule"

    ea = eas[0]
    assert set((ea.triggers or {}).keys()) >= {"bullish", "no_position", "gate_0"}
    assert list((ea.actions or {}).keys()) == ["buy"]
    buy = ea.actions["buy"]
    assert buy["action_type"] == "buy"
    # The open action carries ONLY action_type — no Adjust/tp/sl leg (the bracket regression).
    assert set(buy.keys()) == {"action_type"}
    assert not (_BRACKET_KEYS & set(buy.keys()))


def test_enable_short_adds_symmetric_sell_rule(_trading_db):
    rid = dr.seed_ruleset_from_tree(_TREE, name="long-short", enable_short=True)
    _, eas = _entry_rules(rid)
    assert len(eas) == 2, "enable_short must seed a BUY rule + a SELL rule"

    by_action = {next(iter(ea.actions.keys())): ea for ea in eas}
    assert set(by_action) == {"buy", "sell"}

    sell = by_action["sell"]
    assert sell.actions["sell"]["action_type"] == "sell"
    # The short rule fires on bearish + flat and carries the SAME optimizer gates as the long rule.
    assert set((sell.triggers or {}).keys()) >= {"bearish", "no_position", "gate_0"}
    # And it, too, must not carry an Adjust leg.
    assert set(sell.actions["sell"].keys()) == {"action_type"}


def test_or_group_entry_tree_emits_one_rule_per_group(_trading_db):
    """A top-level OR of AND-groups (e.g. an imported live ruleset with several alternative entry
    conditions) must seed one BUY rule PER group — preserving OR semantics (ANY group enters).
    Flattening to a single ANDed rule would AND mutually-exclusive gates (long_term AND short_term)
    and never fire."""
    tree = {"id": "root", "type": "OR", "conditions": [
        {"id": "g1", "type": "AND", "conditions": [
            {"id": "g1a", "field": "confidence", "op": ">=", "value": 80}]},
        {"id": "g2", "type": "AND", "conditions": [
            {"id": "g2a", "field": "confidence", "op": ">=", "value": 75},
            {"id": "g2b", "field": "expected_profit", "op": ">=", "value": 10}]},
        {"id": "g3", "type": "AND", "conditions": [
            {"id": "g3a", "field": "confidence", "op": ">=", "value": 85}]},
    ]}
    rid = dr.seed_ruleset_from_tree(tree, name="or-tree", enable_short=False)
    _, eas = _entry_rules(rid)
    assert len(eas) == 3, "OR of 3 groups must seed 3 BUY rules"
    assert all(list(ea.actions.keys()) == ["buy"] for ea in eas)
    # Each rule keeps the base flags; gate counts differ per group (1, 2, 1 numeric gates).
    for ea in eas:
        assert {"bullish", "no_position"} <= set((ea.triggers or {}).keys())


def test_seed_ruleset_from_tree_has_no_entry_bracket_param():
    """The dead ``entry_bracket`` kwarg was removed: the engine's ``_apply_initial_brackets``
    is the single bracket path, so the entry seeder no longer accepts a forward-compat bracket.
    """
    import inspect

    params = inspect.signature(dr.seed_ruleset_from_tree).parameters
    assert "entry_bracket" not in params, (
        "seed_ruleset_from_tree must not carry the dead entry_bracket parameter"
    )
