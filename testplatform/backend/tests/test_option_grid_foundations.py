"""The option entry rule must gate on a signal EVERY expert produces.

WHY THIS CHANGED. The four price_* gates read price_vs_target_low_percent /
price_vs_target_high_percent, and PriceVsTargetLowCondition is hard-keyed to
expert_recommendation.data["FMPRating"]["target_low"]. Only FMPRating writes target_low, so
under any other expert all four gates fail CLOSED -- 8 of ~28 genes per structure, and any
genome enabling one trades nothing. That is the pathology the launcher already records for the
confidence gate in opt 333 (enabled 0.80 in dead genomes vs 0.14 in trading ones).

expected_profit_percent is NON-NULLABLE on ExpertRecommendation, so every expert produces it,
and N_EXPECTED_PROFIT_TARGET_PERCENT already exists as a condition. target_price is nullable and
DERIVES from expected_profit_percent when absent -- the model's own field description says so --
so the two are the same signal and one gate replaces four.
"""
import importlib.util
import sys

import pytest


def _launcher():
    spec = importlib.util.spec_from_file_location("lch", "testplatform/ba2test_launcher.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _space(m, key, expert="FMPRating"):
    from app.services.strategy_param_space import collect_param_space
    return collect_param_space(m._build_strategy(key, f"g-{key}", expert))


PURE = ["O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS", "O_BEARCS", "O_BULLPS",
        "O_CSP", "O_IC", "O_JL", "O_RS", "O_SSTD", "O_SSTG", "O_STRD", "O_STRG"]


@pytest.mark.parametrize("key", PURE)
def test_no_structure_gates_on_the_analyst_target_range(key):
    """price_vs_target_* is FMPRating-only data. Nothing may depend on it."""
    genes = _space(_launcher(), key)
    offenders = [g for g in genes if "price_low" in g or "price_high" in g]
    assert not offenders, f"{key} still carries FMPRating-only price gates: {offenders}"


@pytest.mark.parametrize("key", PURE)
def test_every_structure_gates_on_expected_profit(key):
    genes = _space(_launcher(), key)
    assert any("exp_profit" in g for g in genes), (
        f"{key} has no expected-profit gate; the entry has no universal signal gate at all")


@pytest.mark.parametrize("key", PURE)
def test_the_expected_profit_gate_is_searchable_and_toggleable(key):
    genes = _space(_launcher(), key)
    assert any(g.endswith("-exp_profit:value") for g in genes)
    assert any(g.endswith("-exp_profit:enabled") for g in genes)


def test_the_swap_shrinks_every_structure_genome():
    """The point is not only correctness: 8 genes out, 2 in, on every structure."""
    m = _launcher()
    for key in PURE:
        assert len(_space(m, key)) <= 26, (
            f"{key} genome is {len(_space(m, key))}; the price-gate swap should put every "
            f"structure at or under 26 genes")


def test_the_group_genome_shrinks_too():
    m = _launcher()
    assert len(_space(m, "OS1")) <= 95, "OS1 should fall from 120 to ~90 after the swap"


def test_no_value_offset_from_survives_in_an_option_entry_rule():
    """The four price gates chained via value_offset_from, and those offsets resolve their base
    against the GLOBAL gene map -- so any future shared condition id would have silently coupled
    members across a family. Removing the gates removes the trap; keep it removed."""
    m = _launcher()
    strat = m._build_strategy("OS1", "g-OS1", "FMPRating")

    def walk(node, out):
        if isinstance(node, list):
            for n in node:
                walk(n, out)
        elif isinstance(node, dict):
            if "value_offset_from" in node:
                out.append(node.get("id"))
            for v in node.values():
                walk(v, out)
        return out

    assert walk(strat.entry_rules, []) == []


SHARED = ["rel_volume", "gate_confidence"]


@pytest.mark.parametrize("gate", SHARED)
def test_the_expert_independent_gates_are_shared_across_group_members(gate):
    """One gene for the whole family, not one per member.

    These two are shared because their SEMANTICS do not vary by structure -- the launcher's own
    comment on rel_volume says "the searched threshold is the only per-half difference, and there
    is none". iv_rank and iv_to_realized_vol are NOT shared and must not be: their operator flips
    between debit (`<`, buy cheap vol) and credit (`>`, sell rich vol) members, and the GA never
    searches an operator, so a shared gate there is not expressible at all.
    """
    genes = [g for g in _space(_launcher(), "OS1") if gate in g]
    assert genes, f"{gate} produced no genes at all"
    assert all(g.startswith(f"cond:shared-{gate}") for g in genes), (
        f"{gate} is still per-member: {sorted(genes)}")
    assert len(genes) == 2, f"expected exactly value+enabled, got {sorted(genes)}"


@pytest.mark.parametrize("gate", ["iv_rank", "iv_rv"])
def test_the_direction_dependent_gates_stay_per_member(gate):
    genes = [g for g in _space(_launcher(), "OS1") if gate in g]
    assert not any("shared-" in g for g in genes), (
        f"{gate}'s operator flips debit/credit; sharing it is not expressible")
    assert len(genes) == 10, f"OS1 has 5 members, so {gate} should emit 10 genes"


@pytest.mark.parametrize("gate", SHARED)
def test_single_and_group_jobs_use_the_SAME_key_for_a_shared_gate(gate):
    """The seeding requirement. A stage-1 single-structure winner is later encoded into the
    stage-2 group space; a key present in one and absent from the other is dropped silently by
    encode_params, so the shared gates must key identically in both shapes."""
    m = _launcher()
    single = {g for g in _space(m, "O_LC") if gate in g}
    group = {g for g in _space(m, "OS1") if gate in g}
    assert single == group, f"{gate} keys differ: single={sorted(single)} group={sorted(group)}"


# ==============================================================================================
# Task 3 — O_WHEEL
# ==============================================================================================


def test_wheel_is_a_registered_strategy():
    m = _launcher()
    assert "O_WHEEL" in m._STRATEGY_BUILDERS
    assert "O_WHEEL" in m._OPTION_STRATEGY_KEYS


def test_wheel_enters_by_selling_a_put():
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    actions = [a.get("action_type") for r in s.entry_rules for a in (r.get("actions") or [])]
    assert "sell_cash_secured_put" in actions, f"wheel entry actions were {actions}"


def test_wheel_writes_calls_ONLY_against_assigned_shares():
    """The distinction that makes it a wheel.

    Gating on has_position would write calls against any stock the expert holds;
    has_assigned_shares writes them only against shares the wheel's own put put you into. The
    condition exists and is tested as a rule trigger in tests/test_wheel_assignment_order.py.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    fields = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if node.get("field"):
                fields.append(node["field"])
            for v in node.values():
                walk(v)

    walk(s.exit_rules)
    assert "has_assigned_shares" in fields, f"wheel overlay gates on {sorted(set(fields))}"
    cc_rules = [r for r in s.exit_rules
                if any(a.get("action_type") == "sell_covered_call" for a in (r.get("actions") or []))]
    assert cc_rules, "wheel has no covered-call overlay rule"


def test_wheel_overlay_is_reachable_not_appended():
    """The bug that made every historical O_CC number a mislabelled equity run.

    An overlay appended AFTER S2's floor stop can never fire -- that rule is conditioned only on
    has_position, matches every managed position, and declares no toggle gene so the GA cannot
    route around it. O_CC and O_PP, two OPPOSITE strategies, produced byte-identical top-5
    results with zero trades carrying a contract symbol because of it. The overlay must be
    SPLICED before the first stop-adjusting rule.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    ids = [r.get("id") for r in s.exit_rules]
    assert "cc_sell" in ids, f"no overlay rule in {ids}"
    assert ids.index("cc_sell") < len(ids) - 1, (
        f"the overlay is LAST in the exit list, which is the appended-and-unreachable shape: {ids}")


def test_wheel_guards_against_stacking_a_second_call():
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    guards = [r for r in s.exit_rules
              if any(a.get("action_type") == "stop_processing" for a in (r.get("actions") or []))]
    assert guards, "no stop_processing guard; the overlay will re-fire every manage cycle"


def test_wheel_overlay_precedes_the_option_closes():
    """The pure-option list has no adjust_* rule, so 'not last' is not enough.

    ``opt_tp`` (profit_loss_percent >) and ``opt_time`` (days_opened >) compare fields an
    assigned-STOCK position also carries, so either can match on the very position the overlay
    exists to cover and break the first-match walk with a close_option that has no option to
    close. Appending the pair anywhere behind them re-creates OPT-B1 in a quieter form.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    ids = [r.get("id") for r in s.exit_rules]
    assert ids.index("cc_guard") < ids.index("cc_sell"), (
        f"the NOT-idiom guard only works if it evaluates first: {ids}")
    for closer in ("opt_tp", "opt_time", "opt_dte"):
        assert ids.index("cc_sell") < ids.index(closer), (
            f"{closer} can match on an assigned-stock position and shadow the overlay: {ids}")


def test_wheel_is_scored_and_railed_as_a_PURE_option_kind():
    """It has no _OPTION_STRATS row (it reuses O_CSP's entry), but it is not an equity strategy.

    Both consumers of _PURE_OPTION_STRATEGIES would be wrong if the wheel were classed with the
    equity-entry overlays: its book is an option book, so calmar/sharpe would reward the
    barely-trading configs the option metric exists to reject, and it reads the options cache, so
    an unrailed window would spend the reserved 2026 walk-forward set.
    """
    m = _launcher()
    assert "O_WHEEL" in m._PURE_OPTION_STRATEGIES
    assert m._resolve_fitness(None, "O_WHEEL", "calmar_ratio") == "option_consistent_annual_return"
    with pytest.raises(SystemExit):
        m._assert_option_window_excludes_holdout(["O_WHEEL"], "2026-03-01")


def test_wheel_reuses_O_CSPs_entry_gene_keys_verbatim():
    """Not cosmetic: it is what lets a stage-1 O_CSP winner seed an O_WHEEL job. encode_params
    drops keys the target space does not know, so a renamed entry rule would silently discard
    every entry gene of the seed."""
    m = _launcher()
    csp = {g for g in _space(m, "O_CSP") if g.startswith("entry:") or g.startswith("cond:")}
    wheel = {g for g in _space(m, "O_WHEEL") if g.startswith("entry:") or g.startswith("cond:")}
    assert csp == wheel, f"entry gene keys diverged: {sorted(csp ^ wheel)}"
