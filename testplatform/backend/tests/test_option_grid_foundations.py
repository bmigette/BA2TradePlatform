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
import os
import sys

import pytest


# tests/ -> backend/ -> testplatform/, then the launcher beside backend/. Resolved off
# __file__ rather than a CWD-relative string because CI runs with working-directory
# testplatform/backend and a bare "testplatform/ba2test_launcher.py" resolves differently
# there than from the repo root -- the exact failure that broke the parity workflow on
# 2026-08-28 (dcd12237). Every other launcher-loading test in this directory already
# resolves off __file__ (e.g. test_bull_put_spread_grid.py); this file was the one holdout.
_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch", _LAUNCHER_PATH)
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
    """The point is not only correctness: 8 genes out, 2 in, on every structure.

    The cap moved 26 -> 28 when ``opt_sl_ml`` landed (Task 9): every MEASURED-max-loss
    structure deliberately gained exactly two genes (``exit:opt_sl_ml:enabled`` +
    ``cond:sl_ml:value``). It moved 28 -> 31 when the three SelectionPolicy weight genes
    landed (Task 10): every pure-option member gains EXACTLY its half's shared
    ``optsel:<half>:{w_premium,w_iv,w_rvol}`` -- 3 genes, hand-derived, never more, because
    the sharing tier collapses them per half rather than per member. The budget still
    bites -- an accidental fourth gene anywhere fails it."""
    m = _launcher()
    for key in PURE:
        assert len(_space(m, key)) <= 31, (
            f"{key} genome is {len(_space(m, key))}; the price-gate swap plus the two "
            f"opt_sl_ml genes plus the three shared selection-weight genes should put "
            f"every structure at or under 31 genes")


def test_the_group_genome_shrinks_too():
    """95 -> 98 with Task 10's weight genes: a GROUP also gains exactly 3 (every launcher
    group is single-half, so the shared tier adds one set, not one per member -- OS1
    measured 81 before, 84 after)."""
    m = _launcher()
    assert len(_space(m, "OS1")) <= 98, "OS1 should sit at ~84: 81 + the 3 shared weights"


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
    out = subprocess.run([sys.executable, _LAUNCHER_PATH, "optimize", "--help"],
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


def test_o_wheel_builds_by_default():
    """It used to REFUSE, by design, and the refusal is now gone because its precondition is.

    The engine liquidated assigned stock at the next bar's open (daily_engine step 4a-pre),
    AFTER the manage pass had written a covered call against it (step 3), so every wheel
    position the engine opened was a naked short call wearing a wheel's name -- a DIFFERENT
    STRATEGY's numbers, which is worse than bad ones because they look plausible. Plan Task 10
    added ``BacktestAccount``'s ``hold_assigned_stock`` (default OFF), and the test below is what
    makes the removal of the refusal safe rather than merely quiet: it proves the wheel's run
    config actually turns the setting ON.
    """
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    assert s is not None and s.entry_rules and s.exit_rules
    assert "O_WHEEL" in m._STRATEGY_BUILDERS


def test_o_wheel_run_config_holds_assigned_stock(monkeypatch):
    """The wheel is only a wheel if the ENGINE keeps the assigned shares.

    Asserted where the GA actually reads it: this drives the REAL ``_cmd_optimize`` (the same
    harness ``test_equity_cap_launcher`` uses for --equity-cap, with only the GA and the top-N
    persistence stubbed) and reads ``hold_assigned_stock`` back off the persisted
    ``StrategyOptimization.optimization_config``. Asserting ``_hold_assigned_stock("O_WHEEL")``
    alone would pass with the helper wired to nothing.
    """
    from tests.test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize

    args = _parse(_BASE_ARGV + ["--strategy", "O_WHEEL"])
    cfg = _run_optimize(args, monkeypatch)
    assert cfg["backtest"]["account_settings"]["hold_assigned_stock"] is True, (
        "the wheel's run config does not hold assigned stock, so the engine sells the shares "
        "out from under the covered call on the bar it is written -- a naked short call"
    )


def test_only_the_wheel_holds_assigned_stock(monkeypatch):
    """The mirror, and the reason this is per-strategy rather than a CLI flag.

    A run whose rules cannot sell stock must keep the no-orphaned-stock liquidation; O_CSP is
    exactly that case (it sells puts and its exits are all ``close_option``), so if it ever
    started holding, its assigned shares would ride unmanaged to the end of the run.
    """
    from tests.test_equity_cap_launcher import _BASE_ARGV, _parse, _run_optimize

    cfg = _run_optimize(_parse(_BASE_ARGV + ["--strategy", "O_CSP"]), monkeypatch)
    assert cfg["backtest"]["account_settings"]["hold_assigned_stock"] is False


def test_the_hold_set_is_explicit_about_which_kinds_manage_stock():
    """One membership set, so adding a stock-managing structure is one line next to the reason."""
    m = _launcher()
    assert m._HOLDS_ASSIGNED_STOCK == {"O_WHEEL"}
    assert m._hold_assigned_stock("O_WHEEL") is True
    assert m._hold_assigned_stock("O_CSP") is False
    assert m._hold_assigned_stock(None) is False, \
        "bypass experts ignore --strategy; their account settings must not inherit it"


# ==============================================================================================
# Task 9 -- opt_sl_ml: a stop at a % of the structure's MEASURED max loss (design 2026-08-29 S6)
# ==============================================================================================
#
# THE EMISSION PREDICATE IS "MEASURED max_loss", NOT "defined-risk spread" AND NOT "no naked
# short" (the corrected S6 rule, 2026-08-30): a cash-secured put is bounded below -- strike
# minus credit, at an underlying of zero -- so it IS measured and DOES carry the rule; only a
# net-uncovered short CALL (short strangle / straddle) is genuinely unbounded. Emission is a
# strategy-level APPLICABILITY gate, not the safety mechanism: even where emitted, the
# condition self-disarms on any position whose parent order lacks the persisted
# max_loss_per_contract stamp (test_max_loss_persisted_at_submit.py pins that submit-side
# guarantee). [2026-08-31: the wheel's covered-call legs, unstamped by design in Task 8, now
# STAMP -- the builder supplies its verified stock cover, so the CC phase measures
# (spot - credit) x 100 and opt_sl_ml can drive it. The self-disarm remains the safety net
# for any genuinely unstamped order.]

SL_ML_RULE_ID = "opt_sl_ml"
SL_ML_COND_ID = "sl_ml"


def _exit_rule(m, kind, rule_id):
    for r in m._option_exit_rules(kind):
        if r["id"] == rule_id:
            return r
    raise AssertionError(f"{kind}: exit rule {rule_id!r} not found "
                         f"(have {[r['id'] for r in m._option_exit_rules(kind)]})")


def test_opt_sl_ml_is_never_emitted_for_a_member_whose_max_loss_is_unbounded():
    """Asserted over ALL members, not spot-checked (plan Task 9's 'test that matters most')."""
    m = _launcher()
    for kind in m._OPTION_STRATS:
        ids = {r["id"] for r in m._option_exit_rules(kind)}
        if kind in m._UNDEFINED_RISK_MEMBERS:
            assert SL_ML_RULE_ID not in ids, f"{kind} has no max loss to take a fraction of"
        else:
            assert SL_ML_RULE_ID in ids, f"{kind} has a MEASURED max loss and no {SL_ML_RULE_ID}"


def test_the_unbounded_set_is_exactly_the_uncovered_short_call_structures():
    """The corrected S6 rule, pinned as data: a short strangle and a short straddle each carry
    a short call no other leg of the order covers; NOTHING else in the taxonomy does. In
    particular O_CSP (short put: bounded at strike minus credit), O_JL (its short call is
    covered by the long wing) and O_RS (all puts) are MEASURED -- listing any of them here is
    the stale pre-correction table row this branch explicitly retired."""
    m = _launcher()
    assert m._UNDEFINED_RISK_MEMBERS == {"O_SSTG", "O_SSTD"}


# ==============================================================================================
# The grid stops SEARCHING unbounded structures (operator decision 2026-08-31,
# AskUserQuestion: "Only truly unbounded"). O_SSTG/O_SSTD leave the searched set; everything
# else stays (CSP/wheel stay -- the rails size them as uncovered risk). The structures remain
# fully SUPPORTED in code: builders, reserve math and settlement are untouched, and an
# EXPLICIT single request refuses loudly instead of silently running or silently skipping.
# ==============================================================================================
def test_no_searched_group_contains_an_unbounded_structure():
    """The searched set, member by member. Hand-derived from _OPTION_GROUPS_ALL:
    OS1 [O_LC,O_LP,O_VERT,O_BF,O_BULLCS] loses nothing; OS2 [O_SSTG,O_SSTD,O_IC,O_CSP]
    loses O_SSTG/O_SSTD (risk, 2026-08-31) AND O_CSP (affordability,
    _FULL_NOTIONAL_OPTION_KINDS -- a standing, separate decision) leaving [O_IC];
    OS3 [O_JL,O_RS,O_BEARCS,O_BULLPS] loses O_JL/O_RS (affordability) leaving
    [O_BEARCS,O_BULLPS]; OS4 [O_STRD,O_STRG] loses nothing."""
    m = _launcher()
    for group, members in m._OPTION_GROUPS.items():
        assert not set(members) & m._UNDEFINED_RISK_MEMBERS, (group, members)
    assert m._OPTION_GROUPS["OS2"] == ["O_IC"]
    assert m._OPTION_GROUPS["OS3"] == ["O_BEARCS", "O_BULLPS"]
    # The TAXONOMY keeps them: the exclusion shrinks what is searched, never what exists.
    assert set(m._OPTION_GROUPS_ALL["OS2"]) >= {"O_SSTG", "O_SSTD"}


def test_the_search_exclusion_is_driven_by_the_one_unbounded_definition():
    """No second hand-written list: the searched groups are exactly _OPTION_GROUPS_ALL
    minus the two named sets. A mutant that forks its own idea of 'unbounded' (or of
    'unaffordable') diverges from this recomputation and dies."""
    m = _launcher()
    assert m._OPTION_GROUPS == {
        key: [mem for mem in members
              if mem not in m._FULL_NOTIONAL_OPTION_KINDS
              and mem not in m._UNDEFINED_RISK_MEMBERS]
        for key, members in m._OPTION_GROUPS_ALL.items()
    }


@pytest.mark.parametrize("bad", ["O_SSTG", "O_SSTD"])
def test_an_explicit_unbounded_single_refuses_loudly(bad):
    """`ba2-test optimize --strategy O_SSTG` dies citing the decision -- BEFORE any DB
    row is created. Never silently runs, never silently skips."""
    from types import SimpleNamespace as NS

    m = _launcher()
    with pytest.raises(SystemExit) as e:
        m._cmd_optimize(NS(expert="FMPRating", strategy=bad))
    msg = str(e.value)
    assert bad in msg and "2026-08-31" in msg and "Only truly unbounded" in msg


def test_the_batch_command_refuses_the_same_keys_with_the_same_message():
    m = _launcher()
    with pytest.raises(SystemExit) as e:
        m._refuse_unbounded_strategy_request("optimize-batch", ["S2", "O_SSTD"])
    assert "O_SSTD" in str(e.value) and "2026-08-31" in str(e.value)
    # A request with no unbounded key passes through untouched.
    assert m._refuse_unbounded_strategy_request("optimize-batch", ["S2", "O_IC"]) is None


def test_the_builders_stay_fully_supported():
    """Only the SEARCH shrank. The rows, builders and genome derivation for the excluded
    structures are untouched -- an explicit non-grid consumer (or a future re-admission)
    finds them exactly as they were."""
    m = _launcher()
    for kind in ("O_SSTG", "O_SSTD"):
        assert kind in m._OPTION_STRATS
        assert kind in m._STRATEGY_BUILDERS
        s = m._build_strategy(kind, f"g-{kind}", "FMPRating")
        assert s.entry_rules, kind


def test_a_cash_secured_put_carries_the_max_loss_stop():
    """THE CORRECTED-RULE CASE. A naked short put is not 'undefined risk': its loss is bounded
    at (strike - credit) x 100, the submit path stamps that measurement
    (test_a_naked_short_put_submit_STAMPS_its_measured_max_loss_corrected_s6), and so the
    stop's denominator exists. Emit-for-CSP is the assertion the stale design table would
    have failed."""
    m = _launcher()
    assert SL_ML_RULE_ID in {r["id"] for r in m._option_exit_rules("O_CSP")}


def test_the_rule_body_matches_design_s6_exactly():
    """Field, op, default, band and both toggles -- the shape design S6 spells out verbatim.
    op is '>' on a POSITIVE loss percentage (the condition negates the P&L), value_step 5
    against opt_sl's 25: max-loss fractions are scale-free so the finer grid is affordable."""
    m = _launcher()
    rule = _exit_rule(m, "O_VERT", SL_ML_RULE_ID)
    assert rule["action_type"] == "close_option"
    assert rule["toggle_optimize"] is True
    leaves = rule["conditions"]["conditions"]
    assert [c["field"] for c in leaves] == ["loss_pct_of_max_loss"]
    leaf = leaves[0]
    assert leaf["id"] == SL_ML_COND_ID
    assert leaf["op"] == ">"
    assert leaf["value"] == 50
    assert leaf["optimize"] is True
    assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (25, 75, 5)


def test_the_rule_is_identical_across_every_carrying_member():
    """One shape, no per-member drift: the threshold is scale-free BECAUSE the denominator is
    each structure's own max loss, so nothing about the band may vary by structure."""
    m = _launcher()
    reference = _exit_rule(m, "O_VERT", SL_ML_RULE_ID)
    for kind in m._OPTION_STRATS:
        if kind not in m._UNDEFINED_RISK_MEMBERS:
            assert _exit_rule(m, kind, SL_ML_RULE_ID) == reference, kind


def test_a_group_with_any_measured_member_carries_the_rule_once():
    """Groups share ONE exit list, and every SEARCHED group now consists of measured
    members only (the unbounded O_SSTG/O_SSTD left the searched set 2026-08-31 -- when
    OS2 still mixed them with O_IC, the rule was emitted and self-disarmed on the
    unstamped orders via Task 8's absence guarantee). The any-member-measured predicate
    is retained and pinned by the hypothetical-group test below."""
    m = _launcher()
    for group in m._OPTION_GROUPS:
        ids = [r["id"] for r in m._option_exit_rules(group)]
        assert ids.count(SL_ML_RULE_ID) == 1, (group, ids)


def test_a_hypothetical_all_unbounded_group_would_not_carry_it(monkeypatch):
    """The group predicate is 'any member measured', so a family of nothing but naked short
    calls emits no rule -- pinned against the cheap regression of emitting unconditionally
    for every group key."""
    m = _launcher()
    monkeypatch.setitem(m._OPTION_GROUPS, "OS_TEST_UNBOUNDED", ["O_SSTG", "O_SSTD"])
    ids = {r["id"] for r in m._option_exit_rules("OS_TEST_UNBOUNDED")}
    assert SL_ML_RULE_ID not in ids


def test_the_wheel_inherits_the_rule_from_o_csp():
    """The mixed-strategy case from the plan: the wheel's CSP entry stamps a measured max
    loss, and -- since the 2026-08-31 covered-call decision -- so does its covered-call
    overlay (the builder supplies its verified stock cover; (spot - credit) x 100). The
    rule rides along and drives BOTH phases; Task 8's absence gate remains the safety net
    for any genuinely unstamped order."""
    m = _launcher()
    s = m._build_strategy("O_WHEEL", "g-wheel", "FMPRating")
    assert SL_ML_RULE_ID in [r.get("id") for r in s.exit_rules]


def test_both_stops_coexist_on_a_credit_carrier():
    """Design S6: two independently toggleable rules, not a basis gene on opt_sl -- the GA
    selects the basis by toggling which rule is live. A measured CREDIT structure therefore
    carries BOTH stops."""
    m = _launcher()
    ids = [r["id"] for r in m._option_exit_rules("O_IC")]
    assert "opt_sl" in ids and SL_ML_RULE_ID in ids


# ---------------------------------------------------------------------------
# genome -> ruleset: the genes exist where (and only where) the rule does, and they MOVE it
# ---------------------------------------------------------------------------

def test_the_genes_are_emitted_for_measured_strategies_only():
    m = _launcher()
    for kind in ["O_VERT", "O_CSP", "O_IC"]:
        space = _space(m, kind)
        assert space[f"exit:{SL_ML_RULE_ID}:enabled"] == {
            "type": "int", "min": 0, "max": 1, "step": 1}, kind
        assert space[f"cond:{SL_ML_COND_ID}:value"] == {
            "type": "float", "min": 25.0, "max": 75.0, "step": 5.0}, kind
    for kind in ["O_SSTG", "O_SSTD"]:
        space = _space(m, kind)
        assert f"exit:{SL_ML_RULE_ID}:enabled" not in space, kind
        assert f"cond:{SL_ML_COND_ID}:value" not in space, kind


def test_the_threshold_gene_decodes_onto_the_condition():
    """Kills the hardcoded-threshold mutant: a decoded 30 must land on the leaf, not be
    shadowed by the literal 50."""
    from app.services.strategy_param_space import decode_params

    m = _launcher()
    s = m._build_strategy_option("O_VERT")
    decoded = decode_params(s, {f"cond:{SL_ML_COND_ID}:value": 30})
    rule = next(r for r in decoded["exit_rules"] if r["id"] == SL_ML_RULE_ID)
    leaf = next(c for c in rule["conditions"]["conditions"] if c["id"] == SL_ML_COND_ID)
    assert leaf["value"] == 30
    assert leaf["field"] == "loss_pct_of_max_loss"
    assert leaf["op"] == ">"


def test_toggling_the_rule_off_drops_it_and_spares_the_siblings():
    from app.services.strategy_param_space import decode_params

    m = _launcher()
    s = m._build_strategy_option("O_IC")
    assert SL_ML_RULE_ID in [r["id"] for r in s.exit_rules]
    decoded = decode_params(s, {f"exit:{SL_ML_RULE_ID}:enabled": 0})
    ids = [r["id"] for r in decoded["exit_rules"]]
    assert SL_ML_RULE_ID not in ids
    assert "opt_sl" in ids and "opt_tp" in ids


def test_the_sl_ml_leaf_becomes_a_real_engine_trigger():
    """A field missing from rule_builders' FIELD_EVENT map is silently DROPPED by
    triggers_from_condition_tree and the GA tunes a gene the engine cannot see -- the exact
    dead-gene failure the OS1 price gates shipped. Prove the leaf survives seeding."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    m = _launcher()
    rule = _exit_rule(m, "O_VERT", SL_ML_RULE_ID)
    triggers = triggers_from_condition_tree(rule["conditions"])
    assert [t["event_type"] for t in triggers.values()] == ["loss_pct_of_max_loss"]
    assert list(triggers.values())[0]["operator"] == ">"
