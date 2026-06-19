"""Reconciliation regression: an API/frontend-shaped exit rule must seed correctly.

The API/UI emit exit rules in the shape ``{action, comparison, action_value}`` (e.g.
``action="adjust_stop_loss"``, leaf ``comparison=">="``), while the canonical EventAction shape
the ``TradeActionEvaluator`` parses uses ``{action_type, operator, value}``. Before the refactor
to the shared ``ba2_common.core.rule_builders`` core, ``seed_open_positions_ruleset`` read
``rule['action_type']`` and ``leaf['op']/'operator']`` only — so an API-shaped rule was SILENTLY
SKIPPED (no EventAction seeded) and a numeric leaf's ``comparison`` operator was lost (defaulted
to ``>``). This test pins that the API shape now seeds a faithful EventAction.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_exit_rule_shape_reconciliation.py -v
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.services.backtest.seam_wiring import wire_backtest_seams
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest.default_rulesets import seed_open_positions_ruleset
from ba2_common.core.models import Ruleset
import ba2_common.core.db as cdb


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("exit-shape-test"):
        yield


def test_api_shape_exit_rule_seeds_action_and_operator(_trading_db):
    rid = seed_open_positions_ruleset([{
        "id": "r1",
        "action": "adjust_stop_loss",
        "reference_value": "order_open_price",
        "action_value": -8.0,
        "conditions": {"id": "g", "operator": "AND", "conditions": [
            {"id": "c", "field": "profit_loss_percent", "comparison": ">=", "value": 5},
        ]},
    }])

    with Session(cdb.get_engine()) as s:
        rs = s.get(Ruleset, rid)
        assert "OPEN_POSITIONS" in str(rs.subtype)
        eas = list(rs.event_actions)
        # The API-shaped rule must NOT be silently skipped.
        assert len(eas) == 1, "API-shaped exit rule must seed exactly one EventAction"
        ea = eas[0]

        # action: 'action' alias must resolve to action_type adjust_stop_loss with the
        # reference_value + value (the % offset) carried through (value/action_value reconciled).
        actions = ea.actions or {}
        assert len(actions) == 1
        act = next(iter(actions.values()))
        assert act["action_type"] == "adjust_stop_loss"
        assert act["reference_value"] == "order_open_price"
        assert act["value"] == -8.0

        # trigger: the leaf's 'comparison' operator must survive (NOT defaulted to '>'),
        # carrying the numeric event_type + value 5.
        triggers = ea.triggers or {}
        assert len(triggers) == 1
        trig = next(iter(triggers.values()))
        assert trig["event_type"] == "profit_loss_percent"
        assert trig["operator"] == ">=", "leaf 'comparison' must reconcile, not default to '>'"
        assert trig["value"] == 5


def test_option_action_exit_rule_seeds_option_event_action(_trading_db):
    """An OPTION-action exit rule (e.g. ``buy_call`` with option selection params) must seed an
    EventAction whose action carries the option ``action_type`` + selection params in the EXACT
    keys the ``TradeActionEvaluator`` reads — so the engine builds the option TradeAction
    (BuyCallAction etc.) in the backtest, same as live. Before option actions were wired into the
    shared ``action_from_rule``, this rule produced NO action and was silently skipped."""
    rid = seed_open_positions_ruleset([{
        "id": "opt1",
        "action": "buy_call",
        "option_strike_method": "delta",
        "option_strike_param": 0.3,
        "option_dte_min": 20,
        "option_dte_max": 45,
        "option_sizing": 5.0,
        "conditions": {"id": "g", "operator": "AND", "conditions": [
            {"id": "c", "field": "bullish"},
        ]},
    }])

    with Session(cdb.get_engine()) as s:
        rs = s.get(Ruleset, rid)
        assert "OPEN_POSITIONS" in str(rs.subtype)
        eas = list(rs.event_actions)
        # The option-action rule must NOT be silently skipped.
        assert len(eas) == 1, "option-action exit rule must seed exactly one EventAction"
        ea = eas[0]

        actions = ea.actions or {}
        assert len(actions) == 1
        act = next(iter(actions.values()))
        # Option action_type + selection params in the evaluator's keys.
        assert act["action_type"] == "buy_call"
        assert act["strike_method"] == "delta"
        assert act["strike_param"] == 0.3
        assert act["dte_min"] == 20
        assert act["dte_max"] == 45
        assert act["sizing"] == 5.0

        # The flag trigger survives.
        triggers = ea.triggers or {}
        assert len(triggers) == 1
        trig = next(iter(triggers.values()))
        assert trig["event_type"] == "bullish"
