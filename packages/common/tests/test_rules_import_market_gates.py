"""The FOURTH door onto the same failure: importing rules from JSON.

``market_condition_rules`` calls it the worst outcome in the whole market-condition design --
a market-condition gate on an OPEN-POSITIONS ruleset. Outside the entry decision pass the live
resolver has no decision context, so the gate reads ``no_context``, the rule NEVER FIRES, and
the position's exit or protective-order adjustment silently stops happening. A deployed sleeve
whose stop-loss rule cannot fire looks exactly like one that is simply holding.

Three doors already refused it: the expert dialog (attaching a gated ruleset to the
open-positions slot), the rules editor (saving a ruleset, and saving a rule), and the deploy
importer (``trade_rules_to_live_export``). ``rules_export_import`` was the fourth and it checked
NOTHING -- ``_import_rule_to_session`` builds an ``EventAction`` from the payload verbatim and
links it, and the module contained no reference to ``market_condition_fields`` at all. A payload
whose RULESET is open_positions and whose RULE says enter_market imported clean, and the link is
what the live engine reads.

The refusal keys on the ruleset the rule is being LINKED INTO, not on the rule's own subtype,
for exactly the reason the editor's does: ``db.ruleset_event_actions`` loads a ruleset's rules
by the link table alone.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from ba2_common.core.db import get_db
from ba2_common.core.models import EventAction, Ruleset
from ba2_common.core.rules_export_import import RulesImporter

ADX = "underlying_adx_14"
GATE = {"cond_0": {"event_type": ADX, "operator": ">", "value": 25.0}}
ORDINARY = {"cond_0": {"event_type": "profit_loss_percent", "operator": "<", "value": -5.0}}
CLOSE = {"action_0": {"action_type": "close_position"}}


def _rule(name, subtype, triggers):
    return {"name": name, "type": "trading_recommendation_rule", "subtype": subtype,
            "triggers": triggers, "actions": CLOSE, "extra_parameters": {},
            "continue_processing": False, "order_index": 0}


def _ruleset(name, subtype, rules):
    return {"name": name, "description": None, "type": "trading_recommendation_rule",
            "subtype": subtype, "rules": rules}


def _names(model):
    with get_db() as session:
        return {row.name for row in session.exec(select(model)).all()}


# ------------------------------------------------------------------ the ruleset's subtype rules

def test_a_gated_rule_imported_into_an_open_positions_ruleset_is_refused():
    """The payload the hole let through: the RULE says enter_market, so a check on the rule's own
    subtype passes it, and the ruleset it is being linked into is the exit slot."""
    payload = {"ruleset": _ruleset("imported exits", "open_positions",
                                   [_rule("gated entry", "enter_market", GATE)])}

    with pytest.raises(ValueError) as excinfo:
        RulesImporter.import_ruleset(payload)

    msg = str(excinfo.value)
    assert "open-positions / exit ruleset" in msg
    assert "imported exits" in msg, "the message must name the ruleset, not just the leaf"
    assert ADX in msg or "cond_0" in msg


def test_nothing_is_written_when_the_import_is_refused():
    """Refused BEFORE the ruleset row, so a half-imported strategy cannot be left behind: the
    operator reading the error believes nothing landed."""
    payload = {"ruleset": _ruleset("refused exits", "open_positions",
                                   [_rule("refused gated rule", "enter_market", GATE)])}

    with pytest.raises(ValueError):
        RulesImporter.import_ruleset(payload)

    assert "refused exits" not in _names(Ruleset)
    assert "refused gated rule" not in _names(EventAction)


def test_the_same_rules_import_fine_into_an_enter_market_ruleset():
    """Gates DECIDE ENTRY: on the entry ruleset they are the feature. (Whether the expert's
    profile serves the field is the expert dialog's question, not the importer's.)"""
    payload = {"ruleset": _ruleset("imported entries", "enter_market",
                                   [_rule("gated entry ok", "enter_market", GATE)])}

    ruleset_id, _ = RulesImporter.import_ruleset(payload)

    assert ruleset_id
    assert "gated entry ok" in _names(EventAction)


def test_an_ordinary_open_positions_ruleset_still_imports():
    payload = {"ruleset": _ruleset("plain exits", "open_positions",
                                   [_rule("stop loss", "open_positions", ORDINARY)])}

    ruleset_id, _ = RulesImporter.import_ruleset(payload)

    assert ruleset_id


def test_the_multi_ruleset_importer_refuses_it_too():
    """Three importers create ruleset->rule links; a guard on one of them is a guard on none."""
    payload = {"rulesets": [_ruleset("multi exits", "open_positions",
                                     [_rule("multi gated", "enter_market", GATE)])]}

    with pytest.raises(ValueError, match="open-positions / exit ruleset"):
        RulesImporter.import_multiple_rulesets(payload)

    assert "multi gated" not in _names(EventAction)


def test_the_reuse_by_name_importer_refuses_it_too():
    """The most dangerous of the three: it REPLACES an existing ruleset's rules in place, so
    every expert pointing at that ruleset follows the change."""
    payload = {"rulesets": [_ruleset("reused exits", "open_positions",
                                     [_rule("reused gated", "enter_market", GATE)])]}

    with pytest.raises(ValueError, match="open-positions / exit ruleset"):
        RulesImporter.import_rulesets_reusing_by_name(payload)

    assert "reused gated" not in _names(EventAction)


def test_a_refused_replacement_leaves_the_existing_rulesets_rules_alone():
    """The replacement drops the old links before adding the payload's. Refusing after that
    would leave a live exit ruleset EMPTY -- no rules at all -- which is the same silence this
    refusal exists to prevent, arrived at from the other side."""
    good = {"rulesets": [_ruleset("standing exits", "open_positions",
                                  [_rule("standing stop", "open_positions", ORDINARY)])]}
    RulesImporter.import_rulesets_reusing_by_name(good)

    bad = {"rulesets": [_ruleset("standing exits", "open_positions",
                                 [_rule("sneaky gate", "enter_market", GATE)])]}
    with pytest.raises(ValueError, match="open-positions / exit ruleset"):
        RulesImporter.import_rulesets_reusing_by_name(bad)

    with get_db() as session:
        ruleset = session.exec(select(Ruleset).where(Ruleset.name == "standing exits")).first()
        from ba2_common.core.db import ruleset_event_actions
    assert [r.name for r in ruleset_event_actions(ruleset.id)] == ["standing stop"]


# ------------------------------------------------------------------- the standalone rule door

def test_a_standalone_open_positions_rule_carrying_a_gate_is_refused():
    """``import_rule`` links nothing, so only the rule's own subtype says where it is headed --
    the same half the editor keeps for a rule that has no links yet."""
    payload = {"rule": _rule("standalone gated exit", "open_positions", GATE)}

    with pytest.raises(ValueError, match="open-positions / exit ruleset"):
        RulesImporter.import_rule(payload)

    assert "standalone gated exit" not in _names(EventAction)


def test_a_standalone_enter_market_rule_carrying_a_gate_imports():
    payload = {"rule": _rule("standalone gated entry", "enter_market", GATE)}

    rule_id, _ = RulesImporter.import_rule(payload)

    assert rule_id
