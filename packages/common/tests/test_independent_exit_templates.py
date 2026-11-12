"""``independent_exit_rules``: the ``rsi`` / ``spy`` exit templates (no market-condition profile involved)."""
import pytest

from ba2_common.core.market_condition_rules import _unresolved_reason
from ba2_common.core.market_condition_templates import (
    ALL_MARKET_EXIT_KINDS, INDEPENDENT_EXIT_KINDS, INDEPENDENT_EXIT_RULE_SUFFIXES, MARKET_EXIT_KINDS,
    PULLBACK_RSI_FIELDS, independent_exit_rules, market_exit_rules)
from ba2_common.core.rule_models import ConditionLeaf, normalize_trade_rules


def _leaves(rule):
    return rule["conditions"]["conditions"]


def test_kind_tables():
    assert MARKET_EXIT_KINDS == ("exit", "stop", "tp")
    assert INDEPENDENT_EXIT_KINDS == ("rsi", "spy")
    assert ALL_MARKET_EXIT_KINDS == MARKET_EXIT_KINDS + INDEPENDENT_EXIT_KINDS
    assert INDEPENDENT_EXIT_RULE_SUFFIXES == ("-pb-exit-rsi", "-spy-stop")


def test_market_exit_rules_still_rejects_the_new_kinds():
    with pytest.raises(ValueError):
        market_exit_rules("x", ("ohlcv-v1",), "long", ("rsi",))


@pytest.mark.parametrize("direction", ["long", "short"])
def test_every_rule_is_off_by_default_and_every_leaf_is_fixed(direction):
    rules = independent_exit_rules("x", direction, ("rsi", "spy"))
    assert [r["id"] for r in rules] == ["x-pb-exit-rsi", "x-spy-stop"]
    for r in rules:
        assert r["enabled"] is False and r["toggle_optimize"] is True
        for leaf in _leaves(r):
            assert not any(leaf.get(k) for k in ("mode_optimize", "mode_choices", "toggle_optimize"))
            assert leaf["mode"] != "off"
    assert [r["continue_processing"] for r in rules] == [False, True]


def test_the_rsi_leaf_has_a_field_gene_but_no_mode_gene_and_validates():
    (rule,) = independent_exit_rules("x", "long", ("rsi",))
    (leaf,) = _leaves(rule)
    parsed = ConditionLeaf.model_validate(leaf)            # field_optimize without mode_optimize on a fixed leaf
    assert parsed.field_optimize is True and parsed.field_choices == list(PULLBACK_RSI_FIELDS)
    (norm,) = normalize_trade_rules([rule])
    assert norm["conditions"]["conditions"][0]["field_choices"] == list(PULLBACK_RSI_FIELDS)
    assert norm["enabled"] is False


def test_a_field_gene_still_needs_a_mode_gene_or_a_fixed_mode():
    bad = {"id": "l", "field": PULLBACK_RSI_FIELDS[0], "field_type": "numeric", "op": ">", "value": 50.0,
           "field_optimize": True, "field_choices": list(PULLBACK_RSI_FIELDS)}
    with pytest.raises(ValueError, match="field_optimize"):
        ConditionLeaf.model_validate(bad)
    ConditionLeaf.model_validate({**bad, "mode": "above", "optimize": True, "value_min": 50.0, "value_max": 90.0, "value_step": 5.0})


def test_the_unscoped_spy_rule_has_one_leaf_and_the_scoped_one_two():
    (plain,) = independent_exit_rules("x", "long", ("spy",))
    assert len(_leaves(plain)) == 1 and not _unresolved_reason(_leaves(plain)[0])
    (scoped,) = independent_exit_rules("x", "long", ("spy",), spy_up_tag=1)
    tag = _leaves(scoped)[-1]
    assert (tag["field"], tag["op"], tag["value"]) == ("entry_tag", "==", 1.0)
    # the tag is a pure scope: no gene of any kind
    assert not any(tag.get(k) for k in ("optimize", "mode_optimize", "toggle_optimize"))
    (rsi,) = independent_exit_rules("x", "long", ("rsi",), spy_up_tag=1)
    assert [l["field"] for l in _leaves(rsi)] == [PULLBACK_RSI_FIELDS[0]]      # the tag scopes the spy rule only
