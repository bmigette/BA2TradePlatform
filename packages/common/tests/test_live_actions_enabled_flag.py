"""``rule["enabled"] is False`` must be a FAIL-CLOSED guard in ``live_actions_from_trade_rule``
-- the ONE choke point both the backtest seeder (``default_rulesets.seed_ruleset_from_rules``)
and the live export path (``strategy_to_live_export`` / ``trade_rules_to_live_export``) share.

Reviewer finding (2026-09-02): before this fix, a rule-level ``enabled: False`` marker (the
launcher's ``_OPTION_SL_ML_AUTHORED_OFF`` convention for O_ERN/O_CBS/O_PBS/O_CONVEX's
``opt_sl_ml``) survived onto a DEFAULT (unsearched) genome's emitted ruleset untouched, and
NOTHING downstream ever consulted that key for a backtest run -- so "authored off" was inert
for exactly the case it exists to cover: a default-genome trial, an unsearched run, and a
seeded live deploy all carried an ACTIVE close_option max-loss stop the design forbids. This
guard is the fail-closed half of the fix: it applies to ANY rule dict handed to it, whether or
not it ever passed through the GA's decode-time removal
(``strategy_param_space._decode_rule_list``), which is what makes it safe for a HAND-WRITTEN
ruleset too.

THE LEGACY SHAPE (added 2026-09-02, plan Task 14b item 1): two call sites never reach
``live_actions_from_trade_rule`` at all -- ``strategy_to_live_export``'s exit loop and
``default_rulesets.seed_open_positions_ruleset`` both convert a rule through
``rule_builders.action_from_rule`` (the ONE-action-per-rule legacy shape ``decode_params``
still emits) and so bypassed the guard entirely. The guard therefore lives in BOTH shared
converters; ``action_from_rule`` is the choke point for the legacy pair exactly as
``live_actions_from_trade_rule`` is for the ordered-actions pair.
"""
from ba2_common.core.rule_builders import action_from_rule
from ba2_common.core.rules_convert import (
    live_actions_from_trade_rule,
    strategy_to_live_export,
    trade_rules_to_live_export,
)

_CLOSE_RULE_ENABLED_FALSE = {
    "id": "opt_sl_ml", "name": "opt_sl_ml", "enabled": False, "toggle_optimize": True,
    "conditions": {"type": "AND", "conditions": [
        {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">", "value": 50}]},
    "actions": [{"action_type": "close_option"}],
}

_CLOSE_RULE_NO_FLAG = {
    "id": "opt_tp", "name": "opt_tp",
    "conditions": {"type": "AND", "conditions": [
        {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 100}]},
    "actions": [{"action_type": "close_option"}],
}

_CLOSE_RULE_ENABLED_TRUE = {**_CLOSE_RULE_ENABLED_FALSE, "id": "opt_sl_ml_on", "enabled": True}


def test_a_rule_flagged_enabled_false_converts_to_no_action():
    assert live_actions_from_trade_rule(_CLOSE_RULE_ENABLED_FALSE) is None


def test_a_rule_with_no_enabled_key_converts_normally():
    actions = live_actions_from_trade_rule(_CLOSE_RULE_NO_FLAG)
    assert actions is not None
    assert actions["a0"]["action_type"] == "close_option"


def test_a_rule_explicitly_flagged_enabled_true_converts_normally():
    """The guard is specifically ``is False`` -- an explicit True (or absence) must not be
    treated as off."""
    actions = live_actions_from_trade_rule(_CLOSE_RULE_ENABLED_TRUE)
    assert actions is not None
    assert actions["a0"]["action_type"] == "close_option"


def test_the_live_export_path_drops_the_rule_entirely():
    """Integration-level: through ``trade_rules_to_live_export`` (the live/export side of the
    shared choke point), an enabled=False exit rule produces NO open_positions ruleset entry
    at all -- not a rule with an inert flag."""
    out = trade_rules_to_live_export(
        entry_rules=[], exit_rules=[_CLOSE_RULE_ENABLED_FALSE, _CLOSE_RULE_NO_FLAG])
    op_rulesets = [rs for rs in out["rulesets"] if rs["subtype"] == "open_positions"]
    assert len(op_rulesets) == 1
    rule_ids = {r["name"] for r in op_rulesets[0]["rules"]}
    assert "opt_tp" in rule_ids
    assert "opt_sl_ml" not in rule_ids


def test_a_ruleset_of_only_disabled_rules_produces_no_open_positions_ruleset():
    """Fail-closed all the way: if EVERY exit rule is enabled=False, no open_positions
    ruleset is emitted at all (mirrors the "rules whose actions all fail to convert are
    dropped" contract ``trade_rules_to_live_export`` already documents)."""
    out = trade_rules_to_live_export(entry_rules=[], exit_rules=[_CLOSE_RULE_ENABLED_FALSE])
    assert not [rs for rs in out["rulesets"] if rs["subtype"] == "open_positions"]


# ==================================================================================================
# THE LEGACY (one-action-per-rule) SHAPE -- ``rule_builders.action_from_rule``
# ==================================================================================================
# ``decode_params`` still emits exit rules in the flat legacy shape
# ``{id, conditions, action_type, action_value, enabled}``, and TWO shared converters read that
# shape directly instead of going through ``live_actions_from_trade_rule``:
#   * ``rules_convert.strategy_to_live_export``'s exit loop (the LIVE export half), and
#   * ``default_rulesets.seed_open_positions_ruleset`` (the BACKTEST seeder half, pinned in
#     testplatform/backend/tests/backtest/test_sl_ml_authored_off_parity.py -- it needs a DB).
# Both call ``action_from_rule``, so that is where the guard belongs for this pair.

_LEGACY_DISABLED_EXIT = {
    "id": "opt_sl_ml", "name": "opt_sl_ml", "enabled": False, "toggle_optimize": True,
    "conditions": {"type": "AND", "conditions": [
        {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">", "value": 50}]},
    "action_type": "close_option",
}

_LEGACY_LIVE_EXIT = {
    "id": "opt_tp", "name": "opt_tp",
    "conditions": {"type": "AND", "conditions": [
        {"id": "tp", "field": "profit_loss_percent", "op": ">", "value": 100}]},
    "action_type": "close_option",
}


def test_action_from_rule_returns_none_for_a_rule_flagged_enabled_false():
    assert action_from_rule(_LEGACY_DISABLED_EXIT) is None


def test_action_from_rule_still_converts_a_rule_with_no_enabled_key():
    assert action_from_rule(_LEGACY_LIVE_EXIT)["act"]["action_type"] == "close_option"


def test_action_from_rule_still_converts_a_rule_explicitly_enabled_true():
    """``is False``, not falsiness: an explicit True must convert."""
    rule = {**_LEGACY_DISABLED_EXIT, "enabled": True}
    assert action_from_rule(rule)["act"]["action_type"] == "close_option"


def test_action_from_rule_guards_an_option_action_too():
    """The option branch returns BEFORE the EXIT_ACTION lookup, so the guard has to sit above
    both or an option rule (which is exactly what ``opt_sl_ml`` is) slips through."""
    rule = {"action_type": "buy_call", "enabled": False,
            "option_strike_method": "delta", "option_strike_param": 0.8}
    assert action_from_rule(rule) is None


def test_strategy_to_live_export_drops_a_disabled_legacy_exit_rule():
    """The legacy LIVE export path end-to-end: no open_positions rule for the disabled one."""
    out = strategy_to_live_export(
        buy_tree=None, sell_tree=None,
        exit_rules=[_LEGACY_DISABLED_EXIT, _LEGACY_LIVE_EXIT])
    op = next(rs for rs in out["rulesets"] if rs["subtype"] == "open_positions")
    names = {r["name"] for r in op["rules"]}
    assert "opt_tp" in names
    assert "opt_sl_ml" not in names


def test_strategy_to_live_export_emits_no_open_positions_ruleset_when_all_are_disabled():
    out = strategy_to_live_export(buy_tree=None, sell_tree=None,
                                  exit_rules=[_LEGACY_DISABLED_EXIT])
    assert not [rs for rs in out["rulesets"] if rs["subtype"] == "open_positions"]
