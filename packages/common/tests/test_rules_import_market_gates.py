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

PLAN 2026-09-24 TASK B2 NARROWED IT. Since B1 the live open-positions pass opens a decision
scope, so the gate evaluates live exactly as in the backtest and a failed read is UNKNOWN (the
rule does not fire). An open-positions rule may therefore carry a gate when it only CLOSES,
REDUCES or ADJUSTS TP/SL; a gated rule with any other action (an open, a ``stop_processing``,
a roll) is still refused at every door below, before anything is written.
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
CLOSE = {"action_0": {"action_type": "close"}}
BUY = {"action_0": {"action_type": "buy"}}
REFUSED = "may not use"


def _rule(name, subtype, triggers, actions=BUY):
    return {"name": name, "type": "trading_recommendation_rule", "subtype": subtype,
            "triggers": triggers, "actions": actions, "extra_parameters": {},
            "continue_processing": False, "order_index": 0}


def _ruleset(name, subtype, rules):
    return {"name": name, "description": None, "type": "trading_recommendation_rule",
            "subtype": subtype, "rules": rules}


def _names(model):
    with get_db() as session:
        return {row.name for row in session.exec(select(model)).all()}


# ------------------------------------------------------------------ the ruleset's subtype rules

def test_a_gated_buy_rule_imported_into_an_open_positions_ruleset_is_refused():
    """The payload the hole let through: the RULE says enter_market, so a check on the rule's own
    subtype passes it, and the ruleset it is being linked into is the exit slot. Its action is an
    OPEN, which a gated exit rule may not do."""
    payload = {"ruleset": _ruleset("imported exits", "open_positions",
                                   [_rule("gated entry", "enter_market", GATE)])}

    with pytest.raises(ValueError) as excinfo:
        RulesImporter.import_ruleset(payload)

    msg = str(excinfo.value)
    assert REFUSED in msg and "'buy'" in msg
    assert "imported exits" in msg, "the message must name the ruleset, not just the leaf"
    assert "gated entry" in msg and "cond_0" in msg


def test_a_gated_close_rule_imports_into_an_open_positions_ruleset():
    """The B2 contract: a gate on an exit rule that only closes is a market EXIT, and imports."""
    payload = {"ruleset": _ruleset("market exits", "open_positions",
                                   [_rule("market close", "open_positions", GATE, CLOSE)])}

    ruleset_id, _ = RulesImporter.import_ruleset(payload)

    assert ruleset_id
    assert "market close" in _names(EventAction)
    # The expert dialog's reader sees the rule as stored: its name, its gate and its action.
    from ba2_common.core.market_condition_live import ruleset_rule_contents

    (name, triggers, actions), = ruleset_rule_contents(ruleset_id)
    assert (name, triggers, actions) == ("market close", GATE, CLOSE)
    assert ruleset_rule_contents(None) == ()


def test_a_gated_stop_processing_rule_is_refused_on_an_open_positions_ruleset():
    stop = {"action_0": {"action_type": "stop_processing"}}
    payload = {"ruleset": _ruleset("stopping exits", "open_positions",
                                   [_rule("gated stop", "open_positions", GATE, stop)])}

    with pytest.raises(ValueError, match="'stop_processing'"):
        RulesImporter.import_ruleset(payload)


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
                                   [_rule("stop loss", "open_positions", ORDINARY, CLOSE)])}

    ruleset_id, _ = RulesImporter.import_ruleset(payload)

    assert ruleset_id


def test_the_multi_ruleset_importer_refuses_it_too():
    """Three importers create ruleset->rule links; a guard on one of them is a guard on none."""
    payload = {"rulesets": [_ruleset("multi exits", "open_positions",
                                     [_rule("multi gated", "enter_market", GATE)])]}

    with pytest.raises(ValueError, match=REFUSED):
        RulesImporter.import_multiple_rulesets(payload)

    assert "multi gated" not in _names(EventAction)


def test_the_reuse_by_name_importer_refuses_it_too():
    """The most dangerous of the three: it REPLACES an existing ruleset's rules in place, so
    every expert pointing at that ruleset follows the change."""
    payload = {"rulesets": [_ruleset("reused exits", "open_positions",
                                     [_rule("reused gated", "enter_market", GATE)])]}

    with pytest.raises(ValueError, match=REFUSED):
        RulesImporter.import_rulesets_reusing_by_name(payload)

    assert "reused gated" not in _names(EventAction)


def test_a_refused_replacement_leaves_the_existing_rulesets_rules_alone():
    """The replacement drops the old links before adding the payload's. Refusing after that
    would leave a live exit ruleset EMPTY -- no rules at all -- which is the same silence this
    refusal exists to prevent, arrived at from the other side."""
    good = {"rulesets": [_ruleset("standing exits", "open_positions",
                                  [_rule("standing stop", "open_positions", ORDINARY, CLOSE)])]}
    RulesImporter.import_rulesets_reusing_by_name(good)

    bad = {"rulesets": [_ruleset("standing exits", "open_positions",
                                 [_rule("sneaky gate", "enter_market", GATE)])]}
    with pytest.raises(ValueError, match=REFUSED):
        RulesImporter.import_rulesets_reusing_by_name(bad)

    with get_db() as session:
        ruleset = session.exec(select(Ruleset).where(Ruleset.name == "standing exits")).first()
        from ba2_common.core.db import ruleset_event_actions
    assert [r.name for r in ruleset_event_actions(ruleset.id)] == ["standing stop"]


# ------------------------------------------------------------------- the standalone rule door

def test_a_standalone_open_positions_buy_rule_carrying_a_gate_is_refused():
    """``import_rule`` links nothing, so only the rule's own subtype says where it is headed --
    the same half the editor keeps for a rule that has no links yet."""
    payload = {"rule": _rule("standalone gated exit", "open_positions", GATE)}

    with pytest.raises(ValueError, match=REFUSED):
        RulesImporter.import_rule(payload)

    assert "standalone gated exit" not in _names(EventAction)


def test_a_standalone_open_positions_close_rule_carrying_a_gate_imports():
    payload = {"rule": _rule("standalone gated close", "open_positions", GATE, CLOSE)}

    rule_id, _ = RulesImporter.import_rule(payload)

    assert rule_id


def test_a_standalone_enter_market_rule_carrying_a_gate_imports():
    payload = {"rule": _rule("standalone gated entry", "enter_market", GATE)}

    rule_id, _ = RulesImporter.import_rule(payload)

    assert rule_id


# ------------------------------------------- replace-in-place: the experts ALREADY using it
#
# ``import_rulesets_reusing_by_name`` edits a ruleset experts may be running. A market gate it
# adds must be SERVED by each of them (their ``market_condition_profile``), or it reads
# ``no_context`` for ever and the exit it guards never fires. Every other importer creates a NEW
# ruleset, which no expert uses yet -- the expert dialog checks the profile when it is attached.

class _Expert:
    def __init__(self, profile):
        self.settings = {"market_condition_profile": profile}


@pytest.fixture
def profiles(monkeypatch):
    """``instance id -> profile setting``, served through the instance-resolver seam."""
    from ba2_common.core import instance_resolver

    table: dict = {}
    asked: list = []

    class _Resolver:
        def get_expert_instance(self, instance_id):
            asked.append(instance_id)
            return _Expert(table[instance_id])

    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(_Resolver())
    table["asked"] = asked
    yield table
    instance_resolver.set_instance_resolver(previous)


def _linked_exit_ruleset(name, profile_table, profile, *, enabled=True):
    """An existing open-positions ruleset used by one expert whose profile is ``profile``."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertInstance

    ruleset_ids, _ = RulesImporter.import_rulesets_reusing_by_name(
        {"rulesets": [_ruleset(name, "open_positions",
                               [_rule(f"{name} stop", "open_positions", ORDINARY, CLOSE)])]})
    instance_id = add_instance(ExpertInstance(account_id=1, expert="MockExpert", enabled=enabled,
                                              open_positions_ruleset_id=ruleset_ids[0]))
    profile_table[instance_id] = profile
    return ruleset_ids[0], instance_id


def _market_exit_payload(name):
    return {"rulesets": [_ruleset(name, "open_positions",
                                  [_rule(f"{name} market close", "open_positions", GATE, CLOSE)])]}


@pytest.mark.parametrize("enabled", [True, False])
def test_replacing_a_linked_ruleset_with_an_unserved_market_exit_is_refused(profiles, enabled):
    ruleset_id, instance_id = _linked_exit_ruleset(f"unserved-{enabled}", profiles, "",
                                                   enabled=enabled)

    with pytest.raises(ValueError) as e:
        RulesImporter.import_rulesets_reusing_by_name(_market_exit_payload(f"unserved-{enabled}"))

    msg = str(e.value)
    assert f"expert instance {instance_id}" in msg and ADX in msg and "open-positions" in msg
    from ba2_common.core.db import ruleset_event_actions
    assert [r.name for r in ruleset_event_actions(ruleset_id)] == [f"unserved-{enabled} stop"], (
        "refused BEFORE the old links were dropped")


def test_a_profile_that_serves_nothing_the_leaf_needs_is_refused_too(profiles):
    _linked_exit_ruleset("wrong-profile", profiles, "ta-structure-v1")
    with pytest.raises(ValueError, match="ta-structure-v1"):
        RulesImporter.import_rulesets_reusing_by_name(_market_exit_payload("wrong-profile"))


def test_replacing_a_linked_ruleset_with_a_served_market_exit_is_accepted(profiles):
    ruleset_id, _ = _linked_exit_ruleset("served", profiles, "ohlcv-v1")

    ids, _ = RulesImporter.import_rulesets_reusing_by_name(_market_exit_payload("served"))

    assert ids == [ruleset_id]
    from ba2_common.core.db import ruleset_event_actions
    assert [r.name for r in ruleset_event_actions(ruleset_id)] == ["served market close"]


def test_replacing_an_unlinked_ruleset_with_a_market_exit_is_accepted(profiles):
    RulesImporter.import_rulesets_reusing_by_name(
        {"rulesets": [_ruleset("nobody uses me", "open_positions",
                               [_rule("nobody stop", "open_positions", ORDINARY, CLOSE)])]})

    ids, _ = RulesImporter.import_rulesets_reusing_by_name(_market_exit_payload("nobody uses me"))

    assert ids and profiles["asked"] == []


def test_replacing_a_linked_ruleset_with_ordinary_rules_asks_no_expert(profiles, monkeypatch):
    """No market leaf, no expert query: the linked-expert lookup is never reached."""
    import ba2_common.core.market_condition_live as live

    _linked_exit_ruleset("ordinary", profiles, "")
    looked_up: list = []
    real = live.experts_linked_to_rulesets
    monkeypatch.setattr(live, "experts_linked_to_rulesets",
                        lambda *a, **k: looked_up.append(a) or real(*a, **k))

    RulesImporter.import_rulesets_reusing_by_name(
        {"rulesets": [_ruleset("ordinary", "open_positions",
                               [_rule("ordinary stop 2", "open_positions", ORDINARY, CLOSE)])]})

    assert looked_up == [] and profiles["asked"] == []


def test_an_expert_whose_profile_cannot_be_read_refuses_rather_than_guesses(profiles):
    """An unreadable setting is not "no profile": the save is refused and says why."""
    _linked_exit_ruleset("unreadable", profiles, "")
    profiles.clear()          # the resolver now raises KeyError for that instance
    profiles["asked"] = []

    with pytest.raises(ValueError, match="cannot verify the market-condition gates"):
        RulesImporter.import_rulesets_reusing_by_name(_market_exit_payload("unreadable"))


def test_the_linked_expert_lookup_reads_both_slots():
    from ba2_common.core.db import add_instance
    from ba2_common.core.market_condition_live import experts_linked_to_rulesets
    from ba2_common.core.models import ExpertInstance

    entry_id, exit_id = 880_001, 880_002
    a = add_instance(ExpertInstance(account_id=1, expert="MockExpert",
                                    enter_market_ruleset_id=entry_id,
                                    open_positions_ruleset_id=exit_id))
    b = add_instance(ExpertInstance(account_id=1, expert="MockExpert", enabled=False,
                                    open_positions_ruleset_id=exit_id))

    assert sorted(experts_linked_to_rulesets([entry_id, exit_id])) == sorted([
        (a, "enter-market", entry_id), (a, "open-positions", exit_id),
        (b, "open-positions", exit_id)])
    assert experts_linked_to_rulesets([]) == ()
    assert experts_linked_to_rulesets([None]) == ()
