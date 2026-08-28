"""``option_entry_cross`` must be a SEARCHED gene on every option entry, defaulting to a no-op.

Enforcement and direction land in ``TradeActions`` / ``option_entry_quote`` (see
``packages/common/tests/test_option_entry_cross_gene.py``) and the fill-side closure in
``tests/backtest/test_option_entry_cross_fill.py``. These assert the WIRING -- the failure mode
this whole track keeps hitting is a knob that is plumbed through five layers and stays inert
because no producer ever emits it (``option_min_volume`` did exactly that).

WHY IT IS SEARCHABLE AT ALL. The default ``next_bar_open`` fill model makes the NEXT bar cross a
quote struck on the ANALYSIS bar, and the historical option store's ``bid == ask`` puts that
quote at the MID -- so an entry must earn the whole modelled spread back overnight before
anything fills. How much of that spread an entry should give up is a real trade-off (price
against fill probability) with no obvious right answer, which is precisely what the GA is for.
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

from app.services.strategy_param_space import (  # noqa: E402
    _collect_action_genes,
    _decode_rule_list,
    collect_param_space,
)

_SINGLES = sorted(mod._OPTION_STRATS)
_GROUPS = sorted(mod._OPTION_GROUPS)


def _build(kind):
    if kind in mod._OPTION_GROUPS:
        return mod._build_strategy_option_group(kind)
    return mod._build_strategy_option(kind)


# --------------------------------------------------------------------------- #
# 1. the authored default is the exact no-op
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", _SINGLES)
def test_every_option_structure_is_authored_at_the_neutral_value(kind):
    """THE REQUIREMENT. 0.0 quotes at the mid -- the pre-F3 behaviour -- so a run that does not
    search this gene produces byte-identical numbers to one from before it existed."""
    from ba2_common.core.option_entry_quote import ENTRY_CROSS_NEUTRAL

    cfg = mod._option_entry_action_for(kind)
    assert cfg["option_entry_cross"] == ENTRY_CROSS_NEUTRAL == 0.0


def test_the_overlay_actions_are_authored_at_the_neutral_value_too():
    """O_CC's covered call and O_PP's protective put are the grid's only option legs that are
    not in _OPTION_STRATS; they were the ones min_volume forgot."""
    for action_type in ("sell_covered_call", "buy_protective_put"):
        cfg = mod._option_overlay_action(action_type, strike_param=5.0, strike_min=2.0,
                                         strike_max=10.0, strike_step=2.0)
        assert cfg["option_entry_cross"] == 0.0
        assert cfg["option_entry_cross_optimize"] is True


# --------------------------------------------------------------------------- #
# 2. the band
# --------------------------------------------------------------------------- #
def test_the_band_runs_from_the_mid_to_the_full_cross():
    lo, hi, step = mod._OPTION_ENTRY_CROSS_BAND
    assert (lo, hi) == (0.0, 1.0), "the band must span mid -> far touch and nothing beyond"
    assert step > 0 and (hi - lo) / step >= 2, "a band the GA cannot move is a constant"


@pytest.mark.parametrize("kind", _SINGLES)
def test_the_emitted_range_matches_the_band(kind):
    cfg = mod._option_entry_action_for(kind)
    out = {}
    _collect_action_genes("entry", "enter", 0, cfg, out)
    spec = out["entry:enter:a0:option_entry_cross"]
    lo, hi, step = mod._OPTION_ENTRY_CROSS_BAND
    assert (spec["min"], spec["max"], spec["step"]) == (lo, hi, step)
    assert spec["type"] == "float"


def test_the_authored_default_is_a_level_the_GA_can_actually_sample():
    """A default outside the sampled lattice would mean the un-searched run and the GA's
    'lowest' trial are two different configurations."""
    lo, hi, step = mod._OPTION_ENTRY_CROSS_BAND
    levels = [lo + i * step for i in range(int(round((hi - lo) / step)) + 1)]
    assert 0.0 in levels and 1.0 in levels


# --------------------------------------------------------------------------- #
# 3. every built strategy carries it -- including the group members
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", _SINGLES)
def test_every_pure_option_structure_searches_its_entry_quote(kind):
    space = collect_param_space(_build(kind))
    key = f"entry:{kind.lower()}-entry:a0:option_entry_cross"
    assert key in space, (
        f"{kind}: the entry quote is not in the emitted parameter space, so it is a constant "
        f"the GA cannot touch: {sorted(space)}")


@pytest.mark.parametrize("kind", _GROUPS)
def test_every_group_member_quotes_independently(kind):
    space = collect_param_space(_build(kind))
    for member in mod._OPTION_GROUPS[kind]:
        assert f"entry:{member.lower()}-entry:a0:option_entry_cross" in space


# --------------------------------------------------------------------------- #
# 4. a decoded value survives every hop down to the action kwarg
# --------------------------------------------------------------------------- #
def test_a_decoded_value_reaches_the_action_config():
    rules = [{"id": "enter", "actions": [mod._option_entry_action_for("O_CSP")]}]
    decoded = _decode_rule_list(
        rules, "entry", {"enter": {"a0": {"option_entry_cross": 0.75}}}, {})
    assert decoded[0]["actions"][0]["option_entry_cross"] == 0.75


def test_the_decoded_value_survives_the_rule_builder_into_the_action_kwargs():
    """End to end: gene -> rule dict -> action config key -> the ctor kwarg the entry reads."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    from ba2_common.core.rule_builders import action_from_rule

    rules = [{"id": "enter", "actions": [mod._option_entry_action_for("O_IC")]}]
    decoded = _decode_rule_list(
        rules, "entry", {"enter": {"a0": {"option_entry_cross": 0.5}}}, {})
    cfg = action_from_rule(decoded[0]["actions"][0])["act"]
    assert cfg["entry_cross"] == 0.5
    assert "entry_cross" in _OPTION_ENTRY_PARAM_KEYS


def test_the_UNdecoded_default_also_survives_the_rule_builder():
    """0.0 must reach the action as 0.0, not be dropped as falsy -- an absent kwarg and an
    explicit neutral one happen to agree today, and this is what keeps them agreeing."""
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule(mod._option_entry_action_for("O_CSP"))["act"]
    assert cfg["entry_cross"] == 0.0
