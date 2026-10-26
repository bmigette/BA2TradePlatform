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

import copy
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
        raw = [(a.get("action_type") or a.get("action"), a.get("reference_value"),
                None if a.get("action_value") is None else float(a["action_value"]))
               for a in r.get("actions") or []]
        # the bracket legs after the open action are independent; the order S6 uses (TP, stop) and the exploration's
        # (stop, TP) are the same behaviour
        acts = tuple(raw[:1]) + tuple(sorted(raw[1:], key=str))
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
        # a1 = take-profit, a2 = stop: the roles and indices S6 uses
        assert sp["entry:buy:a1:action_value"] == {"type": "float", "min": 10.0, "max": 30.0, "step": 5.0}
        assert sp["entry:buy:a2:action_value"] == {"type": "float", "min": -14.0, "max": -6.0, "step": 2.0}
        assert sp["cond:xt:value"] == {"type": "float", "min": 20.0, "max": 120.0, "step": 10.0}
        # no on/off toggle anywhere: the recipe IS "buy with a stop, a target and a time exit"
        assert not [k for k in sp if k.endswith(":enabled")]
        # the same names S6 uses for the same roles
        s6 = collect_param_space(_M._build_strategy("S6", "s6", "DeterministicScorer"))
        assert {"entry:buy:a1:action_value", "entry:buy:a2:action_value", "cond:xt:value"} <= set(s6)
        for sqm_strat in (strat, _M._build_strategy("S6", "s6", "DeterministicScorer")):       # same roles in both
            kinds = [a["action_type"] for a in sqm_strat.entry_rules[0]["actions"]]
            assert kinds[:3] == ["buy", "adjust_take_profit", "adjust_stop_loss"]

    def test_defaults_are_the_recipe_values(self):
        _, dec = _sqm()
        (entry,), (exit_,) = dec["entry_rules"], dec["exit_rules"]
        acts = {a["action_type"]: a for a in entry["actions"]}
        assert acts["adjust_stop_loss"]["action_value"] == -12.0
        assert acts["adjust_take_profit"]["action_value"] == 20.0
        assert [l for l in _leaves(exit_["conditions"]) if l[0] == "days_opened"][0][2] == 30.0

    def test_genes_move_the_decoded_ruleset(self):
        _, dec = _sqm({"entry:buy:a2:action_value": -8.0, "entry:buy:a1:action_value": 30.0,
                       "cond:xt:value": 90.0})
        sig = _signature(dec["entry_rules"], dec["exit_rules"])
        assert ("adjust_stop_loss", "order_open_price", -8.0) in sig[0][0][1]
        assert ("adjust_take_profit", "order_open_price", 30.0) in sig[0][0][1]
        assert ("days_opened", ">", 90.0) in sig[1][0][0]


# ----------------------------------------------------------------------------- default == exploration recipe
class TestDefaultsAreTheExplorationRecipe:
    """SQM's defaults against the exploration run that found the recipe: the ``quality_momentum`` family's 70/30 variant
    (``profiles.variants``). Its baseline states stop -8 / target +20 / time exit 60; the variant turns the stop into a
    -12..-8 range and the time exit into a 30..90 range, and the best grid point (fitness 3.16) was stop -12, 30 days.
    Nothing is overwritten here: the best point is SELECTED from the variant's own ranges, and the test fails if the
    point is outside them or the baseline's stated values are not what the recipe says they are."""
    BEST = {"stop": -12.0, "target": 20.0, "days": 30.0}

    @staticmethod
    def _variant():
        prof = _exploration()
        base = prof.new_idea_baseline("quality_momentum", prof.load_baselines()["large_ds"])
        job = next(v for v in prof.variants("quality_momentum", base) if v["variant"] == "blend_70_30")
        return base, job

    def test_the_baseline_states_the_values_the_variant_then_searches(self):
        base, job = self._variant()
        acts = {a["action_type"]: a for a in base["strategy"]["entry_rules"][0]["actions"]}
        assert acts["adjust_stop_loss"]["action_value"] == -8.0 and acts["adjust_take_profit"]["action_value"] == 20.0
        base_days = [n["value"] for r in base["strategy"]["exit_rules"] for n in r["conditions"]["conditions"]
                     if n["field"] == "days_opened"]
        assert base_days == [60]
        stop = next(a for a in job["strategy"]["entry_rules"][0]["actions"] if a["action_type"] == "adjust_stop_loss")
        assert (stop["action_value_min"], stop["action_value_max"]) == (-12.0, -8.0)
        days = next(n for r in job["strategy"]["exit_rules"] for n in r["conditions"]["conditions"]
                    if n["field"] == "days_opened")
        assert (days["value_min"], days["value_max"]) == (30, 90)

    @classmethod
    def _recipe_rules(cls):
        """The variant's rules with the best grid point selected from its own search ranges (asserted)."""
        _, job = cls._variant()
        entry = copy.deepcopy(job["strategy"]["entry_rules"])
        exit_rules = copy.deepcopy(job["strategy"]["exit_rules"])
        for a in entry[0]["actions"]:
            if a["action_type"] == "adjust_stop_loss":
                assert a["action_value_min"] <= cls.BEST["stop"] <= a["action_value_max"]
                a["action_value"] = cls.BEST["stop"]
            if a["action_type"] == "adjust_take_profit":
                assert a["action_value"] == cls.BEST["target"]
        for r in exit_rules:
            for n in r["conditions"]["conditions"]:
                if n["field"] == "days_opened":
                    assert n["value_min"] <= cls.BEST["days"] <= n["value_max"]
                    n["value"] = cls.BEST["days"]
        return entry, exit_rules

    def test_the_default_genome_equals_the_recipe_on_the_live_export(self):
        _, dec = _sqm()
        r_entry, r_exit = self._recipe_rules()
        mine = _signature(dec["entry_rules"], dec["exit_rules"])
        theirs = _signature(r_entry, r_exit)
        assert mine == theirs, f"SQM: {mine} / recipe: {theirs}"

    def test_equal_after_the_deploy_conversion_too(self):
        _, dec = _sqm()
        r_entry, r_exit = self._recipe_rules()

        def live(entry, exit_):
            lx = trade_rules_to_live_export(entry, exit_, name="x")
            back = live_export_to_trade_rules(lx)
            return _signature(back["entry_rules"], back["exit_rules"])
        assert live(dec["entry_rules"], dec["exit_rules"]) == live(r_entry, r_exit)

    def test_the_variants_other_grid_points_are_reachable_by_the_sqm_genes(self):
        # stop -12..-8 and time 30..90 (the variant's ranges) must lie inside SQM's gene ranges
        strat, _ = _sqm()
        sp = collect_param_space(strat)
        assert sp["entry:buy:a2:action_value"]["min"] <= -12.0 and sp["entry:buy:a2:action_value"]["max"] >= -8.0
        assert sp["cond:xt:value"]["min"] <= 30 and sp["cond:xt:value"]["max"] >= 90


# ----------------------------------------------------------------------------- export / deploy parity
class TestExportDeployParity:
    GENOME = {"entry:buy:a2:action_value": -10.0, "entry:buy:a1:action_value": 25.0, "cond:xt:value": 70.0}

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
        # SQM is not one of S1-S7: a market profile on it would label the job gated, demand snapshot coverage and
        # search no market genes. The launch gate must actually EXIT with that message.
        try:
            _M._resolve_market_condition_profiles("ohlcv-v1", "optimize")
            with pytest.raises(SystemExit) as e:
                _M._apply_market_conditions("optimize", {}, None, "SQM")
            msg = str(e.value)
            assert "--market-condition-profile" in msg and "'SQM'" in msg and "emits no market-condition gates" in msg
            # and the same call is accepted for an S1-S7 key (it must get past the kind check)
            try:
                _M._apply_market_conditions("optimize", {}, None, "S2")
            except SystemExit as other:
                assert "emits no market-condition gates" not in str(other)
            except Exception:       # a later check (manifest / coverage) may complain; not the kind refusal
                pass
        finally:
            _M._resolve_market_condition_profiles(None, "optimize")

    def test_the_market_profile_gene_and_market_exits_are_refused_for_sqm(self):
        _M._SEARCH_MARKET_PROFILE = True
        try:
            with pytest.raises(SystemExit) as e:
                _M._refuse_market_profile_gene_unsupported("optimize", "SQM", bypass=False)
            assert "SQM" in str(e.value) and "equity S1-S7" in str(e.value)
        finally:
            _M._SEARCH_MARKET_PROFILE = False
        _M._MARKET_EXIT_KINDS_SELECTED = ("structure",)
        try:
            with pytest.raises(SystemExit) as e:
                _M._apply_market_conditions("optimize", {}, None, "SQM")
            assert "--market-exit is equity-only" in str(e.value) or "emits no market-condition" in str(e.value)
        finally:
            _M._MARKET_EXIT_KINDS_SELECTED = ()

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
