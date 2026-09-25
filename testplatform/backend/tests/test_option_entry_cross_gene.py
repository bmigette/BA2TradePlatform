"""``option_entry_cross`` must be a SEARCHED gene on every option entry, over the 0.75..1.0 band.

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

WHY THE BAND NO LONGER REACHES THE MID (plan 2026-09-24 Task 9). It was 0.0..1.0 authored at
the 0.0 no-op. Under next-open fills a passive quote fills only when the premium moved the
entry's way overnight, which selects against the days the thesis works; the O_LP diagnosis
measured 23 of 39 and 149 of 191 submitted entries expiring unfilled, and discretionary exits
concede the same fraction, so positions rode to expiry. New grid runs search 0.75..1.0, and the
authored default is the band floor -- an un-searched run is no longer the pre-F3 quote.
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
# 1. the authored default is the band floor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", _SINGLES)
def test_every_option_structure_is_authored_at_the_band_floor(kind):
    """The authored value is the band's floor, 0.75: inside the searched band (so the
    un-searched run is a configuration the GA can also sample) and, deliberately, NOT the
    pre-F3 mid quote any more -- that quote is what left entries and exits expiring unfilled."""
    from ba2_common.core.option_entry_quote import ENTRY_CROSS_NEUTRAL

    cfg = mod._option_entry_action_for(kind)
    assert cfg["option_entry_cross"] == mod._OPTION_ENTRY_CROSS_BAND[0] == 0.75
    assert cfg["option_entry_cross"] != ENTRY_CROSS_NEUTRAL


def test_the_overlay_actions_are_authored_at_the_band_floor_too():
    """O_CC's covered call and O_PP's protective put are the grid's only option legs that are
    not in _OPTION_STRATS; they were the ones min_volume forgot."""
    for action_type in ("sell_covered_call", "buy_protective_put"):
        cfg = mod._option_overlay_action(action_type, strike_param=5.0, strike_min=2.0,
                                         strike_max=10.0, strike_step=2.0)
        assert cfg["option_entry_cross"] == 0.75
        assert cfg["option_entry_cross_optimize"] is True
        assert (cfg["option_entry_cross_min"], cfg["option_entry_cross_max"],
                cfg["option_entry_cross_step"]) == mod._OPTION_ENTRY_CROSS_BAND


# --------------------------------------------------------------------------- #
# 2. the band
# --------------------------------------------------------------------------- #
def test_the_band_runs_from_three_quarters_to_the_full_cross():
    lo, hi, step = mod._OPTION_ENTRY_CROSS_BAND
    assert (lo, hi, step) == (0.75, 1.0, 0.05), (
        "Task 9: 0.75 -> far touch, nothing beyond; passive quotes went unfilled at next-open")
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
    # The GA decodes a float gene as ``round(v / step) * step`` -- a lattice anchored at ZERO,
    # not at ``min`` -- so both ends must be multiples of the step, or the floor itself (the
    # authored value) is not a level any trial can land on.
    for end in (lo, hi):
        assert round(end / step) * step == pytest.approx(end)
    levels = [round(round((lo + i * step) / step) * step, 10)
              for i in range(int(round((hi - lo) / step)) + 1)]
    assert levels == [0.75, 0.8, 0.85, 0.9, 0.95, 1.0]
    assert mod._option_entry_action_for("O_LC")["option_entry_cross"] in levels


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
        rules, "entry", {"enter": {"a0": {"option_entry_cross": 0.9}}}, {})
    assert decoded[0]["actions"][0]["option_entry_cross"] == 0.9   # != the 0.75 default


def test_the_decoded_value_survives_the_rule_builder_into_the_action_kwargs():
    """End to end: gene -> rule dict -> action config key -> the ctor kwarg the entry reads."""
    from ba2_common.core.TradeActionEvaluator import _OPTION_ENTRY_PARAM_KEYS
    from ba2_common.core.rule_builders import action_from_rule

    rules = [{"id": "enter", "actions": [mod._option_entry_action_for("O_IC")]}]
    decoded = _decode_rule_list(
        rules, "entry", {"enter": {"a0": {"option_entry_cross": 0.95}}}, {})
    cfg = action_from_rule(decoded[0]["actions"][0])["act"]
    assert cfg["entry_cross"] == 0.95
    assert "entry_cross" in _OPTION_ENTRY_PARAM_KEYS


def test_the_UNdecoded_default_also_survives_the_rule_builder():
    """The authored 0.75 must reach the action: a dropped kwarg would silently mean the 0.0
    mid quote -- exactly the unfilled-limit behaviour the 0.75..1.0 band retires."""
    from ba2_common.core.rule_builders import action_from_rule

    cfg = action_from_rule(mod._option_entry_action_for("O_CSP"))["act"]
    assert cfg["entry_cross"] == 0.75


def test_an_explicit_zero_still_survives_the_rule_builder():
    """A stored pre-Task-9 strategy can still carry 0.0; it must reach the action as 0.0, not be
    dropped as falsy -- an absent kwarg and an explicit neutral one agree, and this keeps them
    agreeing."""
    from ba2_common.core.rule_builders import action_from_rule

    action = dict(mod._option_entry_action_for("O_CSP"), option_entry_cross=0.0)
    assert action_from_rule(action)["act"]["entry_cross"] == 0.0
