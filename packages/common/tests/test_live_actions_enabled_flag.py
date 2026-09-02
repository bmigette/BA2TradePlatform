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
"""
from ba2_common.core.rules_convert import (
    live_actions_from_trade_rule,
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
