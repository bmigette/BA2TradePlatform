"""Every gate the option grid emits a GA GENE for must reach the engine.

``_option_entry_rule`` gives each option strategy four ``price_vs_target_*`` gates, each
``optimize=True`` + ``toggle_optimize=True`` -> two genes apiece. ``FIELD_EVENT`` did not
list those fields, so ``triggers_from_condition_tree`` dropped all four leaves at seeding
time: the GA tuned 8 genes per member that the engine never evaluated, and every trial ran
UNGATED while being scored as if gated. On the 5-member OS1 group that is 40 of 77 genes.

This test pins the CLOSURE on the real built strategies (not a hand-written tree): for every
launcher option strategy, the number of triggers seeded must equal the number of leaves, and
no gene may belong to a dropped leaf.
"""
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

from ba2_common.core.rule_builders import (  # noqa: E402
    FIELD_EVENT,
    FLAG_FIELD_EVENT,
    tree_leaves,
    triggers_from_condition_tree,
)

_GROUPS = sorted(mod._OPTION_GROUPS)
_SINGLES = sorted(mod._OPTION_STRATS)


def _build(kind):
    if kind in mod._OPTION_GROUPS:
        return mod._build_strategy_option_group(kind)
    return mod._build_strategy_option(kind)


def _dropped_leaf_ids(strategy):
    out = set()
    for rules in (getattr(strategy, "entry_rules", None), getattr(strategy, "exit_rules", None)):
        for rule in (rules or []):
            for leaf in tree_leaves(rule.get("conditions")):
                field = leaf.get("field")
                if field not in FIELD_EVENT and field not in FLAG_FIELD_EVENT:
                    out.add(leaf.get("id"))
    return out


@pytest.mark.parametrize("kind", _GROUPS + _SINGLES)
def test_no_option_entry_leaf_is_dropped_at_seeding(kind):
    strategy = _build(kind)
    for rule in strategy.entry_rules:
        leaves = [leaf["field"] for leaf in tree_leaves(rule["conditions"])]
        triggers = [t["event_type"] for t in
                    triggers_from_condition_tree(rule["conditions"]).values()]
        assert len(triggers) == len(leaves), (
            f"{kind}/{rule['id']}: {len(leaves) - len(triggers)} condition leaf/leaves were "
            f"DROPPED by triggers_from_condition_tree (leaves={leaves} triggers={triggers}) "
            f"-- the strategy runs ungated on them while the GA tunes their genes"
        )


@pytest.mark.parametrize("kind", _GROUPS + _SINGLES)
def test_no_ga_gene_belongs_to_a_dropped_leaf(kind):
    """The economic statement: a gene whose leaf is dropped is search budget spent on a
    dimension that cannot affect the simulation."""
    from app.services.strategy_param_space import collect_param_space

    strategy = _build(kind)
    dropped = _dropped_leaf_ids(strategy)
    space = collect_param_space(strategy)
    dead = sorted(g for g in space
                  if g.startswith("cond:") and g.split(":")[1] in dropped)
    assert not dead, (
        f"{kind}: {len(dead)}/{len(space)} GA genes are DEAD (their condition leaf never "
        f"becomes a trigger): {dead}"
    )
