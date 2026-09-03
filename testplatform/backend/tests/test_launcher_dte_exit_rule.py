"""The option grid must be able to optimise a REMAINING-LIFE exit (roll-at-DTE).

``_option_exit_rules`` shipped two exits — a premium-profit TP and a ``days_opened``
time stop — and neither expresses remaining option life:

* ``days_opened`` counts days ELAPSED. The entry DTE window is itself a gene
  (``option_dte``, decoded to a >= 14-day-wide window), so "28 days after opening" lands
  on a different remaining life in every trial. The two are not interchangeable.
* roll-at-DTE therefore existed ONLY inside the hardcoded ``OptionPortfolioManager``:
  unreachable from any ruleset, so the GA could not optimise the roll point.
* the planned 0DTE arm has no exit criterion at all unless the threshold can reach 0.

A DTE exit is meaningful for BOTH payoff profiles — a long debit structure wants out
before terminal theta, a short credit structure wants out before gamma — so unlike the
TP/SL bands it is NOT gated on ``_DEBIT_OPTION_KINDS``.

The gene itself must come out of the UNMODIFIED collector: ``_walk_condition_nodes``
emits ``cond:<id>:value`` / ``:enabled`` for any optimizable leaf and
``_collect_rule_list`` emits ``exit:<rid>:enabled`` for any toggleable rule, neither of
which knows field names. This file asserts that end-to-end so nobody "fixes" the
param-space collector to special-case the field.

And the leaf must survive RULESET SEEDING: ``triggers_from_condition_tree`` silently
drops any field missing from ``FIELD_EVENT``, which would leave a gene the GA happily
tunes and the engine never sees.
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

DTE_RULE_ID = "opt_dte"
DTE_COND_ID = "dte"


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


_ALL_KINDS = set(mod._OPTION_STRATS) | set(mod._OPTION_GROUPS)
_DEBIT = sorted(mod._DEBIT_OPTION_KINDS)
_CREDIT = sorted(_ALL_KINDS - mod._DEBIT_OPTION_KINDS)


# ---------------------------------------------------------------------------
# present on both payoff profiles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", _DEBIT)
def test_debit_kinds_get_the_dte_exit(kind):
    leaf = _leaf(_rule(kind, DTE_RULE_ID), DTE_COND_ID)
    assert leaf["field"] == "days_to_expiry"
    assert leaf["op"] == "<="


@pytest.mark.parametrize("kind", _CREDIT)
def test_credit_kinds_get_the_dte_exit(kind):
    """Not gated on ``_DEBIT_OPTION_KINDS`` — short premium needs the roll window most."""
    leaf = _leaf(_rule(kind, DTE_RULE_ID), DTE_COND_ID)
    assert leaf["field"] == "days_to_expiry"
    assert leaf["op"] == "<="


def test_every_pure_option_strategy_key_gets_it():
    for kind in sorted(mod._PURE_OPTION_STRATEGIES):
        _leaf(_rule(kind, DTE_RULE_ID), DTE_COND_ID)


def test_the_band_is_identical_on_debit_and_credit():
    """One band, deliberately: unlike the TP band this is not a payoff-profile choice."""
    debit = _leaf(_rule("O_LC", DTE_RULE_ID), DTE_COND_ID)
    credit = _leaf(_rule("O_SSTG", DTE_RULE_ID), DTE_COND_ID)
    for key in ("value", "value_min", "value_max", "value_step", "op"):
        assert debit[key] == credit[key], key


# ---------------------------------------------------------------------------
# the band
# ---------------------------------------------------------------------------
def test_the_band_reaches_zero_and_stops_at_the_conventional_roll_point():
    leaf = _leaf(_rule("O_LC", DTE_RULE_ID), DTE_COND_ID)
    assert leaf["optimize"] is True
    # 0 is REQUIRED: a 0DTE arm's only exit is "close on the expiry day".
    assert leaf["value_min"] == 0
    # 21 is the conventional roll point for 30-45 DTE structures, and the grid's entry
    # windows bottom out near 10-13 DTE, so a larger cap would only buy a degenerate
    # "open and immediately close" region.
    assert leaf["value_max"] == 21
    assert leaf["value_step"] == 3
    assert leaf["value"] == 21
    # every gene level must be on the grid, 0 and 21 included
    assert (leaf["value_max"] - leaf["value_min"]) % leaf["value_step"] == 0


def test_the_rule_is_toggleable():
    """The GA must be able to turn the DTE exit OFF entirely, like its two siblings."""
    assert _rule("O_LC", DTE_RULE_ID)["toggle_optimize"] is True
    assert _rule("O_SSTG", DTE_RULE_ID)["toggle_optimize"] is True


def test_it_closes_the_option():
    assert _rule("O_LC", DTE_RULE_ID)["action_type"] == "close_option"


@pytest.mark.parametrize("kind", ["O_LC", "O_SSTG"])
def test_each_exit_is_a_SEPARATE_single_leaf_rule(kind):
    """Conditions inside one rule are ANDed. Folded into ``opt_time`` (or ``opt_tp``) the
    DTE exit would need BOTH "held 28 days" AND "21 days left" — a conjunction neither
    exit asked for — and it would lose its own on/off gene. Asserted as an EXACT map so
    an extra leaf anywhere is a failure, not something the other assertions miss."""
    expected = {"opt_tp": ["profit_loss_percent"],
                "opt_time": ["days_opened"],
                DTE_RULE_ID: ["days_to_expiry"]}
    if kind not in mod._DEBIT_OPTION_KINDS:
        expected["opt_sl"] = ["profit_loss_percent"]
    if kind not in mod._UNDEFINED_RISK_MEMBERS:
        # Task 9: every MEASURED-max-loss structure also carries the max-loss-scaled stop.
        expected["opt_sl_ml"] = ["loss_pct_of_max_loss"]

    actual = {r["id"]: [c["field"] for c in r["conditions"]["conditions"]]
              for r in mod._option_exit_rules(kind)}
    assert actual == expected


def test_the_existing_exits_are_untouched():
    """Regression guard: adding an exit must not perturb the TP / time bands the GA has
    already been tuned against."""
    tp = _leaf(_rule("O_LC", "opt_tp"), "tp")
    assert (tp["value"], tp["value_min"], tp["value_max"], tp["value_step"]) == (
        100, 25, 200, 25)
    td = _leaf(_rule("O_LC", "opt_time"), "td")
    assert (td["value"], td["value_min"], td["value_max"], td["value_step"]) == (
        28, 10, 45, 5)
    credit_tp = _leaf(_rule("O_SSTG", "opt_tp"), "tp")
    assert (credit_tp["value"], credit_tp["value_min"], credit_tp["value_max"]) == (
        50, 25, 75)
    sl = _leaf(_rule("O_SSTG", "opt_sl"), "sl")
    assert (sl["value"], sl["value_min"], sl["value_max"]) == (-100, -200, -50)


# ---------------------------------------------------------------------------
# the gene, from the UNMODIFIED collector
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,builder", [("O_LC", "_build_strategy_option"),
                                          ("OS2", "_build_strategy_option_group")])
def test_genes_are_emitted_with_no_param_space_change(kind, builder):
    from app.services.strategy_param_space import collect_param_space

    space = collect_param_space(getattr(mod, builder)(kind))
    assert space[f"cond:{DTE_COND_ID}:value"] == {"type": "float", "min": 0.0,
                                                  "max": 21.0, "step": 3.0}
    assert space[f"exit:{DTE_RULE_ID}:enabled"] == {"type": "int", "min": 0, "max": 1,
                                                    "step": 1}


def test_gene_decodes_back_onto_the_condition():
    from app.services.strategy_param_space import decode_params

    s = mod._build_strategy_option("O_LC")
    decoded = decode_params(s, {f"cond:{DTE_COND_ID}:value": 6})
    rule = next(r for r in decoded["exit_rules"] if r["id"] == DTE_RULE_ID)
    leaf = next(c for c in rule["conditions"]["conditions"] if c["id"] == DTE_COND_ID)
    assert leaf["value"] == 6
    assert leaf["field"] == "days_to_expiry"


def test_zero_decodes_as_zero_not_as_absent():
    """0 is a legitimate level (the 0DTE arm). A falsy-value bug would silently leave the
    default 21 in place and make the whole bottom of the band unreachable."""
    from app.services.strategy_param_space import decode_params

    s = mod._build_strategy_option("O_LC")
    decoded = decode_params(s, {f"cond:{DTE_COND_ID}:value": 0})
    rule = next(r for r in decoded["exit_rules"] if r["id"] == DTE_RULE_ID)
    leaf = next(c for c in rule["conditions"]["conditions"] if c["id"] == DTE_COND_ID)
    assert leaf["value"] == 0


def test_toggling_the_gene_off_drops_the_rule():
    from app.services.strategy_param_space import decode_params

    s = mod._build_strategy_option("O_LC")
    assert DTE_RULE_ID in [r["id"] for r in s.exit_rules]   # it is there to begin with
    decoded = decode_params(s, {f"exit:{DTE_RULE_ID}:enabled": 0})
    assert DTE_RULE_ID not in [r["id"] for r in decoded["exit_rules"]]
    # ... and the siblings survive
    assert "opt_tp" in [r["id"] for r in decoded["exit_rules"]]


# ---------------------------------------------------------------------------
# the leaf must survive ruleset seeding (else the gene is inert)
# ---------------------------------------------------------------------------
def test_the_leaf_becomes_a_real_engine_trigger():
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    s = mod._build_strategy_option("O_LC")
    rule = next(r for r in s.exit_rules if r["id"] == DTE_RULE_ID)
    triggers = triggers_from_condition_tree(rule["conditions"])
    assert [t["event_type"] for t in triggers.values()] == ["days_to_expiry"], (
        "the days_to_expiry leaf was DROPPED by triggers_from_condition_tree — the field "
        "is missing from FIELD_EVENT, so the GA would tune a gene the engine cannot see")
    assert list(triggers.values())[0]["operator"] == "<="


def test_the_engine_can_build_the_condition_for_that_trigger():
    """The last link: the seeded event_type must resolve to a real condition class."""
    from ba2_common.core.TradeConditions import DaysToExpiryCondition, create_condition
    from ba2_common.core.rule_builders import triggers_from_condition_tree
    from ba2_common.core.types import ExpertEventType

    s = mod._build_strategy_option("O_LC")
    rule = next(r for r in s.exit_rules if r["id"] == DTE_RULE_ID)
    cfg = list(triggers_from_condition_tree(rule["conditions"]).values())[0]
    cond = create_condition(
        event_type=ExpertEventType(cfg["event_type"]), account=object(),
        instrument_name="AAPL", expert_recommendation=object(), existing_order=None,
        operator_str=cfg["operator"], value=cfg["value"])
    assert isinstance(cond, DaysToExpiryCondition)
