"""The ARC richness floor must be a SEARCHED GENE, not a constant (OPT-C1).

Enforcement landed in ``TradeActions`` (see
``packages/common/tests/test_option_arc_gate_enforced.py``). A gate the GA cannot see is the
defect this whole track is removing -- ``option_min_volume`` was plumbed end to end through
five layers and stayed completely inert because no caller ever set a value -- so these tests
assert the WIRING: that every credit grid strategy carries the gene, that no debit one does,
and that a decoded value survives into the action config the engine constructs.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import pytest

import ba2test_launcher as L
from app.services.strategy_param_space import _collect_action_genes, _decode_rule_list


CREDIT_KINDS = sorted(k for k, cfg in L._OPTION_STRATS.items()
                      if cfg["action_type"] in L._OPTION_ARC_BANDS)
DEBIT_KINDS = sorted(set(L._OPTION_STRATS) - set(CREDIT_KINDS))


# --------------------------------------------------------------------------- #
# 1. The band table matches the reserve table it is derived from
# --------------------------------------------------------------------------- #
def test_the_band_table_covers_every_credit_builder():
    """DRIFT GUARD. A structure in ``RESERVING_STRATEGIES`` posts collateral, so it has an
    ARC and its builder consults the gate; without a band here it would keep a frozen floor
    of None (gate off) while every sibling searched one. The exemptions -- the three pricing
    aliases with no builder, and the two backspreads whose builders deliberately do not
    consult the gate -- are named ONCE, in
    ``ba2_common.core.option_economics.ARC_FLOOR_EXEMPT_STRATEGIES``, so this guard and its
    packages/common sibling cannot drift apart.
    """
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    from ba2_common.core.option_economics import ARC_FLOOR_EXEMPT_STRATEGIES

    named = {strategy for strategy, _band in L._OPTION_ARC_BANDS.values()}
    assert named == (set(OptionsAccountInterface.RESERVING_STRATEGIES)
                     - ARC_FLOOR_EXEMPT_STRATEGIES)


def test_every_band_starts_at_zero_and_is_a_usable_range():
    """0.0 is the control arm ("the credit may be thin, but it must be PRICEABLE" -- a
    configured 0.0 still refuses an unmeasurable ARC), and each band must offer the GA more
    than one level or the gene is a constant with extra steps."""
    for action_type, (_strategy, (lo, hi, step)) in L._OPTION_ARC_BANDS.items():
        assert lo == 0.0, action_type
        assert hi > lo and step > 0, action_type
        assert (hi - lo) / step >= 2, f"{action_type} offers too few levels to search"


# --------------------------------------------------------------------------- #
# 2. Every credit strategy carries the gene; no debit one does
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", CREDIT_KINDS)
def test_every_credit_strategy_searches_the_arc_floor(kind):
    cfg = L._option_entry_action_for(kind)
    _strategy, (lo, hi, step) = L._OPTION_ARC_BANDS[cfg["action_type"]]
    assert cfg["option_min_arc_optimize"] is True
    assert (cfg["option_min_arc_min"], cfg["option_min_arc_max"],
            cfg["option_min_arc_step"]) == (lo, hi, step)


@pytest.mark.parametrize("kind", DEBIT_KINDS)
def test_no_debit_strategy_carries_an_arc_floor(kind):
    """A debit structure reserves nothing, so its ARC is None and ANY configured floor --
    including 0.0 -- refuses it. Emitting the gene here would delete these structures from
    the search the moment the GA sampled any level."""
    cfg = L._option_entry_action_for(kind)
    assert not any(k.startswith("option_min_arc") for k in cfg), sorted(cfg)


def test_the_credit_and_debit_halves_are_both_non_empty():
    """Guards the two parametrised tests above against silently degenerating into no-ops."""
    assert CREDIT_KINDS and DEBIT_KINDS


def test_the_covered_call_overlay_carries_no_arc_floor():
    """A covered call collects a credit but is collateralised by SHARES, not cash
    (``ZERO_RESERVE_STRATEGIES``), so the gate does not apply and its builder does not
    consult it."""
    cfg = L._option_overlay_action("sell_covered_call", strike_param=5.0,
                                   strike_min=2.0, strike_max=10.0, strike_step=2.0)
    assert not any(k.startswith("option_min_arc") for k in cfg), sorted(cfg)


# --------------------------------------------------------------------------- #
# 3. The gene is collected and decoded back onto the action
# --------------------------------------------------------------------------- #
def test_the_gene_is_collected_from_a_real_strategy_config():
    action = L._option_entry_action_for("O_CSP")
    out = {}
    _collect_action_genes("entry", "enter", 0, action, out)
    entry = out["entry:enter:a0:option_min_arc"]
    assert entry["min"] == 0.0 and entry["max"] == 0.30


def test_a_decoded_value_reaches_the_action_config():
    """The hop that makes it real: a gene value must land on ``option_min_arc``, which
    ``rule_builders`` maps to the ``min_arc`` the credit builders read."""
    rules = [{"id": "enter", "actions": [L._option_entry_action_for("O_CSP")]}]
    decoded = _decode_rule_list(
        rules, "entry", {"enter": {"a0": {"option_min_arc": 0.22}}}, {})
    assert decoded[0]["actions"][0]["option_min_arc"] == 0.22


def test_the_decoded_value_survives_the_rule_builder_into_the_action_kwargs():
    """End to end: decoded gene -> rule dict -> action config key the evaluator forwards."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    from ba2_common.core.rule_builders import action_from_rule

    rules = [{"id": "enter", "actions": [L._option_entry_action_for("O_IC")]}]
    decoded = _decode_rule_list(
        rules, "entry", {"enter": {"a0": {"option_min_arc": 1.5}}}, {})
    cfg = action_from_rule(decoded[0]["actions"][0])["act"]
    assert cfg["min_arc"] == 1.5
    assert "min_arc" in _OPTION_ENTRY_PARAM_KEYS
