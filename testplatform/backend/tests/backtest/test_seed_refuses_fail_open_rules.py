"""The backtest seeder refuses a rule that would seed ALWAYS TRUE or lose a market gate.

Same check as the live export (``rules_convert.trade_rules_to_live_export``), through the shared
``rule_builders.rule_triggers_from_tree``: a rule whose leaves produce no trigger at all would fire
on every position, and one that loses a market-condition leaf runs ungated. An ordinary partial
drop still seeds, as it always did, and a disabled rule is not judged.

Fixture pattern mirrors ``test_sl_ml_authored_off_parity.py``.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.services.backtest import default_rulesets as dr
from app.services.backtest.backtest_db import backtest_trading_db
from app.services.backtest.seam_wiring import wire_backtest_seams
import ba2_common.core.db as cdb
from ba2_common.core.models import Ruleset
from ba2_common.core.types import AnalysisUseCase

UNKNOWN = {"id": "u", "field": "no_such_field_ever", "op": ">", "value": 1}
DAYS = {"id": "d", "field": "days_opened", "op": ">", "value": 10}
ADX_NO_VALUE = {"id": "adx", "field": "underlying_adx_14", "op": ">"}


def _close(rid, *leaves, **extra):
    return {"id": rid, "name": rid, "conditions": {"type": "AND", "conditions": list(leaves)},
            "actions": [{"action_type": "close"}], **extra}


@pytest.fixture()
def _trading_db():
    wire_backtest_seams()
    with backtest_trading_db("fail-open-seed-test"):
        yield


def _triggers(ruleset_id):
    with Session(cdb.get_engine()) as s:
        return {ea.name: ea.triggers for ea in s.get(Ruleset, ruleset_id).event_actions}


def test_a_rule_with_every_leaf_dropped_is_refused(_trading_db):
    with pytest.raises(ValueError, match="'bad'.*ALWAYS TRUE"):
        dr.seed_ruleset_from_rules([_close("ok", DAYS), _close("bad", UNKNOWN)],
                                   AnalysisUseCase.OPEN_POSITIONS, name="fail-open")


def test_a_dropped_market_leaf_is_refused(_trading_db):
    with pytest.raises(ValueError, match="'gated'.*market gate"):
        dr.seed_ruleset_from_rules([_close("gated", DAYS, ADX_NO_VALUE)],
                                   AnalysisUseCase.OPEN_POSITIONS, name="fail-open")


def test_ordinary_partial_drop_and_disabled_rules_still_seed(_trading_db):
    rid = dr.seed_ruleset_from_rules(
        [_close("partial", DAYS, UNKNOWN), _close("off", UNKNOWN, enabled=False)],
        AnalysisUseCase.OPEN_POSITIONS, name="fail-open")
    assert _triggers(rid) == {
        "partial": {"cond_0": {"event_type": "days_opened", "operator": ">", "value": 10}}}
