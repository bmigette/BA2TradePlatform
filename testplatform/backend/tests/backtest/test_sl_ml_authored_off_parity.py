"""Parity: a rule carrying ``enabled: False`` produces NO action on EITHER runtime the launcher
feeds -- the backtest seeder (``default_rulesets.seed_ruleset_from_rules``) and the live export
path (``rules_convert.trade_rules_to_live_export``). Reviewer finding (2026-09-02): before the
fix, ``rules_convert.live_actions_from_trade_rule`` (the ONE function both paths call) did not
consult the ``enabled`` key at all, so a default-genome trial, an unsearched run and a seeded
live deploy all carried an ACTIVE close_option stop the design forbids for O_ERN/O_CBS/O_PBS/
O_CONVEX's ``opt_sl_ml``.

Fixture pattern mirrors ``test_entry_ruleset_seed.py`` (the existing
``seed_ruleset_from_tree`` regression suite) exactly, for the sibling function
``seed_ruleset_from_rules``.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_sl_ml_authored_off_parity.py -v
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.services.backtest.seam_wiring import wire_backtest_seams
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest import default_rulesets as dr
from ba2_common.core.models import Ruleset
from ba2_common.core.rules_convert import trade_rules_to_live_export
from ba2_common.core.types import AnalysisUseCase
import ba2_common.core.db as cdb

_DISABLED_CLOSE_RULE = {
    "id": "opt_sl_ml", "name": "opt_sl_ml", "enabled": False, "toggle_optimize": True,
    "conditions": {"type": "AND", "conditions": [
        {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">", "value": 50}]},
    "actions": [{"action_type": "close_option"}],
}

_LIVE_CLOSE_RULE = {
    "id": "opt_tp", "name": "opt_tp",
    "conditions": {"type": "AND", "conditions": [
        {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 100}]},
    "actions": [{"action_type": "close_option"}],
}


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("sl-ml-parity-test"):
        yield


def _seeded_event_action_names(ruleset_id: int):
    with Session(cdb.get_engine()) as s:
        rs = s.get(Ruleset, ruleset_id)
        return [ea.name for ea in rs.event_actions]


def test_the_backtest_seeder_produces_no_action_for_the_disabled_rule(_trading_db):
    rid = dr.seed_ruleset_from_rules(
        [_DISABLED_CLOSE_RULE, _LIVE_CLOSE_RULE], AnalysisUseCase.OPEN_POSITIONS,
        name="sl-ml-parity")
    names = _seeded_event_action_names(rid)
    assert "opt_tp" in names
    assert "opt_sl_ml" not in names


def test_the_live_export_produces_no_action_for_the_disabled_rule():
    out = trade_rules_to_live_export(
        entry_rules=[], exit_rules=[_DISABLED_CLOSE_RULE, _LIVE_CLOSE_RULE])
    op = next(rs for rs in out["rulesets"] if rs["subtype"] == "open_positions")
    names = {r["name"] for r in op["rules"]}
    assert "opt_tp" in names
    assert "opt_sl_ml" not in names


def test_both_runtimes_agree_the_disabled_rule_produces_no_action_at_all(_trading_db):
    """THE PARITY ASSERTION: feed a ruleset of ONLY the disabled rule to both paths and prove
    both produce nothing for it -- backtest seeds zero event actions, live export seeds no
    open_positions ruleset at all."""
    rid = dr.seed_ruleset_from_rules(
        [_DISABLED_CLOSE_RULE], AnalysisUseCase.OPEN_POSITIONS, name="sl-ml-parity-empty")
    assert _seeded_event_action_names(rid) == []

    out = trade_rules_to_live_export(entry_rules=[], exit_rules=[_DISABLED_CLOSE_RULE])
    assert not [rs for rs in out["rulesets"] if rs["subtype"] == "open_positions"]
