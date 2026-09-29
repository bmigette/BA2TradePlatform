"""``market:enabled`` -- the MASTER GENE over every market-condition gene of a strategy
(atr_grid_2027 market master-gene addendum, operator decision 2026-09-29: one GA per cell with
market conditions (and ATR) togglable by the GA, plus a cheap ablation of the winners).

SCOPE (review fix, 2026-09-29): the gene is added ONLY when the strategy carries the explicit
OPT-IN MARKER (``rule["market_master_gene"] = True``) -- stamped ONLY by
``ba2test_launcher._append_equity_market_condition_gates`` /
``_append_equity_market_exit_rules``, i.e. ONLY the goal2027atr equity S1-S7 jobs. Field
membership alone (a leaf's ``field`` being a registered market-condition field) is NOT enough:
option strategies (O_LC/O_CC/O_PP/...) and the exploration driver carry real market-condition
leaves too, through OTHER code paths that never stamp the marker, and must keep a byte-identical
gene space / checkpoint fingerprint to before this feature existed (a paused/resumed option job
must not silently restart at generation 0).

Resolved entirely in ``decode_params`` (the ONE shared decode path -- GA trial, top-N persist,
re-run, robustness variant, deploy export, tools): decode itself is marker-INDEPENDENT --
whenever ``market:enabled`` is present in the genome at all (which, for a genome any real run
produced, only happens because ``collect_param_space`` added it, i.e. the strategy WAS marked),
it forces off every market-condition LEAF (by field) and RULE (any rule declaring its own
``toggle_optimize`` with a market leaf inside) it finds structurally on the template -- see
``_market_condition_members``. The marker only gates COLLECTION
(``_strategy_opts_into_market_master_gene``), not decode.
"""
import copy
import types

import pytest

from ba2_common.core.market_conditions import FieldSpec, ProfileSpec, registered_profile
from app.services.genetic import GeneticOptimizer
from app.services.strategy_param_space import collect_param_space, decode_params

_STATE_FIELD = "test_mmg_state"
_STATE_CODES = {"bull": 1, "bear": 2}


@pytest.fixture
def state_field():
    spec = ProfileSpec(name="test-mmg-v1", calc_version="test/1", fields=(
        FieldSpec(name=_STATE_FIELD, kind="categorical", short="state", searched=True,
                  codes=_STATE_CODES, ui_name="Test MMG state"),
    ))
    with registered_profile(spec):
        yield spec


def _market_leaf(**over):
    leaf = {"id": "s1-market-adx", "field": "underlying_adx_14", "op": "<", "comparison": "<",
            "value": 25.0, "optimize": True, "value_min": 10.0, "value_max": 40.0,
            "value_step": 5.0, "mode_optimize": True, "mode_choices": ["off", "below", "above"]}
    leaf.update(over)
    return leaf


def _ordinary_leaf(**over):
    """A leaf on an ORDINARY (non-market) field -- proves the master gene is not added when a
    strategy carries only ordinary gates. One gene only (toggle_optimize), so the "everything
    else is byte-identical" tests below compare a single-key space."""
    leaf = {"id": "conf", "field": "confidence", "op": ">", "comparison": ">", "value": 50.0,
            "toggle_optimize": True}
    leaf.update(over)
    return leaf


def _market_exit_rule(rule_id="s1-mkt-exit-structure", *, marker=True, **over):
    """A market-exit rule shaped exactly like ``market_condition_templates.market_exit_rules``
    builds one: off by default behind a rule-level toggle, one market-field leaf, a close
    action. ``marker=True`` (default) mirrors ``_append_equity_market_condition_gates`` stamping
    ``market_master_gene`` on the rule; ``marker=False`` mirrors an option-family rule that
    happens to gate on the same registered field but through a code path that never stamps it."""
    rule = {"id": rule_id, "name": rule_id, "toggle_optimize": True, "enabled": False,
            "continue_processing": False,
            "conditions": {"id": f"{rule_id}-state", "type": "AND", "conditions": [
                {"id": f"{rule_id}-state", "field": "structure_state", "field_type": "numeric",
                 "op": "==", "comparison": "==", "mode": "bear", "value": 2.0}]},
            "actions": [{"action_type": "close"}]}
    if marker:
        rule["market_master_gene"] = True
    rule.update(over)
    return rule


def _ordinary_exit_rule(rule_id="floor-stop"):
    return {"id": rule_id, "name": rule_id, "continue_processing": False,
            "conditions": {"id": f"{rule_id}-root", "type": "AND", "conditions": [
                {"id": "has-pos", "field": "has_position", "field_type": "flag",
                 "op": "is_true", "comparison": "is_true"}]},
            "actions": [{"action_type": "adjust_stop_loss", "action_value": -8.0}]}


def _strategy(entry_leaves=(), exit_rules=(), with_ordinary_entry=True, *, marker=False):
    """A minimal S1-shaped strategy: one entry rule whose AND tree carries ``entry_leaves``
    (optionally preceded by an ordinary confidence gate, mirroring
    ``_append_equity_market_condition_gates`` appending market leaves onto the strategy's own
    entry tree), plus ``exit_rules`` verbatim.

    ``marker=True`` stamps the entry rule with ``market_master_gene`` -- mirrors the equity
    launcher path; the DEFAULT is False, so a caller must opt in explicitly, exactly like an
    option/exploration strategy that carries the SAME kind of market leaf but never gets marked.
    """
    kids = ([_ordinary_leaf()] if with_ordinary_entry else []) + [
        copy.deepcopy(lf) for lf in entry_leaves]
    entry_rule = {"id": "s1-entry", "name": "S1-entry", "continue_processing": False,
                 "actions": [{"action_type": "buy"}],
                 "conditions": {"id": "s1-root", "type": "AND", "conditions": kids}}
    if marker:
        entry_rule["market_master_gene"] = True
    return types.SimpleNamespace(entry_rules=[entry_rule],
                                 exit_rules=[copy.deepcopy(r) for r in exit_rules])


def _entry_leaves(decoded):
    return decoded["entry_rules"][0]["conditions"]["conditions"]


# ---------------------------------------------------------------------------
# presence in the search space
# ---------------------------------------------------------------------------
def test_absent_without_any_market_gene():
    """No market-condition profile at all: the master gene must not appear -- byte-identical
    gene list to every run before this feature."""
    space = collect_param_space(_strategy())
    assert "market:enabled" not in space
    assert list(space) == ["cond:conf:enabled"]


def test_absent_in_the_all_off_control_shape():
    """``--market-condition-mode all-off`` never appends the leaves/rules at all (the launcher
    skips ``_append_equity_market_condition_gates``/``_append_equity_market_exit_rules`` under
    that mode, so it never stamps the marker either), so the strategy template this module sees
    carries no market gene -- the master gene stays absent, exactly like the no-profile case."""
    space = collect_param_space(_strategy())  # what an all-off build leaves the template as
    assert "market:enabled" not in space


def test_option_or_exploration_shaped_strategy_with_market_leaves_gets_no_master_gene():
    """The SCOPE fix's core case (review finding 1): a strategy that carries a REAL
    market-condition leaf (same field an equity S1 leaf would use -- ``underlying_adx_14``, an
    O_LC/O_IC-shaped entry gate would look exactly like this) but was never marked by the equity
    launcher path must NOT collect the master gene. The leaf's OWN genes (mode/value) are still
    collected normally -- only the master gene is scoped, nothing else about the leaf changes."""
    space = collect_param_space(_strategy(entry_leaves=[_market_leaf()], marker=False))
    assert "market:enabled" not in space
    assert set(space) == {"cond:conf:enabled", "cond:s1-market-adx:value", "cond:s1-market-adx:mode"}


def test_option_or_exploration_shaped_market_exit_rule_gets_no_master_gene():
    """Same case, for an exit rule (the O_* family's market-exit templates share the SAME
    ``market_exit_rules`` builder as the equity path -- ``marker=False`` mirrors an option/
    exploration attachment that never stamps it)."""
    space = collect_param_space(_strategy(exit_rules=[_market_exit_rule(marker=False)]))
    assert "market:enabled" not in space
    assert "exit:s1-mkt-exit-structure:enabled" in space  # the rule's OWN toggle is unaffected


def test_present_with_an_entry_leaf_exactly_one_extra_master_gene_appended_last():
    """The positive case: an equity S1 whose entry rule carries the marker gets EXACTLY one
    master gene, last."""
    space = collect_param_space(_strategy(entry_leaves=[_market_leaf()], marker=True))
    assert "market:enabled" in space
    assert space["market:enabled"] == {"type": "int", "min": 0, "max": 1, "step": 1}
    # The market LEAF contributes its own 2 genes (value + mode); the master gene is the ONE
    # gene beyond what the leaf itself would collect, and it is the LAST key -- every other
    # gene's index (including the leaf's own two) is unchanged from a build with that leaf's
    # genes collected on their own, master gene aside.
    leaf_only_space = dict(space)
    leaf_only_space.pop("market:enabled")
    assert list(leaf_only_space) == ["cond:conf:enabled", "cond:s1-market-adx:value",
                                     "cond:s1-market-adx:mode"]
    without_market = _strategy()
    base_space = collect_param_space(without_market)
    # the ordinary gene's index is unchanged regardless of the market leaf/master gene.
    assert list(space)[:len(base_space)] == list(base_space)
    assert list(space)[-1] == "market:enabled"


def test_present_with_only_a_market_exit_rule_no_entry_leaf():
    space = collect_param_space(_strategy(exit_rules=[_market_exit_rule()]))  # marker=True default
    assert "market:enabled" in space
    assert list(space)[-1] == "market:enabled"


def test_present_with_both_entry_and_exit_market_genes_still_exactly_one_master_gene():
    space = collect_param_space(_strategy(entry_leaves=[_market_leaf()], marker=True,
                                          exit_rules=[_market_exit_rule(), _ordinary_exit_rule()]))
    assert sum(1 for g in space if g == "market:enabled") == 1
    assert list(space)[-1] == "market:enabled"


def test_the_marker_alone_is_sufficient_even_without_a_real_market_leaf():
    """The gate is purely the marker, not "does field-based detection find something": a
    strategy stamped but carrying no market field at all still collects the gene (it decodes to
    a harmless no-op, since decode's force-off finds nothing to force off) -- proves collection
    and resolution are two independent mechanisms, exactly as designed."""
    space = collect_param_space(_strategy(marker=True))
    assert "market:enabled" in space
    decoded = decode_params(_strategy(marker=True), {"market:enabled": 0})
    assert [lf["id"] for lf in _entry_leaves(decoded)] == ["conf"]  # nothing to force off


def test_bypass_strategy_never_gets_the_master_gene():
    """A bypass expert's handler drops cond:*/entry:*/exit:* entirely -- there is nothing for a
    master gene to control, so it must not be collected either, even when marked. Given a
    non-empty model:* space (so the call does not hit the "no optimizable parameters" refusal),
    the market leaf/marker are otherwise ignored on the bypass path."""
    expert_cfg = {"some_setting": {"optimize": True, "type": "float", "min": 0.0, "max": 1.0,
                                   "step": 0.1}}
    space = collect_param_space(_strategy(entry_leaves=[_market_leaf()], marker=True),
                                expert_cfg=expert_cfg, bypass=True)
    assert "market:enabled" not in space
    assert list(space) == ["model:some_setting"]


# ---------------------------------------------------------------------------
# decode: enabled=0 == every market mode off + every market exit toggle off
# ---------------------------------------------------------------------------
# Decode itself is marker-INDEPENDENT (see module docstring): the strategies below are built
# WITHOUT the marker on purpose, to prove decode's force-off behaviour has nothing to do with
# it -- only collect_param_space's GATING does.
def test_enabled_zero_is_byte_identical_to_every_market_gene_explicitly_off():
    strat = _strategy(entry_leaves=[_market_leaf()], exit_rules=[_market_exit_rule()])
    via_master = decode_params(strat, {"market:enabled": 0,
                                       "cond:s1-market-adx:mode": "above",  # ignored -- overridden
                                       "cond:s1-market-adx:value": 30.0,
                                       "exit:s1-mkt-exit-structure:enabled": 1})  # ignored too
    via_individual = decode_params(strat, {"cond:s1-market-adx:mode": "off",
                                           "exit:s1-mkt-exit-structure:enabled": 0})
    assert via_master == via_individual
    # concretely: the market leaf is gone from the entry tree, and the market exit rule is gone.
    assert [lf["id"] for lf in _entry_leaves(via_master)] == ["conf"]
    assert via_master["exit_rules"] == []


def test_enabled_zero_with_no_genome_override_for_the_individual_genes_still_forces_off():
    """The whole point of the master gene: it forces off EVEN WHEN the genome's own mode/toggle
    genes would have decoded to something live -- it is not merely a default."""
    strat = _strategy(entry_leaves=[_market_leaf()], exit_rules=[_market_exit_rule()])
    decoded = decode_params(strat, {
        "market:enabled": 0,
        "cond:s1-market-adx:mode": "above", "cond:s1-market-adx:value": 35.0,
        "exit:s1-mkt-exit-structure:enabled": 1,
    })
    assert [lf["id"] for lf in _entry_leaves(decoded)] == ["conf"]
    assert decoded["exit_rules"] == []


def test_enabled_one_leaves_the_individual_genes_in_force():
    strat = _strategy(entry_leaves=[_market_leaf()], exit_rules=[_market_exit_rule()])
    with_master = decode_params(strat, {
        "market:enabled": 1,
        "cond:s1-market-adx:mode": "above", "cond:s1-market-adx:value": 35.0,
        "exit:s1-mkt-exit-structure:enabled": 1,
    })
    without_master_key = decode_params(strat, {
        "cond:s1-market-adx:mode": "above", "cond:s1-market-adx:value": 35.0,
        "exit:s1-mkt-exit-structure:enabled": 1,
    })
    assert with_master == without_master_key
    leaf = next(lf for lf in _entry_leaves(with_master) if lf["id"] == "s1-market-adx")
    assert (leaf["op"], leaf["value"]) == (">", 35.0)
    assert len(with_master["exit_rules"]) == 1 and "enabled" not in with_master["exit_rules"][0]


def test_market_enabled_absent_from_the_genome_behaves_like_one():
    """A genome predating the feature (or a strategy with market genes whose genome simply omits
    the key) must decode exactly as if every individual gene were left in force."""
    strat = _strategy(entry_leaves=[_market_leaf()], exit_rules=[_market_exit_rule()])
    genome = {"cond:s1-market-adx:mode": "below", "cond:s1-market-adx:value": 15.0,
             "exit:s1-mkt-exit-structure:enabled": 0}
    assert decode_params(strat, genome) == decode_params(strat, dict(genome, **{"market:enabled": 1}))


def test_unknown_market_field_raises():
    with pytest.raises(ValueError, match="Unknown market gene field"):
        decode_params(_strategy(entry_leaves=[_market_leaf()]), {"market:bogus": 0})


def test_the_master_gene_does_not_mutate_the_template():
    strat = _strategy(entry_leaves=[_market_leaf()], exit_rules=[_market_exit_rule()])
    before_entry = copy.deepcopy(strat.entry_rules)
    before_exit = copy.deepcopy(strat.exit_rules)
    decode_params(strat, {"market:enabled": 0})
    assert strat.entry_rules == before_entry
    assert strat.exit_rules == before_exit


# ---------------------------------------------------------------------------
# persisted / exported strategy: no active market leaf or rule for enabled=0, and the marker
# itself never reaches a decoded/exported artifact.
# ---------------------------------------------------------------------------
def test_persisted_strategy_for_enabled_zero_carries_no_live_market_leaf_or_rule():
    """What top-N persist / the deploy export actually store: ``decoded['entry_rules']``/
    ``['exit_rules']`` (ba2test_launcher._persist_top_backtests writes these verbatim as
    strategy_params['entryRules']/['exitRules']). For enabled=0 that stored shape must contain
    no market leaf and no market exit rule -- live never has to know the master gene exists."""
    from ba2_common.core.market_condition_rules import iter_market_condition_leaves

    strat = _strategy(entry_leaves=[_market_leaf()], marker=True,
                      exit_rules=[_market_exit_rule(), _ordinary_exit_rule()])
    decoded = decode_params(strat, {"market:enabled": 0, "cond:s1-market-adx:mode": "above",
                                    "cond:s1-market-adx:value": 30.0})
    assert not list(iter_market_condition_leaves(decoded["entry_rules"], "entry"))
    assert not list(iter_market_condition_leaves(decoded["exit_rules"], "exit"))
    # the ordinary exit rule survives untouched.
    assert [r["id"] for r in decoded["exit_rules"]] == ["floor-stop"]


def test_persisted_strategy_for_enabled_one_still_carries_the_resolved_market_leaf_and_rule():
    from ba2_common.core.market_condition_rules import iter_market_condition_leaves

    strat = _strategy(entry_leaves=[_market_leaf()], marker=True, exit_rules=[_market_exit_rule()])
    decoded = decode_params(strat, {"market:enabled": 1, "cond:s1-market-adx:mode": "above",
                                    "cond:s1-market-adx:value": 30.0,
                                    "exit:s1-mkt-exit-structure:enabled": 1})
    assert list(iter_market_condition_leaves(decoded["entry_rules"], "entry"))
    assert [r["id"] for r in decoded["exit_rules"]] == ["s1-mkt-exit-structure"]


def test_the_marker_never_survives_onto_a_decoded_rule():
    """Template provenance, not a live artifact key -- stripped the same way mode_optimize is
    (``_apply_mode``'s docstring: "a decoded leaf is a RULE, not a template")."""
    strat = _strategy(entry_leaves=[_market_leaf()], marker=True, exit_rules=[_market_exit_rule()])
    decoded = decode_params(strat, {"market:enabled": 1, "cond:s1-market-adx:mode": "above",
                                    "cond:s1-market-adx:value": 30.0,
                                    "exit:s1-mkt-exit-structure:enabled": 1})
    for rule in decoded["entry_rules"] + decoded["exit_rules"]:
        assert "market_master_gene" not in rule
        assert "marketMasterGene" not in rule


# ---------------------------------------------------------------------------
# trial-config round trip (encode/decode through the real GeneticOptimizer)
# ---------------------------------------------------------------------------
def test_trial_config_round_trip_carries_the_master_gene():
    strat = _strategy(entry_leaves=[_market_leaf()], marker=True, exit_rules=[_market_exit_rule()])
    space = collect_param_space(strat)
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    flat_in = {"cond:conf:enabled": 1, "cond:s1-market-adx:mode": "off",
              "cond:s1-market-adx:value": 10.0,
              "exit:s1-mkt-exit-structure:enabled": 0, "market:enabled": 0}
    ind = opt.encode_params(flat_in)
    names = list(space)
    assert ind[names.index("market:enabled")] == 0
    flat = opt.decode_individual(ind)
    assert flat == flat_in
    decoded = decode_params(strat, flat)
    assert [lf["id"] for lf in _entry_leaves(decoded)] == ["conf"]
    assert decoded["exit_rules"] == []


def test_trial_config_round_trip_enabled_one_keeps_the_gate_live():
    strat = _strategy(entry_leaves=[_market_leaf()], marker=True)
    space = collect_param_space(strat)
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    flat_in = {"cond:conf:enabled": 1, "cond:s1-market-adx:mode": "above",
              "cond:s1-market-adx:value": 30.0, "market:enabled": 1}
    ind = opt.encode_params(flat_in)
    flat = opt.decode_individual(ind)
    assert flat == flat_in
    decoded = decode_params(strat, flat)
    leaf = next(lf for lf in _entry_leaves(decoded) if lf["id"] == "s1-market-adx")
    assert (leaf["op"], leaf["value"]) == (">", 30.0)


# ---------------------------------------------------------------------------
# categorical market leaf (the ta-structure-v1 shape) also honours the master gene
# ---------------------------------------------------------------------------
def test_categorical_market_leaf_is_forced_off_too(state_field):
    leaf = {"id": "s1-market-state", "field": _STATE_FIELD, "op": "==", "comparison": "==",
            "mode_optimize": True, "mode_choices": ["off", "bull", "bear"]}
    strat = _strategy(entry_leaves=[leaf], marker=True)
    space = collect_param_space(strat)
    assert "market:enabled" in space
    decoded = decode_params(strat, {"market:enabled": 0, "cond:s1-market-state:mode": "bear"})
    assert [lf["id"] for lf in _entry_leaves(decoded)] == ["conf"]
