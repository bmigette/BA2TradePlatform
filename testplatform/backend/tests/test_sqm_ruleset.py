"""goal2027 item 2.8: the ``SQM`` (quality-momentum) ruleset for DeterministicScorer jobs, and the ``xl`` screen band.

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.8.

What this pins:
* the S1-S7-shaped gene names and the ranges (stop -14..-6 step 2 / target 10..30 step 5 / time exit 20..120 step 10);
* with every gene at its default the ruleset IS the exploration ``quality_momentum`` recipe (stop -12%, target +20%,
  30-day time exit), compared on the LIVE export both sides produce;
* export -> deploy parity: the decoded rules survive ``trade_rules_to_live_export`` -> ``live_export_to_trade_rules``
  (the conversion ``tools/import_deploy_payload.py`` writes the live ruleset with) unchanged;
* SQM is refused for any other expert and for the market-condition flags (its gates are S1-S7 only);
* the ``xl`` band documented in the design doc carries the keys of the exploration ``new_idea_baseline`` screen.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

import pytest

from app.services.strategy_param_space import collect_param_space, decode_params
from ba2_common.core.rules_convert import live_export_to_trade_rules, trade_rules_to_live_export

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TESTPLATFORM)
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_sqm", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_sqm"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def _exploration():
    path = os.path.join(_REPO, "tools", "strategy_research", "exploration", "profiles.py")
    spec = importlib.util.spec_from_file_location("exploration_profiles_sqm", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["exploration_profiles_sqm"] = m
    spec.loader.exec_module(m)
    return m


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("field"):
            out.append((node["field"], node.get("op") or node.get("comparison"), node.get("value")))
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


def _signature(entry_rules, exit_rules):
    """Behaviour of a ruleset with ids/names/optimizer metadata stripped, in precedence order."""
    def rule(r):
        acts = tuple((a.get("action_type") or a.get("action"), a.get("reference_value"),
                      None if a.get("action_value") is None else float(a["action_value"]))
                     for a in r.get("actions") or [])
        leaves = tuple(sorted((f, ("is_true" if op in (None, "is_true") else op),
                               None if v is None else float(v)) for f, op, v in _leaves(r.get("conditions"))))
        return leaves, acts, bool(r.get("continue_processing"))
    return [rule(r) for r in entry_rules], [rule(r) for r in exit_rules]


def _sqm(genome=None):
    strat = _M._build_strategy("SQM", "sqm", "DeterministicScorer")
    dec = decode_params(strat, genome or {})
    return strat, dec


# ----------------------------------------------------------------------------- genes
class TestGenes:
    def test_gene_names_follow_the_s1_s7_shape_and_ranges(self):
        strat, _ = _sqm()
        sp = collect_param_space(strat)
        assert sp["entry:buy:a1:action_value"] == {"type": "float", "min": -14.0, "max": -6.0, "step": 2.0}
        assert sp["entry:buy:a2:action_value"] == {"type": "float", "min": 10.0, "max": 30.0, "step": 5.0}
        assert sp["cond:xt:value"] == {"type": "float", "min": 20.0, "max": 120.0, "step": 10.0}
        # no on/off toggle anywhere: the recipe IS "buy with a stop, a target and a time exit"
        assert not [k for k in sp if k.endswith(":enabled")]
        # the same names S6 uses for the same roles
        s6 = collect_param_space(_M._build_strategy("S6", "s6", "DeterministicScorer"))
        assert {"entry:buy:a1:action_value", "entry:buy:a2:action_value", "cond:xt:value"} <= set(s6)

    def test_defaults_are_the_recipe_values(self):
        _, dec = _sqm()
        (entry,), (exit_,) = dec["entry_rules"], dec["exit_rules"]
        acts = {a["action_type"]: a for a in entry["actions"]}
        assert acts["adjust_stop_loss"]["action_value"] == -12.0
        assert acts["adjust_take_profit"]["action_value"] == 20.0
        assert [l for l in _leaves(exit_["conditions"]) if l[0] == "days_opened"][0][2] == 30.0

    def test_genes_move_the_decoded_ruleset(self):
        _, dec = _sqm({"entry:buy:a1:action_value": -8.0, "entry:buy:a2:action_value": 30.0,
                       "cond:xt:value": 90.0})
        sig = _signature(dec["entry_rules"], dec["exit_rules"])
        assert ("adjust_stop_loss", "order_open_price", -8.0) in sig[0][0][1]
        assert ("adjust_take_profit", "order_open_price", 30.0) in sig[0][0][1]
        assert ("days_opened", ">", 90.0) in sig[1][0][0]


# ----------------------------------------------------------------------------- default == exploration recipe
class TestDefaultsAreTheExplorationRecipe:
    @staticmethod
    def _recipe_rules():
        prof = _exploration()
        base = prof.new_idea_baseline("quality_momentum", prof.load_baselines()["large_ds"])
        entry = [dict(r) for r in base["strategy"]["entry_rules"]]
        # the recipe's winning values: entry stop -12 (the baseline's -8 is the grid's other arm), target +20
        for rule in entry:
            for a in rule["actions"]:
                if a["action_type"] == "adjust_stop_loss":
                    a["action_value"] = -12.0
        exit_rules = [dict(r) for r in base["strategy"]["exit_rules"]]
        for rule in exit_rules:
            for leaf in rule["conditions"]["conditions"]:
                if leaf["field"] == "days_opened":
                    leaf["value"] = 30
        return entry, exit_rules

    def test_the_default_genome_equals_the_recipe_on_the_live_export(self):
        _, dec = _sqm()
        r_entry, r_exit = self._recipe_rules()
        mine = _signature(dec["entry_rules"], dec["exit_rules"])
        theirs = _signature(r_entry, r_exit)
        assert mine == theirs, f"\nSQM:    {mine}\nrecipe: {theirs}"

    def test_equal_after_the_deploy_conversion_too(self):
        _, dec = _sqm()
        r_entry, r_exit = self._recipe_rules()

        def live(entry, exit_):
            lx = trade_rules_to_live_export(entry, exit_, name="x")
            back = live_export_to_trade_rules(lx)
            return _signature(back["entry_rules"], back["exit_rules"])
        assert live(dec["entry_rules"], dec["exit_rules"]) == live(r_entry, r_exit)


# ----------------------------------------------------------------------------- export / deploy parity
class TestExportDeployParity:
    GENOME = {"entry:buy:a1:action_value": -10.0, "entry:buy:a2:action_value": 25.0, "cond:xt:value": 70.0}

    def test_decoded_rules_survive_the_live_export_round_trip(self):
        _, dec = _sqm(self.GENOME)
        lx = trade_rules_to_live_export(dec["entry_rules"], dec["exit_rules"], name="sqm")
        subtypes = sorted(r["subtype"] for r in lx["rulesets"])
        assert len(subtypes) == 2
        back = live_export_to_trade_rules(lx)
        assert _signature(back["entry_rules"], back["exit_rules"]) == _signature(
            dec["entry_rules"], dec["exit_rules"])

    def test_a_stored_row_exports_the_ruleset_the_backtest_ran(self):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        _, dec = _sqm(self.GENOME)
        bt = Backtest(name="TOP1-sqm", expert_name="DeterministicScorer", engine_type="daily_expert",
                      strategy_params={**self.GENOME, "entryRules": dec["entry_rules"],
                                       "exitRules": dec["exit_rules"]}, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=None)
        out = _derive_export_payload(bt, "ruleset", db=None)
        assert _signature(out["entry_rules"], out["exit_rules"]) == _signature(
            dec["entry_rules"], dec["exit_rules"])
        # and the deploy conversion of the exported rules is the live ruleset of those values
        lx = trade_rules_to_live_export(out["entry_rules"], out["exit_rules"], name="sqm")
        back = live_export_to_trade_rules(lx)
        sig = _signature(back["entry_rules"], back["exit_rules"])
        assert ("adjust_stop_loss", "order_open_price", -10.0) in sig[0][0][1]
        assert ("adjust_take_profit", "order_open_price", 25.0) in sig[0][0][1]
        assert ("days_opened", ">", 70.0) in sig[1][0][0]

    def test_exit_precedence_is_a_single_close_rule(self):
        _, dec = _sqm()
        assert len(dec["exit_rules"]) == 1
        assert [a["action_type"] for a in dec["exit_rules"][0]["actions"]] == ["close"]
        assert len(dec["entry_rules"]) == 1
        assert [a["action_type"] for a in dec["entry_rules"][0]["actions"]][0] == "buy"


# ----------------------------------------------------------------------------- refusals
class TestRefusals:
    def test_registered_and_ds_only(self):
        assert "SQM" in _M._STRATEGY_BUILDERS
        _M._refuse_ds_only_strategy("optimize", "SQM", "DeterministicScorer")           # no raise
        _M._refuse_ds_only_strategy("optimize", "S2", "FMPRating")                      # unrestricted kinds pass
        for expert in ("FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FMPSenateTraderWeight"):
            with pytest.raises(SystemExit) as e:
                _M._refuse_ds_only_strategy("optimize", "SQM", expert)
            assert "DeterministicScorer" in str(e.value)

    def test_market_condition_gates_are_refused_loudly(self):
        # SQM is not one of S1-S7; a market profile on it would label the job gated and gate nothing.
        assert "SQM" not in _M._EQUITY_MARKET_CONDITION_STRATEGIES
        assert "SQM" not in _M._MARKET_CONDITION_STRATEGIES

    def test_the_capband_driver_knows_the_strategy(self):
        spec = importlib.util.spec_from_file_location(
            "capband_sqm", os.path.join(_REPO, "tools", "run_screener_capband_matrix.py"))
        drv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drv)
        assert "SQM" in drv._KNOWN_STRATEGIES


# ----------------------------------------------------------------------------- the xl band (design doc)
class TestXlBand:
    DOC = os.path.join(_REPO, "docs", "plans", "2026-10-25-goal2027-grid-design.md")

    @staticmethod
    def _doc_block():
        text = open(TestXlBand.DOC, encoding="utf-8").read()
        block = re.search(r"```xl-screen\n(.*?)```", text, re.S)
        assert block, "the design doc must carry the xl screen in an ```xl-screen block"
        out = {}
        for k, v in re.findall(r"^(\w+):\s*(\S+)\s*$", block.group(1), re.M):
            out[k] = (v == "True") if v in ("True", "False") else float(v)
        return out

    def test_the_documented_block_is_the_launcher_preset(self):
        assert self._doc_block() == {k: (v if isinstance(v, bool) else float(v))
                                     for k, v in _M._SCREEN_PRESETS["xl"].items()}

    def test_the_preset_is_the_exploration_screen_apart_from_the_dollar_floor(self):
        prof = _exploration()
        base = prof.new_idea_baseline("quality_momentum", prof.load_baselines()["large_ds"])
        screen = base["backtest"]["screener_opt"]["base_settings"]
        preset = _M._SCREEN_PRESETS["xl"]
        # the exploration baseline also states the bounds the launcher's built-in base states anyway
        from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES
        assert set(preset) <= set(screen) and set(screen) - set(preset) <= set(SCREENER_OFF_VALUES)
        for k in set(screen) - set(preset):
            assert screen[k] == SCREENER_OFF_VALUES[k], k
        for k, v in preset.items():
            if k == "dollar_volume_min":
                # live has no dollar-volume stage; the live simulation refuses a positive floor (see the doc)
                assert v == 0 and screen[k] == 10000000.0
            else:
                assert (bool(v) == bool(screen[k])) if isinstance(v, bool) else float(v) == float(screen[k]), k
