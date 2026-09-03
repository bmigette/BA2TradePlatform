"""``O_BULLPS`` — the bull put credit spread, wired into the option grid.

THE GAP. After the grid's defined-risk filter removes the short straddle/strangle and
``_FULL_NOTIONAL_OPTION_KINDS`` strips ``O_CSP``/``O_JL``/``O_RS`` (a CSP reserves
~$28,800 at spot 320 against the default capital), the searched credit residue was
``O_IC`` (neutral) and ``O_BEARCS`` (bearish). **The sell arm had no bullish
defined-risk credit expression at all** — the GA could express "sell premium, expect
down" and "sell premium, expect nothing", never "sell premium, expect up".

``O_BULLPS`` is the exact mirror of ``O_BEARCS``: a two-leg directional credit vertical,
sized off max loss, priced at the same $160-$1,280 per contract across the universe, and
therefore affordable everywhere ``O_BEARCS`` already is.

WHY OS3 AND NOT OS2. OS2 is the DELTA-NEUTRAL credit family (short strangle/straddle,
iron condor); OS3 is the skewed/directional one, and ``O_BEARCS`` is already there for
exactly this reason. A put credit spread is directional-bullish, so it belongs beside it.

WHY IT MUST NOT BE IN ``_DEBIT_OPTION_KINDS``. That set does not describe the direction
of the trade, it selects the EXIT TEMPLATE: debit kinds get the wide 25-200% TP band (long
premium lives off the right tail) and NO stop-loss; credit kinds get the tastytrade-style
25-75% band plus a toggleable stop at -100% of credit. Put a credit structure in the debit
set and it silently receives the long-premium exit profile — a TP it can never reach
(a credit spread's max gain is 100% of the credit) and no left-tail management at all.
Nothing errors; the arm simply searches a profile that does not fit its payoff.
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

KIND = "O_BULLPS"
RID = "o_bullps-entry"


# ==========================================================================
# the four registrations
# ==========================================================================
def test_the_key_exists_and_names_the_action():
    cfg = mod._OPTION_STRATS[KIND]
    assert cfg["action_type"] == "open_bull_put_spread"
    assert cfg["option_strike_method"] == "percent_otm"
    assert "option_strike_param" in cfg


def test_it_is_registered_in_the_strategy_builders():
    """Absent here, ``--strategies O_BULLPS`` exits with "unknown strategy" and the
    grouped job that lists it as a member cannot build."""
    assert KIND in mod._STRATEGY_BUILDERS
    assert KIND in mod._PURE_OPTION_STRATEGIES
    assert KIND in mod._OPTION_STRATEGY_KEYS


def test_it_joins_the_skewed_credit_family_OS3():
    """The taxonomy (``_OPTION_GROUPS_ALL``) is the source of truth; ``_OPTION_GROUPS``
    is derived from it by the affordability filter, so listing it in only one of the two
    is how a member goes missing from the searched job while looking present."""
    assert KIND in mod._OPTION_GROUPS_ALL["OS3"]
    assert KIND in mod._OPTION_GROUPS["OS3"], (
        "a defined-risk vertical is NOT full-notional — it must survive the "
        "_FULL_NOTIONAL_OPTION_KINDS filter and actually be searched")
    assert KIND not in mod._FULL_NOTIONAL_OPTION_KINDS
    # ...and only OS3.
    elsewhere = [g for g, members in mod._OPTION_GROUPS_ALL.items()
                 if KIND in members and g != "OS3"]
    assert elsewhere == [], elsewhere


def test_it_is_a_CREDIT_structure_and_must_not_be_treated_as_debit():
    assert KIND not in mod._DEBIT_OPTION_KINDS


def test_the_sell_arm_now_has_a_bullish_defined_risk_credit_expression():
    """The whole point of the commit, asserted as the property rather than the wiring.

    ``O_BEARCS`` is bearish, ``O_IC`` neutral. Before this key the SEARCHED credit set
    (what survives both filters and lands in a group) contained no bullish member.
    """
    searched_credit = {m for g, members in mod._OPTION_GROUPS.items()
                       for m in members if m not in mod._DEBIT_OPTION_KINDS}
    assert KIND in searched_credit
    assert mod._OPTION_ENTRY_GATE[KIND] == "bullish"
    bullish_credit = {m for m in searched_credit
                      if mod._OPTION_ENTRY_GATE[m] == "bullish"
                      and m not in ("O_SSTG", "O_SSTD", "O_IC")}
    assert KIND in bullish_credit


# ==========================================================================
# the exit template it receives
# ==========================================================================
def _rule(kind, rule_id):
    for r in mod._option_exit_rules(kind):
        if r["id"] == rule_id:
            return r
    raise AssertionError(f"{kind}: exit rule {rule_id!r} not found "
                         f"(have {[r['id'] for r in mod._option_exit_rules(kind)]})")


def _leaf(rule, cond_id):
    for c in rule["conditions"]["conditions"]:
        if c["id"] == cond_id:
            return c
    raise AssertionError(f"condition {cond_id!r} not in rule {rule['id']!r}")


def test_it_gets_the_CREDIT_exit_profile_byte_for_byte_like_its_mirror():
    """Compared against ``O_BEARCS`` rather than to literals: the two are the same shape
    of structure and must not drift apart, and the comparison catches a wrong
    ``_DEBIT_OPTION_KINDS`` membership in one assertion."""
    assert mod._option_exit_rules(KIND) == mod._option_exit_rules("O_BEARCS")


def test_the_credit_profile_is_not_the_debit_one():
    """Stated positively so the previous test cannot pass by both sides being wrong."""
    tp = _leaf(_rule(KIND, "opt_tp"), "tp")
    assert (tp["value"], tp["value_min"], tp["value_max"]) == (50, 25, 75)
    debit_tp = _leaf(_rule("O_LC", "opt_tp"), "tp")
    assert (debit_tp["value"], debit_tp["value_min"], debit_tp["value_max"]) == (100, 25, 200)
    assert tp != debit_tp


def test_a_credit_structure_gets_the_left_tail_stop_loss():
    """Only the credit branch appends ``opt_sl``. Short premium's whole risk is the left
    tail; the debit kinds deliberately have none."""
    sl = _leaf(_rule(KIND, "opt_sl"), "sl")
    assert (sl["value"], sl["value_min"], sl["value_max"]) == (-100, -200, -50)
    with pytest.raises(AssertionError):
        _rule("O_LC", "opt_sl")


def test_it_still_gets_the_roll_at_DTE_exit():
    """Not gated on the payoff profile — short premium wants out of terminal gamma."""
    leaf = _leaf(_rule(KIND, "opt_dte"), "dte")
    assert leaf["field"] == "days_to_expiry" and leaf["op"] == "<="


# ==========================================================================
# it builds, and its genes are emitted
# ==========================================================================
def test_the_standalone_strategy_builds_and_carries_the_entry_action():
    strat = mod._build_strategy(KIND, KIND, "FMPRating")
    ea = getattr(strat, "entry_action", None)
    assert ea is not None and ea["action_type"] == "open_bull_put_spread"
    assert ea["option_min_volume"] == mod._OPTION_MIN_VOLUME_DEFAULT


def test_it_gates_on_the_BULLISH_signal():
    """A put credit spread pays while the underlying stays UP. Gating it bearish (the
    ``O_BEARCS`` copy-paste) would run it into exactly the move that breaks it."""
    strat = mod._build_strategy(KIND, KIND, "FMPRating")
    fields = [c.get("field") for c in strat.entry_rules[0]["conditions"]["conditions"]]
    assert "bullish" in fields and "bearish" not in fields, fields


def test_its_genes_are_emitted_standalone():
    from app.services.strategy_param_space import collect_param_space

    space = collect_param_space(mod._build_strategy_option(KIND))
    assert f"entry:{RID}:a0:option_strike_param" in space
    assert f"entry:{RID}:a0:option_dte" in space
    # The confidence gate is keyed ``shared-gate_confidence``, NOT ``{member}-...``: expert
    # conviction in a symbol is structure-independent, so one gene serves a whole group, and the
    # same id is used standalone so a single-structure winner's key is still known in the group
    # space. Only the direction-dependent gates (iv_rank, iv_rv, signal) keep a member prefix.
    assert "cond:shared-gate_confidence:value" in space


def test_its_genes_are_emitted_inside_the_OS3_group():
    """A group member with no toggle gene cannot be turned on or off by the GA, so the
    family job would search a structure it can never drop (or never reach)."""
    from app.services.strategy_param_space import collect_param_space

    space = collect_param_space(mod._build_strategy_option_group("OS3"))
    assert f"entry:{RID}:enabled" in space
    assert f"entry:{RID}:a0:option_strike_param" in space
    assert f"entry:{RID}:a0:option_dte" in space


def test_the_OS3_group_builds_with_it_as_a_member():
    strat = mod._build_strategy("OS3", "OS3", "FMPRating")
    rids = [r.get("id") for r in strat.entry_rules]
    assert RID in rids
    rule = next(r for r in strat.entry_rules if r["id"] == RID)
    assert rule["actions"][0]["action_type"] == "open_bull_put_spread"
    assert rule.get("toggle_optimize") is True


def test_the_group_decode_can_select_it_alone():
    from app.services.strategy_param_space import decode_params

    strat = mod._build_strategy("OS3", "OS3", "FMPRating")
    members = mod._OPTION_GROUPS["OS3"]
    flat = {f"entry:{m.lower()}-entry:enabled": (1 if m == KIND else 0) for m in members}
    decoded = decode_params(strat, flat)
    assert [r.get("id") for r in (decoded.get("entry_rules") or [])] == [RID]


def test_this_key_is_not_a_duplicate_of_an_existing_one():
    """A copy-pasted key that kept its donor's ``action_type`` looks like a new structure
    and searches the old one.

    SCOPED TO ``KIND`` (2026-09-01, grid 2). This used to assert that every ``action_type``
    in ``_OPTION_STRATS`` was unique -- a whole-table claim living in a file about ONE key.
    Grid 2 deliberately reuses three builders at different tenors and deltas (``O_LEAPC`` is
    ``buy_call`` like ``O_LC``; see ``test_option_strategy_builders``'s
    ``test_no_two_keys_search_the_SAME_structure_the_same_way``, which is where the
    table-wide invariant now lives, restated as "no two keys search the same space").
    What belongs HERE is the claim this file is about: O_BULLPS introduced a genuinely new
    structure, so nothing else may carry its action_type.
    """
    at = mod._OPTION_STRATS[KIND]["action_type"]
    others = [k for k, v in mod._OPTION_STRATS.items()
              if k != KIND and v["action_type"] == at]
    assert not others, f"{KIND}'s action_type {at!r} is also used by {others}"


def test_the_engine_routes_it_down_the_option_entry_path():
    """``daily_engine._entry_is_option`` keys off ``is_option_action`` on the entry
    action; a value missing from ``get_option_action_values`` silently becomes an equity
    entry."""
    from ba2_common.core.types import is_option_action

    ea = getattr(mod._build_strategy(KIND, KIND, "FMPRating"), "entry_action")
    assert is_option_action(str(ea["action_type"]))


def test_the_rule_builder_forwards_its_selection_params():
    """The backtest builds the option ``TradeAction`` from these keys; a structure the
    builder does not recognise as an option action loses them all."""
    from ba2_common.core.rule_builders import action_from_rule

    rule = dict(mod._option_entry_action_for(KIND))
    cfg = action_from_rule(rule)["act"]
    assert cfg["action_type"] == "open_bull_put_spread"
    assert cfg["strike_method"] == "percent_otm"
    assert cfg["strike_param"] == mod._OPTION_STRATS[KIND]["option_strike_param"]
    assert cfg["dte_min"] and cfg["dte_max"] and cfg["sizing"]
