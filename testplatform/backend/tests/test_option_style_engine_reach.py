"""Stage-2 style rules REACH the engine, and one member's exits never touch another's position.

Plan docs/plans/2026-09-25-option-stage2-build.md, Task 8, as revised by "Tasks 5-6 as built"
(every exit rule is scoped, the stock-phase ones included, E2). Everything here goes through
the SAME seeder the backtest uses (``default_rulesets.seed_*_ruleset_from_rules``, which calls
the live export's own converters), so "the builder emitted it" is never taken as evidence that
the engine sees it:

* no style leaf is dropped when a rule becomes an EventAction's triggers;
* every seeded exit EventAction carries exactly ONE ``entry_tag ==`` trigger, its own member's
  tag -- so a position opened by member A can satisfy no exit of member B (static, every pair of
  members of every candidate style);
* scoping does not change an exit's forced/discretionary class (the concession it pays);
* every seeded entry action carries its member's tag, which is what the order row records.

The two ENGINE facts that isolation between members on one SYMBOL rests on (review note M4) are
pinned against the real engine in ``tests/backtest/test_style_member_isolation_engine.py``.
"""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_option_grid_foundations import _launcher

_CAND = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")
_STYLES = ["short", "mid", "premium", "leaps_convex"]


def _style(m, name):
    m._STYLE_ACTIVE = m._load_style_spec(_CAND, name)
    return m._build_strategy_style("O_STYLE")


def _seed(rules, ns):
    """Seed ``rules`` exactly as the backtest does; return the ordered EventActions as plain
    ``(triggers, actions)`` records (read inside the session, so nothing is detached)."""
    from sqlmodel import select
    from app.services.backtest.backtest_db import backtest_trading_db
    from app.services.backtest.default_rulesets import (
        seed_entry_ruleset_from_rules, seed_exit_ruleset_from_rules)
    from ba2_common.core.db import get_db
    from ba2_common.core.models import EventAction, RulesetEventActionLink

    ctx = backtest_trading_db(f"style-reach-{uuid.uuid4().hex[:8]}")
    ctx.__enter__()
    try:
        seed = seed_entry_ruleset_from_rules if ns == "entry" else seed_exit_ruleset_from_rules
        rid = seed(rules, name=f"style-reach-{ns}")
        with get_db() as session:
            links = session.exec(select(RulesetEventActionLink)
                                 .where(RulesetEventActionLink.ruleset_id == rid)
                                 .order_by(RulesetEventActionLink.order_index)).all()
            out = []
            for link in links:
                ea = session.get(EventAction, link.eventaction_id)
                out.append(SimpleNamespace(triggers=json.loads(json.dumps(ea.triggers)),
                                           actions=json.loads(json.dumps(ea.actions))))
            return out
    finally:
        ctx.__exit__(None, None, None)


def _seedable(rules):
    """The rules the seeder turns into an EventAction, in order (``enabled: False`` rules --
    the authored-off ``opt_sl_ml`` of O_PMCC / O_CONVEX -- convert to nothing, by design)."""
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    return [r for r in rules if live_actions_from_trade_rule(r)]


def _member_of_exit(s, rule_id):
    (member,) = [k for k, idm in s.style_id_maps.items()
                 if rule_id in idm["exit_rules"].values()]
    return member


def _tag_triggers(ea):
    return [t for t in ea.triggers.values() if t["event_type"] == "entry_tag"]


# --- no leaf dropped ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", _STYLES)
def test_no_style_leaf_is_dropped_at_seeding(name):
    from ba2_common.core.rule_builders import tree_leaves, triggers_from_condition_tree
    s = _style(_launcher(), name)
    for rules in (s.entry_rules, s.exit_rules):
        for r in rules:
            leaves = list(tree_leaves(r["conditions"]))
            assert len(triggers_from_condition_tree(r["conditions"])) == len(leaves), r["id"]


# --- the exit ruleset the backtest SEEDS ----------------------------------------------------------
@pytest.mark.parametrize("name", _STYLES)
def test_every_seeded_exit_carries_exactly_its_own_members_tag_trigger(name):
    m = _launcher()
    s = _style(m, name)
    rules = _seedable(s.exit_rules)
    eas = _seed(s.exit_rules, "exit")
    assert len(eas) == len(rules), "an exit rule was silently lost at seeding"
    for rule, ea in zip(rules, eas):
        member = _member_of_exit(s, rule["id"])
        tags = _tag_triggers(ea)
        assert len(tags) == 1, f"{rule['id']}: {len(tags)} entry_tag triggers"
        assert tags[0]["operator"] == "==" and tags[0]["value"] == float(
            m._STYLE_MEMBER_TAGS[member]), (rule["id"], tags)
        # every AUTHORED trigger is still there, beside the tag
        assert len(ea.triggers) == len(list(_leaves(rule["conditions"]))), rule["id"]


def _leaves(node):
    from ba2_common.core.rule_builders import tree_leaves
    return tree_leaves(node)


@pytest.mark.parametrize("name", _STYLES)
def test_a_position_opened_by_member_A_matches_no_exit_rule_of_member_B(name):
    """Static isolation over EVERY ordered pair of members: every seeded exit of B requires
    ``entry_tag == tag(B)``, and EntryTagCondition reads A's tag off A's entry order -- a False
    leaf in an AND, so no exit of B can ever close A's position."""
    from ba2_common.core.TradeConditions import EntryTagCondition
    m = _launcher()
    s = _style(m, name)
    rules = _seedable(s.exit_rules)
    eas = _seed(s.exit_rules, "exit")
    members = [x["key"] for x in m._STYLE_ACTIVE["members"]]
    by_member = {k: [ea for r, ea in zip(rules, eas) if _member_of_exit(s, r["id"]) == k]
                 for k in members}
    assert all(by_member[k] for k in members), "a member seeded no exit at all"
    for a in members:
        a_order = SimpleNamespace(id=1, data={"entry_tag": m._STYLE_MEMBER_TAGS[a]},
                                  parent_order_id=None, contract_symbol=None)
        for b in members:
            for ea in by_member[b]:
                (t,) = _tag_triggers(ea)
                fires = EntryTagCondition(account=None, instrument_name="X",
                                          expert_recommendation=None, operator_str=t["operator"],
                                          value=t["value"], existing_order=a_order).evaluate()
                assert fires is (a == b), (a, b, t)


@pytest.mark.parametrize("name", _STYLES)
def test_forced_exit_classification_is_unchanged_by_scoping(name):
    """For each member, every seeded scoped exit classifies exactly as the member's own
    single-job exit does: the tag must not turn a DTE/SL exit discretionary or vice versa."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit
    m = _launcher()
    s = _style(m, name)
    rules = _seedable(s.exit_rules)
    eas = _seed(s.exit_rules, "exit")
    for mem in m._STYLE_ACTIVE["members"]:
        key = mem["key"]
        own = m._STRATEGY_BUILDERS[key](key).exit_rules
        own_eas = _seed(own, "exit")
        scoped = [ea for r, ea in zip(rules, eas) if _member_of_exit(s, r["id"]) == key]
        assert len(scoped) == len(own_eas), key
        assert [forced_option_exit(x) for x in scoped] == [forced_option_exit(x)
                                                            for x in own_eas], key


# --- the enter ruleset the backtest SEEDS -------------------------------------------------------
@pytest.mark.parametrize("name", _STYLES)
def test_the_entry_action_tag_reaches_the_seeded_enter_ruleset(name):
    """One EventAction per member entry rule, in member order, each carrying its OWN member's
    tag on every opening action (``entry_tag`` in the live action config, which the option
    entry and the equity ``buy`` write onto the order row)."""
    m = _launcher()
    s = _style(m, name)
    eas = _seed(s.entry_rules, "entry")
    assert len(eas) == len(s.entry_rules)
    expected = []
    for mem in m._STYLE_ACTIVE["members"]:
        expected += [m._STYLE_MEMBER_TAGS[mem["key"]]] * len(s.style_id_maps[mem["key"]][
            "entry_rules"])
    got = []
    for ea in eas:
        opening = [a for a in ea.actions.values() if a["action_type"] != "stop_processing"]
        tags = {a["entry_tag"] for a in opening}
        assert len(tags) == 1, ea.actions
        got.append(tags.pop())
    assert got == expected
