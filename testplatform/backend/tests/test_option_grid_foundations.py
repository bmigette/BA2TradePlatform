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


def test_wheel_enters_by_selling_a_put(wheel_allowed):
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    actions = [a.get("action_type") for r in s.entry_rules for a in (r.get("actions") or [])]
    assert "sell_cash_secured_put" in actions, f"wheel entry actions were {actions}"


def test_wheel_writes_calls_ONLY_against_assigned_shares(wheel_allowed):
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


def test_wheel_overlay_is_reachable_not_appended(wheel_allowed):
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


def test_wheel_guards_against_stacking_a_second_call(wheel_allowed):
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    guards = [r for r in s.exit_rules
              if any(a.get("action_type") == "stop_processing" for a in (r.get("actions") or []))]
    assert guards, "no stop_processing guard; the overlay will re-fire every manage cycle"


def test_wheel_overlay_precedes_the_option_closes(wheel_allowed):
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


def test_wheel_reuses_O_CSPs_entry_gene_keys_verbatim(wheel_allowed):
    """Not cosmetic: it is what lets a stage-1 O_CSP winner seed an O_WHEEL job. encode_params
    drops keys the target space does not know, so a renamed entry rule would silently discard
    every entry gene of the seed."""
    m = _launcher()
    csp = {g for g in _space(m, "O_CSP") if g.startswith("entry:") or g.startswith("cond:")}
    wheel = {g for g in _space(m, "O_WHEEL") if g.startswith("entry:") or g.startswith("cond:")}
    assert csp == wheel, f"entry gene keys diverged: {sorted(csp ^ wheel)}"


# ==============================================================================================
# Task 4 — --gates-off, the smoke stage's "prove the plumbing, not the strategy" switch
# ==============================================================================================
#
# MECHANISM, AND WHY IT IS REMOVAL AND NOT A FLAG. The obvious implementation -- stamp
# ``enabled: False`` on each optional leaf -- is a no-op TWICE OVER on this call chain, and both
# halves were checked against the real code before this was written:
#
#   1. ``ConditionLeaf.to_canonical_dict`` rebuilds every leaf from DECLARED fields only, and
#      ``enabled`` is not one of them. ``normalize_trade_rules`` -- which BOTH option builders
#      call -- therefore deletes the key. (The same trap ``value_offset_from``'s own docstring
#      warns about: "an undeclared key is silently dropped by normalize_trade_rules".)
#   2. Nothing downstream reads a leaf-level ``enabled`` even when it survives.
#      ``triggers_from_condition_tree`` emits one EventAction trigger per leaf it walks, and the
#      GA's own ON/OFF toggle disables a gate by DELETING the child node
#      (``strategy_param_space._apply_to_tree``: "a child whose 'enabled' gene decoded to 0 is
#      dropped"), never by flagging it.
#
# A marked-but-present gate would have produced a smoke run that reports itself gates-off while
# every gate still fires -- precisely the confusion stage 0a exists to eliminate. Removing the
# leaf also removes its ``cond:<id>:enabled`` gene, so the GA cannot switch the gate back on for
# half the population, which a static flag could never have prevented either.


def _entry_leaves(rule):
    return rule["conditions"]["conditions"]


def _toggleable(rule):
    return [c for c in _entry_leaves(rule) if c.get("toggle_optimize")]


def test_gates_off_disables_every_optional_entry_gate():
    """Stage 0a's purpose: separate 'the plumbing is broken' from 'the strategy is bad'.

    iv_rank and iv_to_realized_vol fail CLOSED when IV is unmeasurable, and an options cache
    without greeks makes every gated individual trade nothing and score the zero-trade sentinel.
    With the gates off, 'traded nothing' can only mean data or wiring.

    ``toggle_optimize`` is the discriminator: a leaf carrying it is a strategy opinion the GA is
    already allowed to switch off; a leaf without it is a correctness guard.
    """
    m = _launcher()
    assert _toggleable(m._option_entry_rule("O_LC")), (
        "no toggleable gates found; the test is measuring the wrong thing")
    rule = m._option_entry_rule("O_LC", gates_off=True)
    assert _toggleable(rule) == [], (
        f"these gates are still in the tree: {[c['id'] for c in _toggleable(rule)]}")


def test_gates_off_leaves_the_structural_conditions_ALONE():
    """``has_no_position`` is not a strategy gate, it is a correctness guard. Dropping it would
    let the smoke run stack duplicate positions and mask the very plumbing it is testing."""
    m = _launcher()
    rule = m._option_entry_rule("O_LC", gates_off=True)
    assert [c["id"] for c in _entry_leaves(rule)] == ["o_lc-flat"]


def test_gates_off_defaults_to_false():
    m = _launcher()
    normal = m._option_entry_rule("O_LC")
    assert [c["id"] for c in _entry_leaves(normal)] == [
        "o_lc-signal", "o_lc-flat", "shared-gate_confidence", "o_lc-iv_rank",
        "shared-rel_volume", "o_lc-iv_rv", "o_lc-exp_profit"]


def test_gates_off_survives_normalisation_and_reaches_the_ENGINE():
    """The test that a marked-not-removed implementation fails.

    The engine never sees the rule dict the builder returns: it sees the EventAction triggers
    that ``normalize_trade_rules`` -> ``triggers_from_condition_tree`` produce from it. A gate
    that is still a leaf at that point is still evaluated, whatever it is flagged with.
    """
    from ba2_common.core.rule_builders import triggers_from_condition_tree
    from ba2_common.core.rule_models import normalize_trade_rules

    m = _launcher()
    rule = normalize_trade_rules([m._option_entry_rule("O_LC", gates_off=True)])[0]
    fired = {t["event_type"] for t in triggers_from_condition_tree(rule["conditions"]).values()}
    assert fired == {"has_no_position"}, f"the engine still evaluates {sorted(fired)}"


def test_gates_off_reaches_the_built_strategies_through_the_module_toggle(monkeypatch):
    """``_build_strategy`` dispatches ``_STRATEGY_BUILDERS[kind](kind)`` for option kinds, so the
    flag cannot travel as an argument; it rides the module-level toggle set at command entry,
    the same route ``--option-min-volume`` already takes. Single AND group shapes."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    m = _launcher()
    monkeypatch.setattr(m, "_OPTION_GATES_OFF", True)
    # O_WHEEL needs the engine-development override: it refuses to build by default because the
    # backtest sells assigned stock out from under the covered call the same bar it is written.
    monkeypatch.setenv("BA2_ALLOW_UNRUNNABLE_WHEEL", "1")
    for kind in ("O_LC", "O_CSP", "O_WHEEL", "OS1"):
        strat = m._build_strategy(kind, f"g-{kind}", "FMPRating")
        for rule in strat.entry_rules:
            fired = {t["event_type"]
                     for t in triggers_from_condition_tree(rule["conditions"]).values()}
            assert fired == {"has_no_position"}, f"{kind}/{rule.get('id')} still gates on {fired}"


ENTRY_GATE_GENE_MARKERS = ("signal", "gate_confidence", "iv_rank", "rel_volume", "iv_rv",
                           "exp_profit")


def test_gates_off_leaves_no_gene_the_GA_could_flip_back_ON(monkeypatch):
    """Removal, not marking, is what makes this hold: a gate that keeps its
    ``cond:<id>:enabled`` gene is switched back ON by roughly half the population, so a
    'gates-off' run would still be gated for most of its trials.

    The EXIT conditions (``tp`` / ``td`` / ``dte``) are deliberately untouched — stage 0a's pass
    criteria include "at least one structure CLOSES at or before expiry", which is exactly what
    those rules do. --gates-off is about entry gates only.
    """
    m = _launcher()
    on = {g for g in _space(m, "O_LC") if g.startswith("cond:")}
    entry_genes = {g for g in on if any(k in g for k in ENTRY_GATE_GENE_MARKERS)}
    assert entry_genes, "control failed: O_LC has no entry-gate genes even with the gates ON"
    monkeypatch.setattr(m, "_OPTION_GATES_OFF", True)
    off = {g for g in _space(m, "O_LC") if g.startswith("cond:")}
    assert off == on - entry_genes, (
        f"gates-off should remove exactly the entry-gate genes; diff={sorted(off ^ (on - entry_genes))}")


def test_gates_off_is_threaded_from_the_optimize_command():
    """A flag the CLI parses but never applies is exactly as inert as no flag at all -- the
    min_volume bug this suite's sibling (test_option_min_volume_wiring.py) was written for.

    ``_cmd_optimize`` sets the module toggle before it does anything else, so an unknown expert
    (which exits a few lines later) is enough to observe it.
    """
    from types import SimpleNamespace

    m = _launcher()
    assert m._OPTION_GATES_OFF is False
    with pytest.raises(SystemExit):
        m._cmd_optimize(SimpleNamespace(gates_off=True, expert="NoSuchExpert-for-the-test"))
    assert m._OPTION_GATES_OFF is True


def test_the_gates_off_flag_exists_on_the_optimize_command():
    """The parser is built inline in ``main()``, which chdirs into backend/ -- so it is
    exercised the way a user would: as a subprocess."""
    import os
    import subprocess

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    out = subprocess.run([sys.executable, "testplatform/ba2test_launcher.py", "optimize", "--help"],
                         capture_output=True, text=True, env=env, timeout=300)
    assert "--gates-off" in out.stdout, out.stdout[-2000:] + out.stderr[-2000:]


def test_option_jobs_can_size_a_full_notional_structure_at_spot_100():
    m = _launcher()
    cap = m._rm_opt_for("O_CSP")["max_virtual_equity_per_instrument_percent"]
    assert cap["max"] >= 50.0, (
        f"per-instrument cap tops out at {cap['max']}%; a cash-secured put at spot 100 reserves "
        f"$10,000, i.e. 50% of a $20k account, so it can never open")


@pytest.mark.parametrize("kind", ["S2", "O_STK"])
def test_the_equity_BASELINE_keeps_the_original_cap(kind):
    """O_STK is the control arm, and it is inside _OPTION_STRATEGY_KEYS.

    _build_strategy_stock -> _build_strategy_S2: O_STK IS the plain-equity baseline the option
    strategies are measured against. An earlier version of _rm_opt_for gated on
    _OPTION_STRATEGY_KEYS and so handed it 50%, which would have made it incomparable to S2 and
    to every prior O_STK run. Pinning only S2 could not catch that, because S2 is not in the set.
    """
    m = _launcher()
    assert m._rm_opt_for(kind)["max_virtual_equity_per_instrument_percent"]["max"] == 30.0


@pytest.mark.parametrize("kind", ["O_CC", "O_PP", "O_WHEEL", "O_CSP"])
def test_the_hundred_share_structures_DO_get_the_raised_cap(kind):
    """The mirror of the test above, and the reason the exclusion is O_STK alone.

    A covered call must fund 100 shares -- $10,000 at spot $100, i.e. 50% of a $20k account,
    exactly a cash-secured put's constraint. Gating on _PURE_OPTION_STRATEGIES would drop O_CC
    and O_PP to 30% and pin them at spot $60, which is what the raise exists to relieve.
    """
    m = _launcher()
    assert m._rm_opt_for(kind)["max_virtual_equity_per_instrument_percent"]["max"] == 50.0


def test_equity_jobs_keep_the_original_cap():
    """The same setting is read by the equity risk manager. Raising it globally would move every
    equity grid and make new results incomparable to old ones."""
    m = _launcher()
    assert m._rm_opt_for("S2")["max_virtual_equity_per_instrument_percent"]["max"] == 30.0


def test_the_full_notional_sizing_band_reaches_fifty_percent():
    m = _launcher()
    lo, hi, step = m._OPTION_SIZING_BANDS[20.0]
    assert hi >= 50.0, f"full-notional option_sizing band tops out at {hi}%"


def test_raising_one_range_alone_would_not_help():
    """Documents WHY both move: the budget is the MIN of the two."""
    m = _launcher()
    cap = m._rm_opt_for("O_CSP")["max_virtual_equity_per_instrument_percent"]["max"]
    sizing = m._OPTION_SIZING_BANDS[20.0][1]
    assert min(cap, sizing) >= 50.0


@pytest.mark.parametrize("cmd", ["_cmd_optimize", "_cmd_optimize_batch"])
def test_both_optimize_commands_build_the_gene_block_through_the_scoped_helper(cmd):
    """An override nobody calls is exactly as inert as no override -- the same failure mode as a
    CLI flag that is parsed and never applied. Both commands spread the classic-RM block into
    ``expert_params`` and BOTH have to go through ``_rm_opt_for`` or option jobs silently keep
    the 30% equity ceiling. Asserted on the source because the real path needs a DB + a queue."""
    import inspect

    src = inspect.getsource(getattr(_launcher(), cmd))
    assert "_rm_opt_for(" in src, f"{cmd} does not build its RM gene block through _rm_opt_for"
    assert "**_RM_OPT" not in src, f"{cmd} still spreads the unscoped _RM_OPT"


def test_gates_off_is_available_on_BOTH_optimize_and_optimize_batch():
    """A flag that parses and does nothing is worse than a missing flag.

    --gates-off sets a module-level toggle. _cmd_optimize set it; _cmd_optimize_batch did not,
    so `optimize-batch --gates-off` would have parsed cleanly, left every gate ON, and reported
    "no trades" -- which is precisely the ambiguity the smoke stage exists to eliminate, arriving
    with a green tick.
    """
    import argparse
    m = _launcher()
    parser = m._build_parser() if hasattr(m, "_build_parser") else None
    if parser is None:
        import inspect
        src = inspect.getsource(m)
        assert src.count('"--gates-off"') >= 2, (
            "--gates-off is declared on only one subcommand")
        assert "_OPTION_GATES_OFF" in src.split("def _cmd_optimize_batch")[1][:2000], (
            "_cmd_optimize_batch never sets the module toggle, so the flag is inert there")


@pytest.fixture
def wheel_allowed(monkeypatch):
    """O_WHEEL refuses to build unless the engine-development override is set.

    The structure is CORRECT; the ENGINE cannot run it. These tests assert the structure, so
    they set the override -- and the refusal itself is asserted separately below.
    """
    monkeypatch.setenv("BA2_ALLOW_UNRUNNABLE_WHEEL", "1")
    yield


def test_o_wheel_refuses_to_build_by_default():
    """A prose warning in a docstring is not a guard.

    The backtest liquidates assigned stock at the next bar's open (daily_engine step 4a-pre),
    AFTER the manage pass has written a covered call against it (step 3) -- so every wheel
    position the engine opens is a naked short call wearing a wheel's name. That does not
    produce bad numbers, it produces a DIFFERENT STRATEGY's numbers, which is worse because they
    look plausible. O_WHEEL is in _STRATEGY_BUILDERS, so any --strategies list containing it
    would have launched.
    """
    m = _launcher()
    with pytest.raises(SystemExit) as exc:
        m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    assert "naked short call" in str(exc.value)
    assert "Task 10" in str(exc.value), "the refusal must name the fix that unblocks it"


def test_o_wheel_stays_registered_so_the_fix_has_somewhere_to_land():
    """Refused at BUILD time, not deleted. The composition is right and tested; only the engine
    is missing, so removing the strategy would throw away correct work."""
    m = _launcher()
    assert "O_WHEEL" in m._STRATEGY_BUILDERS
