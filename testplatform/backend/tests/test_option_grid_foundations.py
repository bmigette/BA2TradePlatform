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
# guarantee), so a composite like the wheel may carry the rule and it simply never fires on
# the unstamped covered-call legs. [2026-08-31 made the CC stamp; review finding M3,
# 2026-09-01, reversed that -- the stamp is measured from the order's OWN legs, so a covered
# call stamps nothing and the self-disarm is again what keeps the rule inert on it. The
# verified cover still reaches the option RM's admission, a different question.]

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
    each structure's own max loss, so nothing about the band may vary by structure.

    ``enabled`` IS EXCLUDED FROM THE COMPARISON, and only ``enabled`` (2026-09-01, grid 2).
    It is not part of the rule's SHAPE -- it is the authored DEFAULT of the on/off gene every
    carrying member still emits, and design 2026-08-31 section 2 sets it OFF for the
    binary-thesis keys ("opt_sl_ml searchable, default OFF -- the thesis is binary; a stop
    mid-event amputates it") on O_ERN / O_CBS / O_PBS. Excluding the whole rule for those
    three would have been the easy fix and the wrong one: the invariant this test exists for
    is that the FIELD, OP, DEFAULT THRESHOLD, BAND and ``toggle_optimize`` never vary by
    structure, and all five are still compared on every member.
    ``test_opt_sl_ml_is_authored_OFF_but_still_searched`` pins the excluded key's values on
    the three keys that set it, so the exclusion cannot hide a change to it.
    """
    m = _launcher()

    def _shape(rule):
        return {k: v for k, v in rule.items() if k != "enabled"}

    reference = _shape(_exit_rule(m, "O_VERT", SL_ML_RULE_ID))
    for kind in m._OPTION_STRATS:
        if kind not in m._UNDEFINED_RISK_MEMBERS:
            assert _shape(_exit_rule(m, kind, SL_ML_RULE_ID)) == reference, kind


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
    loss, its covered-call overlay does not -- the rule rides along and Task 8's absence
    gate keeps it inert on the unstamped legs. (2026-08-31 briefly made the CC stamp;
    review finding M3, 2026-09-01, put it back, so this is again the original reading.)"""
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


# =============================================================================
# THE RISK-MANAGER MODE REACHES A TRIAL (review finding M5, 2026-09-01)
#
# classic_options was selectable by a live expert and by a hand-written config,
# and by NO GRID JOB: nothing in the launcher ever wrote risk_manager_mode onto
# an expert's run settings, so the option risk manager could not be searched at
# all. _expert_run_settings is the one plumbing point; absent stays the default.
# =============================================================================
def test_no_shipped_expert_spec_selects_a_risk_manager_mode():
    """THE GOLDEN NO-OP. Every expert the grid ships must produce the settings dict it
    always did -- no risk_manager_mode key at all, so every existing job is byte-identical.
    A spec that wants the option rails has to say so, deliberately, one expert at a time."""
    m = _launcher()
    for name, spec in m._EXPERT_OPT.items():
        assert "risk_manager_mode" not in spec, name
        settings = m._expert_run_settings(spec, ["AAPL", "MSFT"])
        assert "risk_manager_mode" not in settings, name


def test_a_spec_that_names_classic_options_reaches_the_TRIAL_config():
    """THE RECORDED CHAIN, end to end: spec -> _expert_run_settings -> the run's experts
    block -> _build_daily_trial_config -> the per-trial expert settings the engine feeds to
    the expert. The middle step is a WHITELIST that rebuilds the trial config key by key, so
    "the launcher wrote it" is not evidence that a trial ever sees it.

    MUTATION KILL: drop the two-line plumbing in _expert_run_settings and the key is gone
    from the trial config."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    m = _launcher()
    spec = {**m._EXPERT_OPT["FMPRating"], "risk_manager_mode": "classic_options"}
    settings = m._expert_run_settings(spec, ["AAPL"])
    assert settings["risk_manager_mode"] == "classic_options"

    backtest_cfg = {
        "backtest_id": "m5-chain",
        "start_date": "2024-01-02",
        "end_date": "2024-03-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPRating", "settings": settings}],
        "initial_capital": 100_000.0,
        "account_settings": {},
        "warmup_days": 0,
        "seed": 1,
    }
    trial = _build_daily_trial_config(backtest_cfg, {}, None)
    assert trial["experts"][0]["settings"]["risk_manager_mode"] == "classic_options"


def test_the_mode_the_grid_can_select_is_the_one_the_gate_engages_on():
    """One spelling, checked against the gate rather than against a second copy of the
    string: the H2 dispatch engages on EXACTLY this value and treats anything else as
    legacy, so a typo'd spec would search a mode that does nothing."""
    from ba2_common.core.OptionRiskManagement import (
        RISK_MANAGER_MODE_CLASSIC_OPTIONS, option_risk_manager_enabled,
    )

    m = _launcher()
    spec = {**m._EXPERT_OPT["FMPRating"],
            "risk_manager_mode": RISK_MANAGER_MODE_CLASSIC_OPTIONS}
    settings = m._expert_run_settings(spec, ["AAPL"])
    assert option_risk_manager_enabled(settings) is True


# ==============================================================================================
# GRID 2 (plan 2026-08-31 Task 10, design 2026-08-31 sections 2/5/7)
# ==============================================================================================
#
# The gene tables above are grid 1's. Grid 2's keys are pinned SEPARATELY rather than folded
# into ``PURE``, and deliberately: the two grids answer different questions with different
# structures, and a shared budget assertion over both would either be loose enough to be
# useless for one of them or would fail the moment either grid legitimately grows.

GRID2 = ["O_LEAPC", "O_LEAPP", "O_ERN", "O_CBS", "O_PBS"]


# ---- amendment 1: the method is FIXED, and every searched strike band is a DELTA ------------
@pytest.mark.parametrize("key", GRID2)
def test_no_grid2_key_searches_the_strike_METHOD(key):
    """THE DEAD-GENE TRAP THIS AMENDMENT EXISTS FOR.

    ``option_strike_method`` is a CATEGORICAL gene sharing ONE ``option_strike_param`` domain
    with the percent-OTM alternative. Every grid-2 thesis is stated in delta -- a 0.80-delta
    stock replacement, a 0.40/0.20 backspread -- so there is no single domain that is correct
    under both methods: a genome that picked ``percent_otm`` would read 0.80 as 0.8% OTM
    (at the money on a key whose entire point is deep ITM), and a genome that picked ``delta``
    against a percent domain would refuse outright. Emitting the gene at all is the defect.
    """
    genes = _space(_launcher(), key)
    offenders = [g for g in genes if g.endswith(":option_strike_method")]
    assert not offenders, f"{key} emits a strike-METHOD gene: {offenders}"


@pytest.mark.parametrize("key", ["O_LEAPC", "O_LEAPP", "O_CBS", "O_PBS"])
def test_every_fixed_method_keys_strike_band_is_a_delta_in_the_unit_interval(key):
    """The other half of amendment 1: the bands the fixed-delta keys DO search are deltas.

    A delta lives in (0,1) by definition, and a percent-OTM band (0-8, 2-12, ...) does not --
    so this single arithmetic check catches a percent band that has been given to a
    delta-method row, whatever it is named.
    """
    m = _launcher()
    genes = _space(m, key)
    bands = {g: v for g, v in genes.items() if ":option_strike_delta" in g}
    assert bands, f"{key} searches no strike delta at all"
    for name, spec in bands.items():
        assert 0.0 < spec["min"] < spec["max"] < 1.0, (
            f"{key}'s {name} band {spec['min']}..{spec['max']} is not a delta in (0,1)")
    assert m._OPTION_STRATS[key]["option_strike_method"] == "delta"


def test_o_ern_is_the_documented_exception_and_says_why():
    """O_ERN's two builders CANNOT read a strike method, so its width gene is a percent.

    Design section 2 asks for "strangle width delta 0.25-0.45". ``open_straddle`` and
    ``open_strangle`` are two of the eight builders that hard-code ``percent_otm`` at every
    selection site, so a delta band handed to them would be READ AS A PERCENT -- 0.25% OTM,
    effectively at the money, on both legs. That is the OPT-S2 trap, and the honest response
    is to search the unit the builder reads. Pinned here, against the registry rather than
    against a comment, so the day either builder learns the method this test fails and the
    row can be converted.
    """
    from ba2_common.core.types import honours_strike_method

    m = _launcher()
    for choice in m._OPTION_STRATS["O_ERN"]["option_structure_choices"]:
        assert not honours_strike_method(choice), (
            f"{choice} now honours strike_method -- O_ERN's width gene can and should "
            f"become the design's delta band (0.25-0.45); see its _OPTION_STRATS row")
    assert m._OPTION_STRATS["O_ERN"]["option_strike_method"] == "percent_otm"
    assert "O_ERN" not in m._FIXED_DELTA_METHOD_STRATEGIES


def test_the_fixed_method_guard_survives_a_row_that_also_searches_the_percent_param():
    """The guard is on what the row DECLARES, not on how it happens to be written.

    Today's grid-2 rows also decline ``option_strike_param_optimize``, which the older guard
    already catches -- so without this the amendment would be enforced by an accident of
    authoring, and adding a percent gene to a grid-2 row later would silently re-arm the
    method gene.
    """
    m = _launcher()
    cfg = dict(m._OPTION_STRATS["O_LEAPC"])
    cfg["option_strike_param_optimize"] = True
    out = m._apply_option_strike_method_gene(cfg)
    assert "option_strike_method_optimize" not in out
    assert "option_strike_method_choices" not in out


# ---- dead-gene guards: one per gene FAMILY, genome -> decode -> a DIFFERENT structure ------
def _decoded_entry_action(m, key, genome, expert="FMPRating"):
    """The option ENTRY action as a trial actually receives it: through the REAL
    ``_build_daily_trial_config``, not through ``decode_params`` alone.

    That is the whole point of the guard. ``_build_daily_trial_config`` rebuilds the trial
    config KEY BY KEY (it says so itself), so a gene can decode perfectly and still never
    reach the engine -- the whitelist trap. Reading the action back off the trial config's
    ``entry_rules`` proves the value survived the whole journey.

    THE EMISSION IS CHECKED TOO, and it has to be: ``decode_params`` reads a FLAT dict and
    never consults the param space, so a genome key can decode and land on the action while
    ``collect_param_space`` emits no gene for it at all -- the GA would then never produce
    that key and the whole chain below would be exercising a value only this test can set.
    Emitted AND reaches the action is the guard; either half alone is not.
    """
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import collect_param_space, decode_params

    strat = m._build_strategy(key, f"g2-{key}", expert)
    space = collect_param_space(strat)
    missing = [g for g in genome if g not in space]
    assert not missing, (
        f"{key} does not EMIT {missing} as gene(s), so the GA can never set them; the "
        f"decode below would be testing a value nothing in a real run can produce")
    decoded = decode_params(strat, genome)
    backtest_cfg = {
        "backtest_id": f"grid2-{key}",
        "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet",
    }
    trial = _build_daily_trial_config(backtest_cfg, decoded, None)
    return trial["entry_rules"][0]["actions"][0]


def test_the_leaps_delta_gene_moves_the_strike_target():
    """Gene family: ``option_strike_delta`` (single-leg). Two levels of the SAME gene must
    reach the action as two different selection targets."""
    m = _launcher()
    lo = _decoded_entry_action(m, "O_LEAPC",
                               {"entry:o_leapc-entry:a0:option_strike_delta": 0.70})
    hi = _decoded_entry_action(m, "O_LEAPC",
                               {"entry:o_leapc-entry:a0:option_strike_delta": 0.90})
    assert lo["option_strike_method"] == hi["option_strike_method"] == "delta"
    assert lo["option_strike_param"] == 0.70
    assert hi["option_strike_param"] == 0.90


def test_the_backspread_leg_genes_move_the_two_legs_INDEPENDENTLY():
    """Gene family: the per-leg delta PAIR.

    The failure this catches is not "the gene is dropped" but "the two genes collapse onto
    one target" -- which would leave the selector picking the two nearest strikes to a single
    delta, a materially different structure from the 0.40/0.20 backspread design section 2
    searches, with both genes still moving something.
    """
    m = _launcher()
    for key, prefix in (("O_CBS", "o_cbs"), ("O_PBS", "o_pbs")):
        a = _decoded_entry_action(m, key, {
            f"entry:{prefix}-entry:a0:option_strike_delta": 0.35,
            f"entry:{prefix}-entry:a0:option_strike_delta_long": 0.15})
        b = _decoded_entry_action(m, key, {
            f"entry:{prefix}-entry:a0:option_strike_delta": 0.50,
            f"entry:{prefix}-entry:a0:option_strike_delta_long": 0.30})
        # [long, short] -- the order ``TradeActions._spread_params`` destructures.
        assert a["option_strike_param"] == [0.15, 0.35], a["option_strike_param"]
        assert b["option_strike_param"] == [0.30, 0.50], b["option_strike_param"]
        # And moving ONE gene moves ONE leg.
        mixed = _decoded_entry_action(m, key, {
            f"entry:{prefix}-entry:a0:option_strike_delta": 0.35,
            f"entry:{prefix}-entry:a0:option_strike_delta_long": 0.30})
        assert mixed["option_strike_param"] == [0.30, 0.35]


def test_the_structure_gene_submits_a_DIFFERENT_builder():
    """Gene family: ``option_structure``. Not a parameter -- the action TYPE itself, i.e.
    which builder the engine constructs."""
    m = _launcher()
    a = _decoded_entry_action(m, "O_ERN",
                              {"entry:o_ern-entry:a0:option_structure": "open_straddle"})
    b = _decoded_entry_action(m, "O_ERN",
                              {"entry:o_ern-entry:a0:option_structure": "open_strangle"})
    assert a["action_type"] == "open_straddle"
    assert b["action_type"] == "open_strangle"
    assert a["action_type"] != b["action_type"]


def test_the_grid2_dte_gene_moves_the_selection_window():
    """Gene family: ``option_dte``. Decoded as a window CENTRE, so the pin is on the WINDOW
    the action ends up carrying -- the two numbers the selector filters the chain by."""
    m = _launcher()
    lo = _decoded_entry_action(m, "O_LEAPC", {"entry:o_leapc-entry:a0:option_dte": 410})
    hi = _decoded_entry_action(m, "O_LEAPC", {"entry:o_leapc-entry:a0:option_dte": 500})
    assert (lo["option_dte_min"], lo["option_dte_max"]) == (365, 455)
    assert (hi["option_dte_min"], hi["option_dte_max"]) == (455, 545)


@pytest.mark.parametrize("key,prefix", [("O_LEAPC", "o_leapc"), ("O_ERN", "o_ern"),
                                        ("O_CBS", "o_cbs")])
def test_the_sizing_gene_reaches_the_action(key, prefix):
    """Gene family: ``option_sizing`` -- the one gene that gates the fitness arithmetic
    (contracts x max_loss IS sizing% of equity by construction)."""
    m = _launcher()
    a = _decoded_entry_action(m, key, {f"entry:{prefix}-entry:a0:option_sizing": 1.0})
    b = _decoded_entry_action(m, key, {f"entry:{prefix}-entry:a0:option_sizing": 10.0})
    assert a["option_sizing"] == 1.0 and b["option_sizing"] == 10.0


def test_the_event_timing_genes_reach_the_seeded_rules():
    """Gene family: the two O_ERN timing thresholds, which live on CONDITIONS rather than on
    the action -- a different half of the decode, and the half the whitelist could drop
    wholesale (``entry_rules``/``exit_rules``)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    m = _launcher()
    strat = m._build_strategy("O_ERN", "g2-ern", "FMPRating")
    decoded = decode_params(strat, {"cond:o_ern-days_to_earnings:value": 5,
                                    "cond:days_after:value": 2})
    trial = _build_daily_trial_config(
        {"backtest_id": "ern", "start_date": "2024-02-01", "end_date": "2024-06-01",
         "enabled_instruments": ["AAPL"],
         "experts": [{"class": "FMPRating", "settings": {}}], "initial_capital": 20_000.0,
         "account_settings": {}, "warmup_days": 0, "seed": 1,
         "entry_action": getattr(strat, "entry_action", None), "options_store": "parquet"},
        decoded, None)

    entry_leaf = next(c for c in trial["entry_rules"][0]["conditions"]["conditions"]
                      if c["field"] == "rec_days_to_earnings")
    assert entry_leaf["value"] == 5 and entry_leaf["op"] == "<="
    exit_rule = next(r for r in trial["exit_rules"] if r["id"] == "opt_event")
    exit_leaf = exit_rule["conditions"]["conditions"][0]
    assert exit_leaf["field"] == "days_after_event"
    assert exit_leaf["value"] == 2 and exit_leaf["op"] == ">="


def test_the_entry_field_is_the_STAMPED_one_not_the_calendar_fetching_one():
    """Amendment 3. ``days_to_earnings`` is the legacy condition that goes and FETCHES a
    calendar; ``rec_days_to_earnings`` reads the number the recommendation was stamped with.
    Using the former would re-fetch per symbol per bar AND could disagree with the ranking
    the expert computed at that same bar."""
    m = _launcher()
    leaves = m._option_entry_rule("O_ERN")["conditions"]["conditions"]
    fields = [c["field"] for c in leaves]
    assert "rec_days_to_earnings" in fields
    assert "days_to_earnings" not in fields


def test_only_the_event_key_carries_the_timing_gates():
    """The gates are O_ERN's, not the grid's: on any other key the stamp is absent, the leaf
    could never fire, and the strategy would trade nothing while carrying two live genes."""
    m = _launcher()
    for key in [k for k in GRID2 if k != "O_ERN"]:
        fields = [c["field"] for c in m._option_entry_rule(key)["conditions"]["conditions"]]
        assert "rec_days_to_earnings" not in fields, f"{key} carries an event entry gate"
        assert not any(r["id"] == "opt_event" for r in m._option_exit_rules(key)), (
            f"{key} carries the event exit rule")


# ---- amendment 2: the expiry must land AFTER the print -------------------------------------
def test_no_o_ern_genome_can_decode_to_an_expiry_before_the_print():
    """Exhaustive over the DTE gene's OWN lattice, not a sampled genome.

    The band has 4 levels; walking all of them and re-deriving the decoded window is a
    stronger statement than any single decode, and it is the statement the constraint needs:
    NO genome, not "not this one".
    """
    from app.services.strategy_param_space import _apply_option_dte

    m = _launcher()
    cfg = m._OPTION_STRATS["O_ERN"]
    ceiling = m._EVENT_ENTRY_DAYS["value_max"]
    lo, hi, step = (cfg["option_dte_min_range"], cfg["option_dte_max_range"],
                    cfg["option_dte_step"])
    centres = list(range(lo, hi + 1, step))
    assert len(centres) == 4, f"expected 4 searched DTE levels, got {centres}"
    for centre in centres:
        action = dict(cfg)
        _apply_option_dte(action, centre)
        assert action["option_dte_min"] > ceiling, (
            f"DTE centre {centre} decodes to dte_min={action['option_dte_min']}, which is "
            f"not strictly past the {ceiling}-day entry ceiling: the straddle could expire "
            f"BEFORE the print it is a bet on")
        assert 7 <= action["option_dte_min"] and action["option_dte_max"] <= 30, (
            f"DTE centre {centre} decodes to {action['option_dte_min']}.."
            f"{action['option_dte_max']}, outside design section 2's 7-30 band")


def test_the_expiry_constraint_is_checked_at_IMPORT_not_left_implicit():
    """The guard itself, driven by a violating table. Import-time, because at decode time a
    violating genome is one refused individual in a population of 40 -- and 39 scored ones
    that quietly searched a nonsense region."""
    m = _launcher()
    bad = dict(m._OPTION_STRATS["O_ERN"])
    bad["option_dte_min_range"] = 11   # 11 - 7 = 4, inside the 5-day entry ceiling
    original = m._OPTION_STRATS["O_ERN"]
    m._OPTION_STRATS["O_ERN"] = bad
    try:
        with pytest.raises(RuntimeError, match="entry ceiling"):
            m._assert_option_expiry_clears_event_window("O_ERN")
    finally:
        m._OPTION_STRATS["O_ERN"] = original


# ---- amendment 5: the debit/credit partition stays TOTAL ------------------------------------
@pytest.mark.parametrize("key", GRID2)
def test_every_grid2_key_joined_the_DEBIT_half_by_its_iv_rank_thesis(key):
    """All five are long premium, so all five want "buy vol only when it is cheap". A key
    landing on the credit side would get the OPPOSITE gate, and the GA never searches an
    operator, so nothing downstream could recover from it."""
    m = _launcher()
    assert key in m._DEBIT_OPTION_MEMBERS
    assert key not in m._CREDIT_OPTION_MEMBERS


def test_the_partition_is_still_total_over_every_member():
    """The assertion the launcher makes at import, restated as a test so the SET is checked
    and not merely the fact that import succeeded."""
    m = _launcher()
    assert m._DEBIT_OPTION_MEMBERS | m._CREDIT_OPTION_MEMBERS == set(m._OPTION_STRATS)
    assert not (m._DEBIT_OPTION_MEMBERS & m._CREDIT_OPTION_MEMBERS)


@pytest.mark.parametrize("key", GRID2)
def test_every_grid2_key_gets_the_debit_halfs_selection_weights(key):
    """The partition's other consumer: the shared ``optsel:<half>:<w>`` genes. A member with
    no half raises rather than defaulting, so this also pins that the new keys resolve."""
    genes = _space(_launcher(), key)
    for w in ("w_premium", "w_iv", "w_rvol"):
        assert f"optsel:debit:{w}" in genes, f"{key} is missing optsel:debit:{w}"
        assert f"optsel:credit:{w}" not in genes


# ---- gene BUDGET, hand-derived ---------------------------------------------------------------
def test_the_grid2_genome_sizes_are_exactly_what_the_tables_add_up_to():
    """HAND-DERIVED, per key, so an accidental extra gene is caught by arithmetic rather than
    by a ceiling nobody re-checks.

    Shared by EVERY grid-2 single (they all build through ``_option_entry_rule`` +
    ``_option_exit_rules``):

      entry conditions   signal:enabled, shared-gate_confidence:{value,enabled},
                         <k>-iv_rank:{value,enabled}, shared-rel_volume:{value,enabled},
                         <k>-iv_rv:{value,enabled}, <k>-exp_profit:{value,enabled}   = 11
                         (``-flat`` is a correctness guard, no gene)
      entry action       option_dte, option_entry_cross, option_sizing               =  3
      exits              opt_tp:{enabled,cond tp}, opt_time:{enabled,cond td},
                         opt_dte:{enabled,cond dte}, opt_sl_ml:{enabled,cond sl_ml}  =  8
      selection weights  optsel:debit:{w_premium,w_iv,w_rvol}                        =  3
                                                                            SHARED  = 25

    Per key on top of that:
      O_LEAPC / O_LEAPP  option_strike_delta                                = 1  -> 26
      O_ERN              option_strike_param, option_structure,
                         cond o_ern-days_to_earnings, cond days_after       = 4  -> 29
                         (the event exit has NO enabled gene -- not toggleable)
      O_CBS / O_PBS      option_strike_delta, option_strike_delta_long,
                         opt_tp_mult:{enabled, cond tp_mult}                = 4  -> 29
    """
    m = _launcher()
    expected = {"O_LEAPC": 26, "O_LEAPP": 26, "O_ERN": 29, "O_CBS": 29, "O_PBS": 29}
    for key, n in expected.items():
        got = len(_space(m, key))
        assert got == n, f"{key} genome is {got} genes, the table adds up to {n}"


def test_grid1_genomes_did_not_move():
    """The budget pin above is worthless if grid 2 grew grid 1 on the way past. 31 is the
    ceiling ``test_the_swap_shrinks_every_structure_genome`` already derives; this restates
    it AFTER the grid-2 tables exist so a shared-table edit that widened an existing key
    cannot hide behind the new keys' own pins."""
    m = _launcher()
    for key in PURE:
        assert len(_space(m, key)) <= 31, f"{key} grew past the grid-1 budget"


# ---- the lower TRADE FLOOR: long-dated keys only, never O_ERN --------------------------------
@pytest.mark.parametrize("key", ["O_LEAPC", "O_LEAPP"])
def test_the_long_dated_keys_get_the_lower_trade_floor(key):
    """THE LONG-DATED FAMILY IS EXACTLY O_LEAPC/O_LEAPP here (design section 2's third
    member, O_PMCC, is phase-gated and has no row yet). Plan Task 10's wording is "a CONFIG
    naming the long-dated keys only"; the backspreads are the separate convexity-financed
    family at 60-180 DTE and are pinned in the untouched set below."""
    m = _launcher()
    block = {}
    m._apply_option_trade_floor(key, block)
    assert block["car_hard_min_trades_per_year"] == 3.0
    assert block["car_min_trades_per_year"] == 8.0


def test_the_exempt_set_is_exactly_the_long_dated_family():
    """The set itself, so a key cannot be added to it without this test saying so."""
    m = _launcher()
    assert m._OPTION_LOW_TRADE_FLOOR_STRATEGIES == {"O_LEAPC", "O_LEAPP"}


def test_the_event_key_NEVER_gets_the_lower_trade_floor():
    """THE NEGATIVE PIN (plan Task 10, amendment 7, and design section 6's own sentence:
    "O_ERN keeps the normal floor -- earnings events are frequent").

    Earnings are quarterly PER NAME and design section 8 calls O_ERN "the one key whose
    result deserves statistical weight" precisely because it gets hundreds of independent
    events in-window. A lower floor there removes the only breadth check the fitness applies
    to the one key that can actually satisfy it -- and lets a three-trades-a-year O_ERN
    genome, which is the clearest possible sign the entry gates are mis-tuned, score as a
    normal config.
    """
    m = _launcher()
    block = {}
    m._apply_option_trade_floor("O_ERN", block)
    assert block == {}, f"O_ERN must keep the platform trade floor, got {block}"
    assert "O_ERN" not in m._OPTION_LOW_TRADE_FLOOR_STRATEGIES


@pytest.mark.parametrize("key", ["S2", "O_LC", "O_IC", "OS1", "O_CBS", "O_PBS", None])
def test_no_other_strategy_is_touched_by_the_trade_floor(key):
    """Every existing job's fitness must be bit-identical: an absent key leaves the
    expert/platform resolution exactly as it was.

    O_CBS/O_PBS ARE IN THIS LIST, not the exempt one (corrected 2026-09-02). They are the
    convexity-financed family, not the long-dated one: a 60-180 DTE entry exited at a 20-45
    DTE floor lives 15-160 days, i.e. 2.3-24 structures per underlying per year, so the
    platform's 12/yr floor is reachable and disqualifies only a genuinely thin config."""
    m = _launcher()
    block = {}
    m._apply_option_trade_floor(key, block)
    assert block == {}


def test_the_trade_floor_survives_the_trial_config_WHITELIST():
    """THE WHITELIST TRAP. ``_build_daily_trial_config`` rebuilds the trial config key by key,
    so a floor that is parsed, stored and echoed by the launcher is still SILENTLY DEAD if it
    is not listed there -- every long-dated genome disqualified by a floor the run had
    explicitly lowered, with nothing in any log to say so."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    m = _launcher()
    backtest_cfg = {
        "backtest_id": "floor", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPRating", "settings": {}}], "initial_capital": 20_000.0,
        "account_settings": {}, "warmup_days": 0, "seed": 1, "options_store": "parquet",
    }
    m._apply_option_trade_floor("O_LEAPC", backtest_cfg)
    trial = _build_daily_trial_config(backtest_cfg, {}, None)
    assert trial["car_hard_min_trades_per_year"] == 3.0
    assert trial["car_min_trades_per_year"] == 8.0


def test_an_explicit_run_level_floor_beats_the_expert_scan():
    """And the resolver honours it. ``_car_trade_thresholds_for_experts`` takes the TIGHTEST
    across experts, which would silently discard exactly the lowering this feature performs;
    a run-level value is a decision and wins."""
    from app.services.backtest.daily_backtest_handler import _car_trade_thresholds_for_experts

    m = _launcher()
    cfg = {"experts": [{"class": "DeterministicScorer", "settings": {}}]}
    m._apply_option_trade_floor("O_LEAPC", cfg)
    out = _car_trade_thresholds_for_experts(cfg)
    assert out["car_hard_min_trades_per_year"] == 3.0
    assert out["car_min_trades_per_year"] == 8.0
    # ... and with nothing stated, the expert scan is untouched.
    assert "car_hard_min_trades_per_year" not in _car_trade_thresholds_for_experts(
        {"experts": [{"class": "FMPRating", "settings": {}}]})


# ---- amendment 4: the earnings expert is REGISTERED, and the warmup numbers agree -----------
def test_the_earnings_expert_is_a_supported_backtest_expert():
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS
    assert _SUPPORTED_EXPERTS["FMPEarningsEvent"] == "ba2_experts.FMPEarningsEvent"


def test_the_earnings_expert_warmup_table_matches_the_class():
    """EQUALITY, not "both are set". ``derive_warmup_days`` prefers the CLASS attribute and
    falls back to the table when the import fails -- so a disagreement is a silently
    different warmup on exactly the runs where something is already wrong."""
    from ba2_experts.FMPEarningsEvent import FMPEarningsEvent
    from app.services.backtest.daily_backtest_handler import _EXPERT_WARMUP_BARS
    assert _EXPERT_WARMUP_BARS["FMPEarningsEvent"] == FMPEarningsEvent.BACKTEST_WARMUP_BARS
    assert FMPEarningsEvent.BACKTEST_WARMUP_BARS == 620


def test_the_earnings_expert_can_actually_be_optimized():
    """Registration is not enough: ``_cmd_optimize`` exits before building anything when the
    expert has no ``_EXPERT_OPT`` spec, so an O_ERN job would be dead on arrival."""
    m = _launcher()
    assert "FMPEarningsEvent" in m._EXPERT_OPT
    settings = m._expert_run_settings(m._EXPERT_OPT["FMPEarningsEvent"], ["AAPL"])
    assert settings  # a real settings dict, not an empty one


def test_the_earnings_expert_warmup_reaches_a_run():
    from app.services.backtest.daily_backtest_handler import derive_warmup_days
    assert derive_warmup_days(["FMPEarningsEvent"]) >= 620


# ---- phase-gated keys refuse LOUDLY ----------------------------------------------------------
@pytest.mark.parametrize("bad", ["O_PMCC", "O_CAL"])
def test_a_phase_gated_key_refuses_with_the_plan_reference(bad):
    """The naked-exclusion discipline, applied to a different reason: never silently run,
    never silently skip, and say where the decision is written down."""
    m = _launcher()
    with pytest.raises(SystemExit) as e:
        m._refuse_phase_gated_strategy("optimize", [bad])
    msg = str(e.value)
    assert bad in msg and "PHASE-GATED" in msg
    assert "2026-08-31-options-grid2-convex-earnings-impl.md" in msg
    assert "2026-08-31-leaps-grid-design.md" in msg


@pytest.mark.parametrize("bad", ["O_PMCC", "O_CAL"])
def test_a_phase_gated_key_is_a_KNOWN_key_that_refuses(bad):
    """Registered in ``_STRATEGY_BUILDERS`` so argparse accepts it and the operator reads the
    REASON -- rather than "invalid choice", which says nothing about why."""
    m = _launcher()
    assert bad in m._STRATEGY_BUILDERS
    with pytest.raises(SystemExit):
        m._STRATEGY_BUILDERS[bad](bad)


@pytest.mark.parametrize("bad", ["O_PMCC", "O_CAL"])
def test_the_phase_gated_keys_have_no_gene_table(bad):
    """A row would be searchable by any path that reads the table directly."""
    m = _launcher()
    assert bad not in m._OPTION_STRATS
    assert bad not in m._GRID2_OPTION_STRATEGIES


# ---- the emitted exit ruleset ------------------------------------------------------------------
@pytest.mark.parametrize("key", ["O_ERN", "O_CBS", "O_PBS"])
def test_opt_sl_ml_is_authored_OFF_but_still_searched(key):
    """Design section 2, twice: "opt_sl_ml searchable, default OFF -- the thesis is binary; a
    stop mid-event amputates it". Both halves matter -- authored off is what the emitted
    ruleset, a seeded deploy and the persisted top-N read; still searched is what lets the
    GA disagree."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules(key) if r["id"] == "opt_sl_ml")
    assert rule["enabled"] is False
    assert rule["toggle_optimize"] is True
    assert "exit:opt_sl_ml:enabled" in _space(m, key)


@pytest.mark.parametrize("key", ["O_LEAPC", "O_LEAPP"])
def test_the_long_dated_keys_keep_opt_sl_ml_ON(key):
    """The negative: design section 2 lists ``opt_sl_ml`` as a plain gene for the LEAPS arms,
    not a default-off one -- a 400-day debit position is not a binary event bet."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules(key) if r["id"] == "opt_sl_ml")
    assert "enabled" not in rule
    assert key not in m._OPTION_SL_ML_AUTHORED_OFF


@pytest.mark.parametrize("key", ["O_CBS", "O_PBS"])
def test_the_backspreads_take_profit_on_a_MULTIPLE_of_the_premium(key):
    """Design section 2: "take-profit multiple 2x-6x | to expiry". The "| to expiry" arm IS
    the rule's own on/off gene, not a sentinel value -- pinned so a later reader does not
    add a magic number for it."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules(key) if r["id"] == "opt_tp_mult")
    leaf = rule["conditions"]["conditions"][0]
    assert leaf["field"] == "profit_multiple_of_premium"
    assert leaf["op"] == ">="
    assert (leaf["value_min"], leaf["value_max"], leaf["value_step"]) == (2, 6, 1)
    assert rule["toggle_optimize"] is True


def test_no_grid1_key_grew_a_take_profit_multiple():
    m = _launcher()
    for key in PURE:
        assert not any(r["id"] == "opt_tp_mult" for r in m._option_exit_rules(key))


def test_grid1_keeps_the_original_dte_exit_band():
    """The per-key DTE band table must not have moved anyone else: 0..21 step 3 is what every
    grid-1 key had, and changing it would re-score every completed option job."""
    m = _launcher()
    for key in PURE + ["OS1", "OS2", "OS3", "OS4"]:
        leaf = next(r for r in m._option_exit_rules(key)
                    if r["id"] == "opt_dte")["conditions"]["conditions"][0]
        assert (leaf["value"], leaf["value_min"], leaf["value_max"], leaf["value_step"]) \
            == (21, 0, 21, 3), f"{key}'s opt_dte band moved"


def test_the_event_exit_is_not_toggleable():
    """Every other exit carries an on/off gene; this one does not, and that is the design's
    own split -- with it off the strategy is not O_ERN with a gate disabled, it is an
    unmanaged straddle waiting for opt_dte."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules("O_ERN") if r["id"] == "opt_event")
    assert "toggle_optimize" not in rule
    assert "exit:opt_event:enabled" not in _space(m, "O_ERN")


# ---- fitness + holdout routing ---------------------------------------------------------------
@pytest.mark.parametrize("key", GRID2)
def test_every_grid2_key_is_scored_and_railed_as_a_PURE_option_kind(key):
    """Two consumers of ``_PURE_OPTION_STRATEGIES``: the fitness default (option_car, design
    section 7) and the walk-forward holdout rail (2026 must stay unspent)."""
    m = _launcher()
    assert key in m._PURE_OPTION_STRATEGIES
    assert m._resolve_fitness(None, key, "sharpe_ratio") == "option_consistent_annual_return"
    with pytest.raises(SystemExit):
        m._assert_option_window_excludes_holdout([key], "2026-06-30")


@pytest.mark.parametrize("key", GRID2)
def test_no_grid2_key_joined_a_SEARCHED_group(key):
    """Design section 7: singles only in round one, so every result is attributable to ONE
    structure."""
    m = _launcher()
    for members in m._OPTION_GROUPS_ALL.values():
        assert key not in members
