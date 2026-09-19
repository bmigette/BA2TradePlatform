"""``_option_entry_rule``'s bullish/bearish rating gate must be toggleable, so the GA can rely
on the other entry gates alone rather than always requiring the expert's direction call.

SCOPE NARROWED 2026-08-27. This file used to also pin the four ``price_vs_target_*`` gates
(``test_price_target_gates_present_and_optimizable_with_correct_directions`` and
``test_every_pure_option_member_gets_all_four_price_target_gates``, both removed with them).
Those gates were deleted -- not weakened -- because ``PriceVsTargetLow/HighCondition`` reads
``expert_recommendation.data["FMPRating"][...]`` and only FMPRating writes that key, so under
any other expert all four failed CLOSED. They are replaced by the single expert-independent
``-exp_profit`` gate; that they must STAY gone is asserted in
test_option_grid_foundations.py::test_no_structure_gates_on_the_analyst_target_range.
"""
import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _find_cond(rule, cond_id):
    for c in rule["conditions"]["conditions"]:
        if c["id"] == cond_id:
            return c
    raise AssertionError(f"condition {cond_id} not found in rule {rule['id']}")


# --- the direction MODE gene (2026-09-19) ---------------------------------------------------
# The signal leaf used to be a FLAG (``bullish``/``bearish``) with an ON/OFF toggle: the GA could
# drop the direction requirement but never FLIP it, so a long call on a SELL signal -- the
# contrarian arm of every structure -- was unreachable by construction. It is now a NUMERIC leaf
# on ``rec_direction`` (the grade centred on HOLD: SELL -2 .. BUY +2) with the threshold pinned
# at 0 and a mode gene over off/below/above. Against 0 those are exactly "no filter" / "bearish"
# / "bullish", and the mode gene REPLACES the toggle gene, so the direction became searchable at
# zero net gene cost.

def test_signal_gate_is_a_direction_mode_gene_not_a_flag():
    rule = mod._option_entry_rule("O_LC")
    signal = _find_cond(rule, "o_lc-signal")
    assert signal["field"] == "rec_direction"
    assert signal["field_type"] == "numeric"
    assert signal["mode_optimize"] is True
    assert signal["mode_choices"] == ["off", "below", "above"]
    # the threshold is the fixed point of a signed scale, never a searched gene -- searching it
    # would put back the gene the mode gene replaced
    assert signal["value"] == 0.0
    assert signal["optimize"] is False
    # ... and the two cannot coexist: 'off' already removes the leaf
    assert "toggle_optimize" not in signal


def test_the_authored_mode_reproduces_the_flag_it_replaced():
    """Bullish members author ``>`` (== mode 'above'), bearish members ``<`` (== 'below').

    This is the parity statement: at the AUTHORED default the rule is the one the flag leaf
    expressed, so a run launched after this change starts from where the old one did.
    """
    assert _find_cond(mod._option_entry_rule("O_LC"), "o_lc-signal")["op"] == ">"
    assert _find_cond(mod._option_entry_rule("O_VERT"), "o_vert-signal")["op"] == ">"
    assert _find_cond(mod._option_entry_rule("O_LP"), "o_lp-signal")["op"] == "<"
    assert _find_cond(mod._option_entry_rule("O_BEARCS"), "o_bearcs-signal")["op"] == "<"
    for member, word in mod._OPTION_ENTRY_GATE.items():
        if member in mod._NEUTRAL_ENTRY_MEMBERS:
            continue
        leaf = _find_cond(mod._option_entry_rule(member), f"{member.lower()}-signal")
        assert leaf["op"] == mod._ENTRY_GATE_OP[mod._ENTRY_GATE_MODE[word]], member


def test_the_direction_field_is_mapped_or_the_gate_is_silently_dropped():
    """``triggers_from_condition_tree`` DROPS an unmapped field with only a WARNING -- every
    option strategy would then enter in BOTH directions while the GA kept tuning the mode."""
    from ba2_common.core.rule_builders import FIELD_EVENT
    assert "rec_direction" in FIELD_EVENT


#: The 15 pure-option structures that are LAUNCHABLE on their own (``--strategy <key>``), i.e.
#: the grid-1 set ``test_option_grid_foundations.PURE`` parametrises. The other
#: ``_OPTION_STRATS`` entries are group-only members (O_LEAPC/O_LEAPP, O_CONVEXC/O_CONVEXP),
#: grid-2 singles with their own budget tables (O_ERN/O_CBS/O_PBS), or phase-gated (O_PMCC).
_PURE_SINGLES = ["O_LC", "O_LP", "O_VERT", "O_BF", "O_BULLCS", "O_BEARCS", "O_BULLPS",
                 "O_CSP", "O_IC", "O_JL", "O_RS", "O_SSTD", "O_SSTG", "O_STRD", "O_STRG"]

#: Gene counts measured on the committed code IMMEDIATELY BEFORE the direction-mode change
#: (expert DeterministicScorer, ``collect_param_space(_build_strategy(...))``). Hand-
#: transcribed from that run so "unchanged" is checked against a RECORDED number rather than
#: against whatever the code happens to produce now.
_GENE_COUNTS_BEFORE_THE_DIRECTION_MODE = {
    "O_LC": 28, "O_LP": 28, "O_VERT": 28, "O_BF": 27, "O_BULLCS": 28, "O_BEARCS": 31,
    "O_BULLPS": 31, "O_CSP": 31, "O_IC": 30, "O_JL": 30, "O_RS": 30, "O_SSTD": 26,
    "O_SSTG": 27, "O_STRD": 25, "O_STRG": 28,
}


def test_every_member_spends_exactly_one_gene_on_its_direction_gate():
    """The leaf-level half of the NET-ZERO claim, over EVERY structure including the
    group-only and grid-2 ones: directional members carry ``mode_optimize`` and no toggle,
    neutral members the reverse. Never both -- ConditionLeaf and the gene collector each
    refuse the pair, because ``off`` already removes the leaf."""
    for member in sorted(mod._OPTION_STRATS):
        leaf = _find_cond(mod._option_entry_rule(member), f"{member.lower()}-signal")
        neutral = member in mod._NEUTRAL_ENTRY_MEMBERS
        assert bool(leaf.get("mode_optimize")) is not neutral, member
        assert bool(leaf.get("toggle_optimize")) is neutral, member


def test_the_mode_gene_replaces_the_toggle_gene_exactly():
    """NET ZERO GENES on the real collected space: the direction gate costs ONE gene before
    and after, it is just a different one.

    The mode gene is only free because ``mode_optimize`` and ``toggle_optimize`` cannot
    coexist and the threshold is not searched. If either changed, every directional member
    would grow a gene and the 31-gene grid-1 budget
    (test_option_grid_foundations::test_grid1_genomes_did_not_move) would have to move.
    """
    from app.services.strategy_param_space import collect_param_space

    for member in _PURE_SINGLES:
        space = collect_param_space(
            mod._build_strategy(member, f"g-{member}", "DeterministicScorer"))
        cid = f"{member.lower()}-signal"
        neutral = member in mod._NEUTRAL_ENTRY_MEMBERS
        assert (f"cond:{cid}:mode" in space) is not neutral, member
        assert (f"cond:{cid}:enabled" in space) is neutral, member
        assert len([g for g in space if g.startswith(f"cond:{cid}:")]) == 1, member


def test_no_pure_option_member_changed_gene_COUNT():
    """The budget statement the 31-gene pin depends on, stated PER MEMBER rather than as a
    ceiling: a swap that grew one member and shrank another would pass a ceiling test."""
    from app.services.strategy_param_space import collect_param_space

    assert set(_GENE_COUNTS_BEFORE_THE_DIRECTION_MODE) == set(_PURE_SINGLES)
    assert set(_PURE_SINGLES) <= set(mod._OPTION_STRATS)
    got = {member: len(collect_param_space(
        mod._build_strategy(member, f"g-{member}", "DeterministicScorer")))
        for member in _PURE_SINGLES}
    assert got == _GENE_COUNTS_BEFORE_THE_DIRECTION_MODE


def test_the_neutral_members_keep_their_FLAG_leaf():
    """off/below/above cannot say ``== HOLD``: that is an equality on the INTERIOR of the
    scale, not an ordering, and 'below OR above' is its complement rather than the thing
    itself. They also want no direction at all, so there is nothing for a mode gene to
    search."""
    for member in sorted(mod._NEUTRAL_ENTRY_MEMBERS):
        signal = _find_cond(mod._option_entry_rule(member), f"{member.lower()}-signal")
        assert signal["field"] == "current_rating_neutral", member
        assert signal["field_type"] == "flag", member
        assert signal["toggle_optimize"] is True, member
        assert "mode_optimize" not in signal, member


def test_every_pure_option_member_gets_the_expected_profit_gate():
    """The replacement for ``test_every_pure_option_member_gets_all_four_price_target_gates``.

    Same shape of guarantee -- every member carries the signal-strength gate, none is left with
    no signal gate at all -- on the field every expert can actually answer.
    """
    for member in mod._OPTION_STRATS:
        gate = _find_cond(mod._option_entry_rule(member), f"{member.lower()}-exp_profit")
        assert gate["field"] == "expected_profit_target_percent"
        assert gate["op"] == ">"
        assert gate["optimize"] is True
        assert gate["toggle_optimize"] is True


# --- non-directional members: neutral signal + low-conviction gate (2026-09-19) -------------
# A straddle/strangle/condor bets on the SIZE of the move, not its sign, so it must not gate on
# the expert's bullish flag. Measured on the stage-1 universe, the HOLD bucket is 15.6% of
# symbol-weeks against SELL's 2.3%, so the neutral reading is a real population to trade.

def test_non_directional_members_gate_on_the_neutral_reading():
    for member in sorted(mod._NEUTRAL_ENTRY_MEMBERS):
        rule = mod._option_entry_rule(member)
        signal = _find_cond(rule, f"{member.lower()}-signal")
        assert signal["field"] == "current_rating_neutral", member
        # still the GA's call -- the launcher widens the space, it does not impose the thesis
        assert signal["toggle_optimize"] is True, member


def test_non_directional_members_get_a_low_confidence_gate():
    for member in sorted(mod._NEUTRAL_ENTRY_MEMBERS):
        rule = mod._option_entry_rule(member)
        leaf = _find_cond(rule, f"{member.lower()}-low_confidence")
        assert leaf["field"] == "confidence", member
        # the WHOLE point: the shared leaf is hardcoded ">", this one must be the mirror
        assert leaf["op"] == "<=", member
        assert leaf["toggle_optimize"] is True, member
        assert leaf["optimize"] is True, member


def test_low_confidence_range_stays_under_the_scorer_ceiling():
    """A 'low conviction' gate at the clamp ceiling would gate nothing.

    DeterministicScorer's confidence tops out near 56, and _clamp_confidence_genes caps gates
    at 50. A low-confidence threshold of 50 would admit essentially every recommendation, so
    the range must sit well below it AND must survive the clamp unchanged.
    """
    ceiling = mod._EXPERT_CONFIDENCE_CEILING["DeterministicScorer"]
    leaf = mod._low_confidence_gate("o_strd")
    # The AUTHORED range now runs to 70 so a loose "anything but high conviction" gate is
    # expressible on an expert that can reach 100. What keeps it meaningful on the scorer is
    # the clamp, not the authored ceiling: _clamp_confidence_genes caps it at 50 there.
    assert leaf["value_max"] == 70
    assert leaf["value_min"] == 10
    assert leaf["value_min"] < ceiling, "the floor must stay reachable under the clamp"


def test_low_confidence_range_is_clamped_for_the_scorer():
    """The widened ceiling must still be cut to what DeterministicScorer can emit."""
    strat = mod._build_strategy("O_STRD", "x", "DeterministicScorer")
    leaves = [c for r in strat.entry_rules for c in r["conditions"]["conditions"]
              if isinstance(c, dict) and c.get("id", "").endswith("-low_confidence")]
    assert leaves, "the low-confidence leaf is missing from the built strategy"
    ceiling = mod._EXPERT_CONFIDENCE_CEILING["DeterministicScorer"]
    for leaf in leaves:
        assert leaf["value_max"] <= ceiling, leaf


def test_directional_members_get_no_low_confidence_gate():
    """The leaf is per-member on purpose; a shared id would give it to structures that never
    want it."""
    for member in ("O_LC", "O_LP", "O_BEARCS", "O_VERT"):
        rule = mod._option_entry_rule(member)
        ids = [c["id"] for c in rule["conditions"]["conditions"]]
        assert f"{member.lower()}-low_confidence" not in ids, member


def test_directional_members_keep_their_direction_gate():
    """Regression fence for the neutral override: it must not leak onto directional keys."""
    assert mod._OPTION_ENTRY_GATE["O_LC"] == "bullish"
    assert mod._OPTION_ENTRY_GATE["O_LP"] == "bearish"
    assert mod._OPTION_ENTRY_GATE["O_BEARCS"] == "bearish"
    assert mod._OPTION_ENTRY_GATE["O_VERT"] == "bullish"


def test_neutral_flag_field_is_a_known_condition():
    """`current_rating_neutral` must be in the flag vocabulary or the leaf is silently dropped.

    An unmapped flag field is the exact silent-drop hole rule_builders documents; a gate that
    vanishes would make these structures fire unconditionally and look like a strategy result.
    """
    from ba2_common.core.rule_builders import FLAG_FIELD_EVENT
    assert "current_rating_neutral" in FLAG_FIELD_EVENT


def test_scorer_section_weights_can_express_single_section_configs():
    """technical-only and fundamental-only must both be reachable."""
    params = mod._EXPERT_OPT["DeterministicScorer"]["expert_params"]
    assert params["w_technical"]["min"] == 0.0
    assert params["w_fundamental"]["min"] == 0.0
    # and the optional legs already could
    assert params["w_analyst"]["min"] == 0.0
    assert params["w_earnings"]["min"] == 0.0


def test_neutral_members_swap_the_confidence_gate_rather_than_adding_one():
    """The low-conviction gate REPLACES the shared high-conviction one for these members.

    Two reasons, both load-bearing. (1) Budget: adding it would push O_IC to 32 genes, past
    the 31-gene grid-1 ceiling pinned in test_option_grid_foundations. (2) Semantics: a
    neutral-gated member only fires on HOLD, and HOLD means the composite sat below the
    entry threshold, so `confidence > 40` alongside `current_rating_neutral` is close to
    unsatisfiable. Keeping both would spend genes on a contradiction.
    """
    for member in sorted(mod._NEUTRAL_ENTRY_MEMBERS):
        ids = [c["id"] for c in mod._option_entry_rule(member)["conditions"]["conditions"]]
        assert "shared-gate_confidence" not in ids, member
        assert f"{member.lower()}-low_confidence" in ids, member
    # ... and the directional members keep it, unchanged
    for member in ("O_LC", "O_LP", "O_BEARCS"):
        ids = [c["id"] for c in mod._option_entry_rule(member)["conditions"]["conditions"]]
        assert "shared-gate_confidence" in ids, member
