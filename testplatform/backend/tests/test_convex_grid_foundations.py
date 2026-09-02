"""``O_CONVEX`` -- the convex-harvest grid's single launchable key (plan Task 13, design
docs/superpowers/specs/2026-08-31-convex-harvest-grid-design.md §2), operator decision
2026-09-02.

O_CONVEX is a GROUP key over two members, the exact OS1-OS4 mechanism
(``_build_strategy_option_group``): "O_CONVEXC" (bullish signal -> buy_call, the "buy a lot of
long calls" thesis) and "O_CONVEXP" (bearish signal -> buy_put, the tail-hedge twin), each its
own toggleable entry TradeRule sharing ONE exit ruleset. There is no ``kind``/``option_structure``
categorical gene -- the option type comes from the SAME directional-signal machinery
O_LEAPC/O_LEAPP already use as two separate keys, merged here via the group builder. No "both"
(simultaneous call+put) arm is offered, and none needs to be carried forward: ``has_no_position``
is EXPERT-level per INSTRUMENT, so once either arm opens a ticket the other is blocked from
opening a second one on the same underlying until the first closes.

Mirrors ``test_option_grid_foundations.py``'s grid-2/group sections and
``test_earnings_event_expert_genes.py``'s recorded-chain style: every gene the launcher's two
rows emit is proven end-to-end (genome -> ``collect_param_space`` -> ``decode_params`` ->
``_build_daily_trial_config`` -> the value where the engine actually reads it). The two other
Task-13 pins live here too: fitness ROUTING (``_refuse_convex_fitness_mismatch``) and the
debit/credit partition.
"""
import importlib.util
import os
import sys

import pytest

# tests/ -> backend/ -> testplatform/, then the launcher beside backend/. Resolved off
# __file__ (see test_option_grid_foundations.py's own comment on why -- CWD-relative import
# resolution differs between a repo-root run and a testplatform/backend run).
_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_convex", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_convex"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _space(m, key="O_CONVEX", expert="FMPRating"):
    from app.services.strategy_param_space import collect_param_space
    return collect_param_space(m._build_strategy(key, f"g-{key}", expert))


def _built(m, expert="FMPRating"):
    return m._build_strategy("O_CONVEX", "g-convex", expert)


def _decoded(m, genome, expert="FMPRating"):
    """The O_CONVEX trial config as a real run actually receives it -- through
    ``_build_daily_trial_config``, exactly like test_option_grid_foundations.py's
    ``_decoded_entry_action`` (the whitelist-trap guard: emitted AND reaches the engine)."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import collect_param_space, decode_params

    strat = _built(m, expert)
    space = collect_param_space(strat)
    missing = [g for g in genome if g not in space]
    assert not missing, (
        f"O_CONVEX does not EMIT {missing} as gene(s), so the GA can never set them; the "
        f"decode below would be testing a value nothing in a real run can produce")
    decoded = decode_params(strat, genome)
    backtest_cfg = {
        "backtest_id": "convex", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet",
    }
    return _build_daily_trial_config(backtest_cfg, decoded, None)


def _entry_rule(trial, rid):
    return next(r for r in trial["entry_rules"] if r["id"] == rid)


# ---- the group exists and is wired -----------------------------------------------------------
def test_o_convex_is_a_pure_option_group_strategy():
    m = _launcher()
    assert "O_CONVEX" in m._OPTION_GROUPS
    assert m._OPTION_GROUPS["O_CONVEX"] == ["O_CONVEXC", "O_CONVEXP"]
    assert "O_CONVEX" in m._PURE_OPTION_STRATEGIES
    assert "O_CONVEX" in m._CONVEX_OPTION_STRATEGIES
    assert "O_CONVEX" in m._STRATEGY_BUILDERS
    assert m._STRATEGY_BUILDERS["O_CONVEX"] is m._build_strategy_option_group


def test_the_members_are_not_independently_launchable():
    """No new single-key surface: only "O_CONVEX" is a real CLI choice."""
    m = _launcher()
    assert "O_CONVEXC" not in m._STRATEGY_BUILDERS
    assert "O_CONVEXP" not in m._STRATEGY_BUILDERS


def test_o_convex_is_absent_from_grid_2s_own_key_set():
    """Design §8: a separate grid, never a matrix row alongside O_LEAPC/O_ERN/etc."""
    m = _launcher()
    assert "O_CONVEX" not in m._GRID2_OPTION_STRATEGIES


def test_the_built_strategy_carries_exactly_two_entry_rules():
    m = _launcher()
    s = _built(m)
    ids = [r["id"] for r in s.entry_rules]
    assert ids == ["o_convexc-entry", "o_convexp-entry"]


def test_both_entry_rules_share_the_same_exit_ruleset():
    m = _launcher()
    s = _built(m)
    exit_ids = {r["id"] for r in s.exit_rules}
    assert exit_ids == {"opt_tp", "opt_time", "opt_dte", "opt_tp_mult", "opt_sl_ml"}


# ---- directional gates: call=bullish, put=bearish, from the EXISTING signal machinery -------
def test_the_call_arm_gates_on_bullish_the_put_arm_on_bearish():
    m = _launcher()
    assert m._OPTION_ENTRY_GATE["O_CONVEXC"] == "bullish"
    assert m._OPTION_ENTRY_GATE["O_CONVEXP"] == "bearish"
    s = _built(m)
    call_rule = next(r for r in s.entry_rules if r["id"] == "o_convexc-entry")
    put_rule = next(r for r in s.entry_rules if r["id"] == "o_convexp-entry")
    call_signal = next(c for c in call_rule["conditions"]["conditions"]
                       if c["field"] in ("bullish", "bearish"))
    put_signal = next(c for c in put_rule["conditions"]["conditions"]
                      if c["field"] in ("bullish", "bearish"))
    assert call_signal["field"] == "bullish"
    assert put_signal["field"] == "bearish"


def test_the_call_arm_submits_buy_call_the_put_arm_submits_buy_put():
    m = _launcher()
    s = _built(m)
    call_rule = next(r for r in s.entry_rules if r["id"] == "o_convexc-entry")
    put_rule = next(r for r in s.entry_rules if r["id"] == "o_convexp-entry")
    assert call_rule["actions"][0]["action_type"] == "buy_call"
    assert put_rule["actions"][0]["action_type"] == "buy_put"


# ---- NO kind/option_structure gene: the operator-mandated absence ---------------------------
def test_no_option_structure_gene_is_emitted_at_all():
    """Operator decision 2026-09-02: the option type comes from the directional-signal
    machinery (two entry rules), not a categorical ``option_structure`` toggle."""
    m = _launcher()
    genes = _space(m)
    offenders = [g for g in genes if g.endswith(":option_structure")]
    assert not offenders, f"O_CONVEX emits an option_structure gene: {offenders}"
    for member in ("O_CONVEXC", "O_CONVEXP"):
        assert "option_structure_optimize" not in m._OPTION_STRATS[member]
        assert "option_structure_choices" not in m._OPTION_STRATS[member]


# ---- the put arm's on/off toggle is the STANDARD group-member gene --------------------------
def test_both_arms_get_the_standard_per_rule_enabled_gene():
    """"the put rule gets that standard gene" -- and, for free from the SAME group mechanism,
    so does the call rule: no new toggle was invented for either."""
    m = _launcher()
    genes = _space(m)
    assert "entry:o_convexc-entry:enabled" in genes
    assert "entry:o_convexp-entry:enabled" in genes


def test_the_put_arm_can_be_switched_off_leaving_the_call_arm_intact():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexp-entry:enabled": 0})
    ids = [r["id"] for r in trial["entry_rules"]]
    assert ids == ["o_convexc-entry"], f"put arm not dropped: {ids}"


def test_the_call_arm_can_be_switched_off_leaving_the_put_arm_intact():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexc-entry:enabled": 0})
    ids = [r["id"] for r in trial["entry_rules"]]
    assert ids == ["o_convexp-entry"], f"call arm not dropped: {ids}"


def test_dropping_the_put_rule_removes_the_bearish_gate_from_the_ruleset():
    """The recorded-chain proof the mutation table needs: drop the whole rule, and its bearish
    directional gate is genuinely gone from the emitted ruleset, not merely disabled."""
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexp-entry:enabled": 0})
    fields = [c["field"] for r in trial["entry_rules"]
             for c in r["conditions"]["conditions"]]
    assert "bearish" not in fields


# ---- fixed delta method -----------------------------------------------------------------------
def test_neither_member_searches_the_strike_METHOD():
    m = _launcher()
    genes = _space(m)
    offenders = [g for g in genes if g.endswith(":option_strike_method")]
    assert not offenders, f"O_CONVEX emits a strike-METHOD gene: {offenders}"
    for member in ("O_CONVEXC", "O_CONVEXP"):
        assert m._OPTION_STRATS[member]["option_strike_method"] == "delta"
        assert member in m._FIXED_DELTA_METHOD_STRATEGIES


def test_the_delta_band_is_in_the_design_range():
    m = _launcher()
    genes = _space(m)
    bands = {g: v for g, v in genes.items() if ":option_strike_delta" in g}
    assert len(bands) == 2, f"expected one delta band per arm, got {list(bands)}"
    for name, spec in bands.items():
        assert (spec["min"], spec["max"], spec["step"]) == (0.10, 0.35, 0.05), (
            f"{name}: {spec} != design §2's 0.10-0.35 step 0.05")


def test_the_delta_gene_moves_the_call_arms_strike_target():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexc-entry:a0:option_strike_delta": 0.35})
    action = _entry_rule(trial, "o_convexc-entry")["actions"][0]
    assert action["option_strike_method"] == "delta"
    assert action["option_strike_param"] == 0.35


def test_the_delta_gene_moves_the_put_arms_strike_target_INDEPENDENTLY():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexc-entry:a0:option_strike_delta": 0.10,
                         "entry:o_convexp-entry:a0:option_strike_delta": 0.35})
    call_action = _entry_rule(trial, "o_convexc-entry")["actions"][0]
    put_action = _entry_rule(trial, "o_convexp-entry")["actions"][0]
    assert call_action["option_strike_param"] == 0.10
    assert put_action["option_strike_param"] == 0.35


# ---- entry DTE 180-540 ------------------------------------------------------------------------
@pytest.mark.parametrize("member", ["O_CONVEXC", "O_CONVEXP"])
def test_no_o_convex_genome_can_decode_outside_the_180_540_band(member):
    """Exhaustive over the DTE gene's own lattice (7 levels), the same discipline
    test_no_o_ern_genome_can_decode_to_an_expiry_before_the_print uses for O_ERN's constraint:
    NO genome, not "not this one"."""
    from app.services.strategy_param_space import _apply_option_dte

    m = _launcher()
    cfg = m._OPTION_STRATS[member]
    lo, hi, step = (cfg["option_dte_min_range"], cfg["option_dte_max_range"],
                    cfg["option_dte_step"])
    centres = list(range(lo, hi + 1, step))
    assert len(centres) == 7, f"expected 7 searched DTE levels, got {centres}"
    for centre in centres:
        action = dict(cfg)
        _apply_option_dte(action, centre)
        assert 180 <= action["option_dte_min"] and action["option_dte_max"] <= 540, (
            f"{member} DTE centre {centre} decodes to {action['option_dte_min']}.."
            f"{action['option_dte_max']}, outside design §2's 180-540 band")


def test_the_dte_gene_moves_the_call_arms_selection_window():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexc-entry:a0:option_dte": 240})
    action = _entry_rule(trial, "o_convexc-entry")["actions"][0]
    assert (action["option_dte_min"], action["option_dte_max"]) == (180, 300)
    trial = _decoded(m, {"entry:o_convexc-entry:a0:option_dte": 480})
    action = _entry_rule(trial, "o_convexc-entry")["actions"][0]
    assert (action["option_dte_min"], action["option_dte_max"]) == (420, 540)


# ---- per-ticket premium sizing 0.5-2.0% of sleeve -------------------------------------------
def test_the_sizing_band_is_the_design_half_to_two_percent():
    m = _launcher()
    genes = _space(m)
    for member, prefix in (("O_CONVEXC", "o_convexc"), ("O_CONVEXP", "o_convexp")):
        spec = genes[f"entry:{prefix}-entry:a0:option_sizing"]
        assert (spec["min"], spec["max"], spec["step"]) == (0.5, 2.0, 0.25), member


def test_the_sizing_gene_reaches_the_action():
    m = _launcher()
    trial = _decoded(m, {"entry:o_convexc-entry:a0:option_sizing": 0.5,
                         "entry:o_convexp-entry:a0:option_sizing": 2.0})
    call_action = _entry_rule(trial, "o_convexc-entry")["actions"][0]
    put_action = _entry_rule(trial, "o_convexp-entry")["actions"][0]
    assert call_action["option_sizing"] == 0.5
    assert put_action["option_sizing"] == 2.0


# ---- take-profit multiple 3x-10x premium, plus hold-to-expiry -------------------------------
def test_the_tp_multiple_band_is_3_to_10x():
    m = _launcher()
    assert m._OPTION_TP_MULTIPLE_BANDS["O_CONVEX"] == (5, 3, 10, 1)


def test_the_tp_multiple_gene_reaches_the_emitted_exit_rule():
    m = _launcher()
    trial = _decoded(m, {"cond:tp_mult:value": 8})
    rule = next(r for r in trial["exit_rules"] if r["id"] == "opt_tp_mult")
    leaf = rule["conditions"]["conditions"][0]
    assert leaf["field"] == "profit_multiple_of_premium"
    assert leaf["op"] == ">="
    assert leaf["value"] == 8


def test_the_hold_to_expiry_arm_is_the_rules_own_toggle_off():
    """Design §2's "plus hold-to-expiry" arm is the SAME toggle_optimize gene the backspreads'
    "| to expiry" uses -- switching opt_tp_mult off, not a second rule or a sentinel value."""
    m = _launcher()
    genes = _space(m)
    assert "exit:opt_tp_mult:enabled" in genes
    trial = _decoded(m, {"exit:opt_tp_mult:enabled": 0})
    ids = {r["id"] for r in trial["exit_rules"]}
    assert "opt_tp_mult" not in ids, "opt_tp_mult must be REMOVABLE (hold-to-expiry arm)"


# ---- opt_sl_ml searchable, default OFF -------------------------------------------------------
def test_o_convex_is_in_the_sl_ml_authored_off_set():
    m = _launcher()
    assert "O_CONVEX" in m._OPTION_SL_ML_AUTHORED_OFF


def test_the_default_genome_carries_no_active_sl_ml_exit():
    """THE PIN: the emitted ruleset from a DEFAULT genome (no genes touched at all) carries
    opt_sl_ml with enabled=False -- design §2's "default OFF", checked on the actual emitted
    rule (mirrors test_opt_sl_ml_is_authored_OFF_but_still_searched's two-assertion shape for
    O_ERN/O_CBS/O_PBS)."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules("O_CONVEX") if r["id"] == "opt_sl_ml")
    assert rule["enabled"] is False
    assert rule["toggle_optimize"] is True
    assert "exit:opt_sl_ml:enabled" in _space(m)
    trial = _decoded(m, {})
    sl_ml = next(r for r in trial["exit_rules"] if r["id"] == "opt_sl_ml")
    assert sl_ml.get("enabled") is False


def test_sl_ml_can_still_be_dropped_by_the_GA_the_other_half_of_searchable():
    m = _launcher()
    trial = _decoded(m, {"exit:opt_sl_ml:enabled": 0})
    assert not any(r["id"] == "opt_sl_ml" for r in trial["exit_rules"])


# ---- max 1 concurrent ticket per underlying, FIXED (has_no_position) ------------------------
@pytest.mark.parametrize("member", ["O_CONVEXC", "O_CONVEXP"])
def test_one_ticket_per_underlying_is_the_fixed_has_no_position_guard(member):
    """The existing per-underlying concurrency knob: ``has_no_position`` is unconditional
    (no ``toggle_optimize``) on BOTH arms' entry rules -- the GA cannot switch it off. It is
    EXPERT-level per INSTRUMENT, not per-structure, so it also rules out holding a call AND a
    put on the same underlying at once: opening either blocks the other until the first
    closes. No new gene is needed or offered for "1 ticket per underlying"."""
    m = _launcher()
    leaf = next(c for c in m._option_entry_rule(member)["conditions"]["conditions"]
               if c["field"] == "has_no_position")
    assert "toggle_optimize" not in leaf, "has_no_position must not be a GA-toggleable gate"
    genes = _space(m)
    assert not any("flat" in g for g in genes), (
        "has_no_position must not surface as a gene at all")


# ---- debit partition, explicit and total ------------------------------------------------------
@pytest.mark.parametrize("member", ["O_CONVEXC", "O_CONVEXP"])
def test_o_convex_members_joined_the_debit_half(member):
    m = _launcher()
    assert member in m._DEBIT_OPTION_MEMBERS
    assert member not in m._CREDIT_OPTION_MEMBERS


def test_the_partition_is_still_total_with_o_convex_members_included():
    m = _launcher()
    assert m._DEBIT_OPTION_MEMBERS | m._CREDIT_OPTION_MEMBERS == set(m._OPTION_STRATS)
    assert not (m._DEBIT_OPTION_MEMBERS & m._CREDIT_OPTION_MEMBERS)


def test_o_convex_gets_the_debit_halfs_selection_weights():
    m = _launcher()
    genes = _space(m)
    for w in ("w_premium", "w_iv", "w_rvol"):
        assert f"optsel:debit:{w}" in genes, f"O_CONVEX is missing optsel:debit:{w}"


# ---- trade floor: O_CONVEX must NOT get the long-dated low floor ----------------------------
def test_o_convex_does_not_get_the_low_trade_floor():
    """The breadth floor (>=30 tickets/yr, >=20 underlyings) lives in option_convex's OWN
    fitness (design §3 item 4), not in the launcher's long-dated exemption -- O_CONVEX must
    keep the platform trade floor. (Restated here; the load-bearing pin lives in
    test_option_grid_foundations.py's test_no_other_strategy_is_touched_by_the_trade_floor,
    which now includes O_CONVEX in its parametrize list.)"""
    m = _launcher()
    assert "O_CONVEX" not in m._OPTION_LOW_TRADE_FLOOR_STRATEGIES
    block = {}
    m._apply_option_trade_floor("O_CONVEX", block)
    assert block == {}


# ---- fitness routing: the mutual-refusal seam -------------------------------------------------
def test_o_convex_refuses_option_car():
    m = _launcher()
    with pytest.raises(SystemExit, match="option_convex"):
        m._refuse_convex_fitness_mismatch("optimize", "O_CONVEX", "option_car")


def test_o_convex_refuses_the_bare_platform_default_too():
    """The auto-resolved default for a pure-option kind (option_consistent_annual_return) is
    just as much a mismatch as an explicit --fitness option_car -- the seam checks the
    EFFECTIVE fitness, so a bare ``--strategy O_CONVEX`` with no --fitness at all still
    refuses rather than silently scoring under CAR."""
    m = _launcher()
    resolved = m._resolve_fitness(None, "O_CONVEX", "consistent_annual_return")
    assert resolved != "option_convex"
    with pytest.raises(SystemExit, match="option_convex"):
        m._refuse_convex_fitness_mismatch("optimize", "O_CONVEX", resolved)


@pytest.mark.parametrize("other_kind", ["O_LEAPC", "O_ERN", "O_CBS", "O_LC", "S2", "O_STK"])
def test_option_convex_fitness_refuses_every_non_convex_kind(other_kind):
    m = _launcher()
    with pytest.raises(SystemExit, match="O_CONVEX"):
        m._refuse_convex_fitness_mismatch("optimize", other_kind, "option_convex")


def test_o_convex_with_option_convex_is_accepted():
    """The positive case: no refusal, no exception, nothing raised."""
    m = _launcher()
    m._refuse_convex_fitness_mismatch("optimize", "O_CONVEX", "option_convex")  # must not raise


def test_o_convex_is_excluded_from_the_car_default_set():
    """The root cause the seam exists to guard: without this exclusion O_CONVEX (or its
    members) would join _OPTION_CAR_STRATEGIES by construction and _resolve_fitness would
    default an unflagged run to option_consistent_annual_return."""
    m = _launcher()
    assert "O_CONVEX" not in m._OPTION_CAR_STRATEGIES
    assert "O_CONVEXC" not in m._OPTION_CAR_STRATEGIES
    assert "O_CONVEXP" not in m._OPTION_CAR_STRATEGIES
    assert "O_CONVEX" in m._PURE_OPTION_STRATEGIES
