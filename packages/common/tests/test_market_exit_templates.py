"""Market exit / stop / TP rule templates (plan 2026-09-24 Task B5).

``market_exit_rules`` builds up to four exit TradeRules (structure close, slope close, stop
adjustment, TP adjustment), each authored OFF behind a rule-level toggle gene. Pinned here:

* every rule is a legal market-gated exit rule (``assert_market_rule_actions``);
* NO leaf can resolve to ``off``. An exit rule whose only leaf is removed has an empty AND,
  which is always true, and would close every position;
* each rule is off by default. The all-off genome (and the no-gene genome) decodes to exactly the
  input exit rules: the no-impact control;
* the close rules stop processing and the adjustment rules continue;
* direction: the structure codes, the slope halves, and the stop/TP percent sign against
  ``TradeActions``;
* a profile that serves none of a kind's fields omits that kind;
* a decoded (enabled) instance of every rule is resolved and survives the deploy converter.

THE DECODER. The GA's collector/decoder (``strategy_param_space``) lives in the test platform,
not in ba2_common. It imports only ba2_common and the stdlib, so it is loaded here BY PATH from
the monorepo checkout. When this package is tested outside the monorepo, the tests that need it
skip, and B6's end-to-end test covers the decode. The ba2_common half of "authored off"
(``rules_convert.live_actions_from_trade_rule`` drops ``enabled: False``) is tested here without it.
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

from ba2_common.core.market_condition_rules import (
    assert_market_conditions_resolved,
    assert_market_rule_actions,
)
from ba2_common.core.market_condition_templates import (
    MARKET_EXIT_KINDS,
    _assert_no_off_leaf,
    market_exit_rules,
)
from ba2_common.core.market_conditions import (
    FIELD_ADX,
    FIELD_STRUCTURE_STATE,
    FIELD_TREND_SLOPE,
    STRUCTURE_STATE_CODES,
    field_spec,
)
from ba2_common.core.rule_models import MODE_OFF, ConditionLeaf, normalize_trade_rules
from ba2_common.core.rules_convert import live_actions_from_trade_rule, trade_rules_to_live_export

OHLCV = "ohlcv-v1"
STRUCT = "ta-structure-v1"
BOTH = [OHLCV, STRUCT]
PREFIX = "research"
IDS_BOTH = [f"{PREFIX}-mkt-exit-structure", f"{PREFIX}-mkt-exit-slope",
            f"{PREFIX}-mkt-stop", f"{PREFIX}-mkt-tp"]

_DECODER_PATH = (pathlib.Path(__file__).resolve().parents[3]
                 / "testplatform" / "backend" / "app" / "services" / "strategy_param_space.py")


@pytest.fixture(scope="module")
def sps():
    """The REAL GA collector/decoder, loaded by path (no ``app`` package on sys.path)."""
    if not _DECODER_PATH.is_file():
        pytest.skip(f"test-platform decoder not in this checkout: {_DECODER_PATH}")
    spec = importlib.util.spec_from_file_location("_b5_strategy_param_space", _DECODER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _leaves(rule):
    return rule["conditions"]["conditions"]


def _all(direction="long", profiles=BOTH, kinds=MARKET_EXIT_KINDS):
    return market_exit_rules(PREFIX, profiles, direction, kinds)


def _ordinary_exit_rules():
    """What a research job already carries: a stop-processing close and a timeout."""
    return [
        {"id": "research_bearish", "conditions": {"type": "AND", "conditions": [
            {"id": "research_bearish_flag", "field": "bearish", "op": "is_true"}]},
         "actions": [{"action_type": "close"}], "continue_processing": False},
        {"id": "research_timeout", "conditions": {"type": "AND", "conditions": [
            {"id": "research_days", "field": "days_opened", "op": ">", "value": 60}]},
         "actions": [{"action_type": "close"}], "continue_processing": False},
    ]


def _strategy(exit_rules):
    return SimpleNamespace(entry_rules=None, exit_rules=exit_rules)


def _midpoint_genome(space, enabled):
    """A genome with every collected gene at a legal value and every rule toggle at ``enabled``."""
    flat = {}
    for key, rng in space.items():
        if key.endswith(":enabled"):
            flat[key] = enabled
        else:
            steps = int(round((rng["max"] - rng["min"]) / rng["step"]))
            flat[key] = rng["min"] + rng["step"] * (steps // 2)
    return flat


# ---------------------------------------------------------------------------------------------
# Shape, legality, defaults
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["long", "short"])
def test_ids_order_and_actions(direction):
    rules = _all(direction)
    assert [r["id"] for r in rules] == IDS_BOTH
    by_id = {r["id"]: r for r in rules}
    assert by_id[IDS_BOTH[0]]["actions"] == [{"action_type": "close"}]
    assert by_id[IDS_BOTH[1]]["actions"] == [{"action_type": "close"}]
    for rid, at, band in ((IDS_BOTH[2], "adjust_stop_loss", (-2.0, 0.0, 1.0)),
                          (IDS_BOTH[3], "adjust_take_profit", (10.0, 30.0, 10.0))):
        (action,) = by_id[rid]["actions"]
        assert action["action_type"] == at
        assert action["reference_value"] == "order_open_price"
        assert action["action_value_optimize"] is True
        assert (action["action_value_min"], action["action_value_max"],
                action["action_value_step"]) == band
        assert band[0] <= action["action_value"] <= band[1]


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("profiles", [BOTH, [OHLCV], [STRUCT]])
def test_every_rule_passes_market_rule_actions(direction, profiles):
    rules = _all(direction, profiles)
    assert rules
    for rule in rules:
        assert rule["conditions"]["type"] == "AND"
        assert all("conditions" not in leaf for leaf in _leaves(rule))  # top-level AND of leaves
    assert_market_rule_actions(rules, "test")
    assert_market_rule_actions(normalize_trade_rules(rules), "test normalized")


@pytest.mark.parametrize("direction", ["long", "short"])
def test_each_rule_is_toggled_off_by_default(direction):
    for rule in _all(direction):
        assert rule["toggle_optimize"] is True
        assert rule["enabled"] is False
        # ba2_common's fail-closed half: the seeder and the live export drop it undecoded.
        assert live_actions_from_trade_rule(rule) is None
    # The markers survive the canonical normaliser (TradeRule keeps ``enabled`` as an extra).
    for rule in normalize_trade_rules(_all(direction)):
        assert rule["enabled"] is False and rule["toggle_optimize"] is True


def test_continue_processing():
    by_id = {r["id"]: r for r in _all()}
    assert by_id[f"{PREFIX}-mkt-exit-structure"]["continue_processing"] is False
    assert by_id[f"{PREFIX}-mkt-exit-slope"]["continue_processing"] is False
    assert by_id[f"{PREFIX}-mkt-stop"]["continue_processing"] is True
    assert by_id[f"{PREFIX}-mkt-tp"]["continue_processing"] is True


# ---------------------------------------------------------------------------------------------
# No leaf can be off
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["long", "short"])
def test_no_leaf_can_resolve_to_off(direction):
    for rule in _all(direction):
        assert _leaves(rule), rule["id"]
        for leaf in _leaves(rule):
            for key in ("mode_optimize", "mode_choices", "toggle_optimize"):
                assert key not in leaf, (rule["id"], key)
            assert leaf["mode"] != MODE_OFF
            assert leaf["op"] and leaf["value"] is not None
            ConditionLeaf.model_validate(leaf)  # a valid leaf of the canonical model


def test_the_builder_refuses_a_leaf_that_could_be_off():
    """The builder's own assertion, fed each way a leaf could become removable."""
    for poison in ({"mode_optimize": True, "mode_choices": ["off", "bull", "bear"]},
                   {"mode": MODE_OFF}, {"toggle_optimize": True}):
        rules = copy.deepcopy(_all())
        _leaves(rules[0])[0].update(poison)
        with pytest.raises(ValueError, match="can be switched off"):
            _assert_no_off_leaf(rules, "t")
    rules = copy.deepcopy(_all())
    rules[0]["conditions"]["conditions"] = []
    with pytest.raises(ValueError, match="always true"):
        _assert_no_off_leaf(rules, "t")


def test_a_single_value_categorical_mode_gene_is_not_expressible(sps):
    """Why structure_state is a FIXED leaf: the categorical mode gene cannot be narrowed to the
    against code. The model requires choices to start with 'off'; the collector requires exactly
    ['off', *codes]. So a restricted choice list is refused, never silently widened."""
    base = {"id": "s", "field": FIELD_STRUCTURE_STATE, "op": "==", "mode_optimize": True}
    with pytest.raises(ValueError, match="must start with"):
        ConditionLeaf.model_validate({**base, "mode_choices": ["bear"]})
    rule = {"id": "r", "conditions": {"type": "AND", "conditions": [
        {**base, "mode_choices": ["off", "bear"]}]}, "actions": [{"action_type": "close"}]}
    with pytest.raises(ValueError, match="must be exactly"):
        sps.collect_param_space(_strategy([rule]))


def test_no_mode_gene_is_collected(sps):
    """The collector emits only rule toggles, thresholds and action values: 9 genes."""
    rules = _all()
    space = sps.collect_param_space(_strategy(rules))
    assert not [k for k in space if k.endswith(":mode")]
    expected = {f"exit:{rid}:enabled" for rid in IDS_BOTH}
    expected |= {f"cond:{PREFIX}-mkt-exit-slope-slope:value",
                 f"cond:{PREFIX}-mkt-tp-slope:value", f"cond:{PREFIX}-mkt-tp-adx:value",
                 f"exit:{PREFIX}-mkt-stop:a0:action_value",
                 f"exit:{PREFIX}-mkt-tp:a0:action_value"}
    assert set(space) == expected


# ---------------------------------------------------------------------------------------------
# The no-impact control
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["long", "short"])
def test_all_off_genome_yields_exactly_the_input_exit_rules(sps, direction):
    ordinary = _ordinary_exit_rules()
    template = copy.deepcopy(ordinary) + _all(direction)
    space = sps.collect_param_space(_strategy(template))
    # Every toggle 0, every threshold/action gene somewhere mid-band: the thresholds are inert.
    decoded = sps.decode_params(_strategy(template), _midpoint_genome(space, enabled=0))
    assert decoded["exit_rules"] == ordinary
    # No gene at all (an unsearched run): authored-off still means absent.
    assert sps.decode_params(_strategy(template), {})["exit_rules"] == ordinary


# ---------------------------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("direction, against, slope_close, slope_with", [
    ("long", "bear", ("below", "<", -0.30, 0.0), ("above", ">", 0.0, 0.30)),
    ("short", "bull", ("above", ">", 0.0, 0.30), ("below", "<", -0.30, 0.0)),
])
def test_direction_codes_and_thresholds(direction, against, slope_close, slope_with):
    by_id = {r["id"]: r for r in _all(direction)}
    slope = field_spec(FIELD_TREND_SLOPE)
    adx = field_spec(FIELD_ADX)
    for rid in (f"{PREFIX}-mkt-exit-structure", f"{PREFIX}-mkt-stop"):
        (leaf,) = _leaves(by_id[rid])
        assert leaf["field"] == FIELD_STRUCTURE_STATE
        assert (leaf["op"], leaf["mode"]) == ("==", against)
        assert leaf["value"] == float(STRUCTURE_STATE_CODES[against])
        assert not leaf.get("optimize")  # no threshold gene either

    def shape(leaf):
        return (leaf["mode"], leaf["op"], leaf["value_min"], leaf["value_max"])

    (close_leaf,) = _leaves(by_id[f"{PREFIX}-mkt-exit-slope"])
    assert close_leaf["field"] == FIELD_TREND_SLOPE
    assert shape(close_leaf)[:2] == slope_close[:2]
    assert close_leaf["value_min"] == pytest.approx(slope_close[2])
    assert close_leaf["value_max"] == pytest.approx(slope_close[3])
    assert close_leaf["value"] == slope.anchor_value == 0.0
    assert close_leaf["value_step"] == slope.value_step and close_leaf["optimize"] is True

    tp_slope, tp_adx = _leaves(by_id[f"{PREFIX}-mkt-tp"])
    assert tp_slope["field"] == FIELD_TREND_SLOPE
    assert shape(tp_slope)[:2] == slope_with[:2]
    assert tp_slope["value_min"] == pytest.approx(slope_with[2])
    assert tp_slope["value_max"] == pytest.approx(slope_with[3])
    assert tp_adx["field"] == FIELD_ADX
    assert (tp_adx["mode"], tp_adx["op"]) == ("above", ">")  # ADX above for both directions
    assert (tp_adx["value_min"], tp_adx["value_max"], tp_adx["value_step"], tp_adx["value"]) == (
        adx.value_min, adx.value_max, adx.value_step, adx.anchor_value)


def test_percent_bands_are_the_same_for_both_directions():
    """The percent is direction-relative in TradeActions (see the price test below), so a short
    uses the SAME bands as a long: -2..0 for the stop, +10..+30 for the TP."""
    def actions(direction):
        return [r["actions"] for r in _all(direction)]
    assert actions("long") == actions("short")


class _Account:
    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_percent_sign_against_trade_actions(side):
    """Every value of the stop band lands at or beyond breakeven on the ADVERSE side, and every
    value of the TP band on the PROFIT side, for a long and a short. ``rule_price`` is the stop's
    percent->price before the min-distance floor (the floor is B4's, not the template's)."""
    from ba2_common.core.TradeActions import AdjustStopLossAction, AdjustTakeProfitAction
    from ba2_common.core.types import OrderRecommendation

    rec = OrderRecommendation.BUY if side == "BUY" else OrderRecommendation.SELL
    entry = 100.0
    order = SimpleNamespace(id=1, symbol="AAPL", side=side, limit_price=None, open_price=entry,
                            expert_recommendation_id=None)
    by_id = {r["id"]: r for r in _all("long" if side == "BUY" else "short")}
    (stop,) = by_id[f"{PREFIX}-mkt-stop"]["actions"]
    (tp,) = by_id[f"{PREFIX}-mkt-tp"]["actions"]

    def band(a):
        n = int(round((a["action_value_max"] - a["action_value_min"]) / a["action_value_step"]))
        return [a["action_value_min"] + i * a["action_value_step"] for i in range(n + 1)]

    for pct in band(stop):
        sl = AdjustStopLossAction("AAPL", _Account(), rec, existing_order=order,
                                  reference_value="order_open_price", percent=pct)
        sl.compute_price(order)
        adverse = entry - sl.rule_price if side == "BUY" else sl.rule_price - entry
        assert adverse == pytest.approx(-pct / 100 * entry), (side, pct)
        assert adverse >= 0
    for pct in band(tp):
        tpa = AdjustTakeProfitAction("AAPL", _Account(), rec, existing_order=order,
                                     reference_value="order_open_price", percent=pct)
        price = super(AdjustTakeProfitAction, tpa).compute_price(order)  # before the TP floor
        profit = price - entry if side == "BUY" else entry - price
        assert profit == pytest.approx(pct / 100 * entry), (side, pct)
        assert profit > 0


# ---------------------------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("profiles, ids", [
    ([OHLCV], [f"{PREFIX}-mkt-exit-slope", f"{PREFIX}-mkt-tp"]),
    ([STRUCT], [f"{PREFIX}-mkt-exit-structure", f"{PREFIX}-mkt-stop"]),
    ([STRUCT, OHLCV], IDS_BOTH),
    ([], []),
])
def test_single_profile_omits_unserved_kinds(profiles, ids):
    assert [r["id"] for r in _all(profiles=profiles)] == ids


def test_kinds_subset_and_refusals():
    assert [r["id"] for r in _all(kinds=("tp",))] == [f"{PREFIX}-mkt-tp"]
    assert [r["id"] for r in _all(kinds=("exit",))] == IDS_BOTH[:2]
    assert [r["id"] for r in _all(kinds=("stop",), profiles=[OHLCV])] == []
    with pytest.raises(ValueError, match="direction"):
        market_exit_rules(PREFIX, BOTH, "flat")
    with pytest.raises(ValueError, match="kinds"):
        market_exit_rules(PREFIX, BOTH, "long", ("exit", "trail"))
    with pytest.raises(ValueError, match="kinds"):
        market_exit_rules(PREFIX, BOTH, "long", ("tp", "tp"))
    with pytest.raises(ValueError, match="not a registered"):
        market_exit_rules(PREFIX, ["ohlcv-v9"], "long")


# ---------------------------------------------------------------------------------------------
# Resolved instances
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["long", "short"])
def test_template_leaves_are_already_resolved(direction):
    """No decoder needed: the leaves carry no mode gene, so the template itself is resolved."""
    assert_market_conditions_resolved(_all(direction), "template")


@pytest.mark.parametrize("direction", ["long", "short"])
def test_decoded_instance_of_each_rule_is_resolved_and_exportable(sps, direction):
    template = _all(direction)
    space = sps.collect_param_space(_strategy(template))
    decoded = sps.decode_params(_strategy(template), _midpoint_genome(space, enabled=1))
    rules = decoded["exit_rules"]
    assert [r["id"] for r in rules] == IDS_BOTH
    for rule in rules:
        assert "enabled" not in rule  # the GA turned it on: no stale marker
        assert_market_conditions_resolved([rule], rule["id"])
        assert_market_rule_actions([rule], rule["id"])
        for leaf in _leaves(rule):
            assert leaf["mode"] != MODE_OFF
    # The genes landed: thresholds and percents at the midpoint genome's values.
    by_id = {r["id"]: r for r in rules}
    assert by_id[f"{PREFIX}-mkt-stop"]["actions"][0]["action_value"] == pytest.approx(-1.0)
    assert by_id[f"{PREFIX}-mkt-tp"]["actions"][0]["action_value"] == pytest.approx(20.0)
    # And they reach live: one open_positions rule each, continue flags carried.
    export = trade_rules_to_live_export(exit_rules=rules)
    (ruleset,) = export["rulesets"]
    assert [r["continue_processing"] for r in ruleset["rules"]] == [False, False, True, True]
