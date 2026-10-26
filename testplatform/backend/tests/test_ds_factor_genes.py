"""goal2027 item 2.8: the DeterministicScorer factor genes (``--search-ds-factor-weights``, token ``dsfw``).

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.8.

What this pins:
* the gene space (ids, ranges, the persisted ``mom_lookback_days`` choice order, ``w_analyst`` widened to 0.8) exists
  ONLY under the flag; without it the gene space and the DS run settings are unchanged;
* zero-sum / dead sub-weights are CANONICALISED (section off, dead genes absent), never scored as a silent 0;
* the genes reach the trial config and survive the TOP-N re-run decode and the deploy export (same key names/types);
* the flag is refused for any expert but DeterministicScorer.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

from app.services.strategy_optimization_handler import _build_daily_trial_config, canonical_trial_params
from app.services.strategy_param_space import (
    DS_FACTOR_GENES, collect_param_space, decode_params, neutralise_ds_factor_genes,
)
from app.services.trial_memo import trial_key

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TESTPLATFORM)
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_dsfw", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_dsfw"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()
_STRAT = SimpleNamespace(entry_rules=None, exit_rules=None)

#: the exploration quality_momentum 70/30 recipe (tools/strategy_research/exploration/profiles.py)
_RECIPE = {"model:w_technical": 0.7, "model:w_fundamental": 0.3, "model:w_analyst": 0.0,
           "model:tw_mom": 0.7, "model:tw_d200": 0.3, "model:tw_rsi": 0.0, "model:tw_don": 0.0,
           "model:fw_quality": 0.6, "model:fw_piotroski": 0.4, "model:fw_value": 0.0, "model:fw_growth": 0.0,
           "model:mom_lookback_days": 126}


class TestNeutralise:
    def test_recipe_is_untouched_and_returns_the_same_object(self):
        assert neutralise_ds_factor_genes(_RECIPE) is _RECIPE

    def test_no_factor_gene_is_a_no_op_for_every_older_job(self):
        flat = {"model:w_technical": 0.5, "model:theta_buy": 0.3}
        assert neutralise_ds_factor_genes(flat) is flat

    def test_zero_sum_technical_switches_the_section_off(self):
        flat = {**_RECIPE, "model:tw_mom": 0.0, "model:tw_d200": 0.0}
        out = neutralise_ds_factor_genes(flat)
        assert out["model:w_technical"] == 0.0
        for k in ("model:tw_mom", "model:tw_d200", "model:tw_rsi", "model:tw_don", "model:mom_lookback_days"):
            assert k not in out
        assert out["model:fw_quality"] == 0.6          # the other section is untouched
        assert flat["model:w_technical"] == 0.7        # never mutates the input

    def test_zero_sum_fundamental_switches_the_section_off(self):
        flat = {**_RECIPE, "model:fw_quality": 0.0, "model:fw_piotroski": 0.0}
        out = neutralise_ds_factor_genes(flat)
        assert out["model:w_fundamental"] == 0.0
        assert not any(k in out for k in ("model:fw_quality", "model:fw_piotroski", "model:fw_value",
                                          "model:fw_growth"))

    def test_zero_section_weight_drops_its_sub_weights(self):
        out = neutralise_ds_factor_genes({**_RECIPE, "model:w_technical": 0.0})
        assert not any(k in out for k in ("model:tw_mom", "model:tw_d200", "model:mom_lookback_days"))
        assert out["model:w_technical"] == 0.0

    def test_zero_momentum_weight_kills_only_the_lookback(self):
        out = neutralise_ds_factor_genes({**_RECIPE, "model:tw_mom": 0.0})
        assert "model:mom_lookback_days" not in out and out["model:tw_d200"] == 0.3

    def test_analyst_detail_is_dead_without_analyst_weight(self):
        flat = {**_RECIPE, "model:analyst_target_window_days": 60, "model:analyst_min_targets": 5}
        assert "model:analyst_min_targets" not in neutralise_ds_factor_genes(flat)
        live = {**flat, "model:w_analyst": 0.5}
        assert neutralise_ds_factor_genes(live) is live

    def test_memo_key_dedups_dead_genes_but_not_live_ones(self):
        def key(flat):
            return trial_key({"engine": "daily", "seed": 1, "params": canonical_trial_params({}, flat)})
        a = {**_RECIPE, "model:w_technical": 0.0}
        b = {**a, "model:tw_mom": 0.2, "model:mom_lookback_days": 252}
        assert key(a) == key(b)
        assert key(_RECIPE) != key({**_RECIPE, "model:mom_lookback_days": 252})

    def test_all_sections_zero_is_left_to_the_trade_gate(self):
        flat = {**_RECIPE, "model:w_technical": 0.0, "model:w_fundamental": 0.0}
        out = neutralise_ds_factor_genes(flat)        # a dead genome, as today: no tw/fw left, no crash
        assert out["model:w_technical"] == 0.0 and out["model:w_fundamental"] == 0.0


class TestDecode:
    def test_decode_canonicalises_and_keeps_types(self):
        flat = {**_RECIPE, "model:tw_mom": 0.0, "model:tw_d200": 0.0}
        d = decode_params(_STRAT, flat)["expert_overrides"]
        assert d["w_technical"] == 0.0 and "tw_mom" not in d and "mom_lookback_days" not in d
        d2 = decode_params(_STRAT, _RECIPE)["expert_overrides"]
        assert d2["mom_lookback_days"] == 126 and isinstance(d2["mom_lookback_days"], int)
        assert d2["tw_mom"] == 0.7 and d2["fw_quality"] == 0.6

    def test_factor_gene_list_is_complete(self):
        assert set(DS_FACTOR_GENES) >= {f"model:{k}" for k in (
            "tw_mom", "tw_d200", "tw_rsi", "tw_don", "fw_quality", "fw_piotroski", "fw_value", "fw_growth",
            "mom_lookback_days")}


class TestTrialConfig:
    @staticmethod
    def _cfg(flat):
        bt = {"backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
              "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0, "seed": 42,
              "account_settings": {}, "experts": [{"class": "DeterministicScorer", "settings": {}}]}
        return _build_daily_trial_config(bt, decode_params(_STRAT, flat), None, option_trade_records=False)

    def test_genes_reach_the_trial_expert_settings(self):
        s = self._cfg({**_RECIPE, "model:w_analyst": 0.8})["experts"][0]["settings"]
        assert (s["tw_mom"], s["tw_d200"], s["fw_quality"], s["fw_piotroski"]) == (0.7, 0.3, 0.6, 0.4)
        assert s["mom_lookback_days"] == 126 and s["w_analyst"] == 0.8

    def test_rerun_of_a_stored_row_decodes_the_same_trial(self):
        a = self._cfg(dict(_RECIPE))
        b = self._cfg(dict(_RECIPE))
        a.pop("backtest_id")
        b.pop("backtest_id")
        assert a == b


class TestLauncherFlag:
    def teardown_method(self):
        _M._resolve_search_ds_factor_weights(False, "optimize")

    def test_gene_space_only_under_the_flag(self):
        assert _M._ds_factor_gene_space() == {}
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        sp = _M._ds_factor_gene_space()
        for k in ("tw_mom", "tw_d200", "tw_rsi", "tw_don", "fw_quality", "fw_piotroski", "fw_value", "fw_growth"):
            assert sp[k] == {"optimize": True, "min": 0.0, "max": 1.0, "step": 0.1, "type": "float"}
        assert sp["mom_lookback_days"]["choices"] == [63, 126, 252]       # persisted order
        assert sp["w_analyst"]["max"] == 0.8

    def test_existing_genes_keep_their_position_and_w_analyst_is_widened(self):
        base = _M._EXPERT_OPT["DeterministicScorer"]["expert_params"]
        assert base["w_analyst"]["max"] == 0.4                              # unchanged without the flag
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        merged = {**base, **_M._ds_factor_gene_space()}
        assert list(merged)[:len(base)] == list(base)
        collected = collect_param_space(_STRAT, expert_cfg=merged)
        assert collected["model:w_analyst"]["max"] == 0.8
        assert collected["model:mom_lookback_days"]["choices"] == [63, 126, 252]

    def test_without_the_flag_the_collected_space_is_unchanged(self):
        base = _M._EXPERT_OPT["DeterministicScorer"]["expert_params"]
        assert collect_param_space(_STRAT, expert_cfg={**base, **_M._ds_factor_gene_space()}) == \
            collect_param_space(_STRAT, expert_cfg=base)

    def test_refused_for_every_other_expert(self):
        for expert in ("FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FMPSenateTraderWeight",
                       "FactorRanker"):
            with pytest.raises(SystemExit) as e:
                _M._resolve_search_ds_factor_weights(True, "optimize", expert)
            assert "DeterministicScorer" in str(e.value)
        assert _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer") is True

    def test_global_resets_on_every_command(self):
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        _M._resolve_search_ds_factor_weights(False, "optimize-batch")
        assert _M._ds_factor_gene_space() == {}


class TestExport:
    @pytest.fixture(autouse=True, scope="class")
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _export(sp):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        bt = Backtest(name="TOP1-t", expert_name="DeterministicScorer", engine_type="daily_expert",
                      strategy_params=sp, start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1),
                      initial_capital=10_000.0, optimization_id=None)
        return _derive_export_payload(bt, "expert_settings", db=None)["settings"]["expert_params"]

    def test_recipe_genes_reach_the_live_settings_with_the_right_names_and_types(self):
        ep = self._export({**_RECIPE, "expertFixedSettings": {}})
        assert ep["tw_mom"] == 0.7 and ep["tw_d200"] == 0.3 and ep["fw_quality"] == 0.6
        assert ep["fw_piotroski"] == 0.4 and ep["mom_lookback_days"] == 126
        assert isinstance(ep["mom_lookback_days"], int) and isinstance(ep["tw_mom"], float)

    def test_zero_sum_row_deploys_the_canonical_phenotype_not_the_raw_genome(self):
        raw = {**_RECIPE, "model:tw_mom": 0.0, "model:tw_d200": 0.0}
        ep = self._export({**raw, "expertFixedSettings": {}})
        bt_side = decode_params(_STRAT, raw)["expert_overrides"]
        assert ep["w_technical"] == 0.0 == bt_side["w_technical"]

    @staticmethod
    def _opt_and_bt(strategy_params, ranges):
        from datetime import datetime
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.models.strategy import Strategy
        from app.models.strategy_optimization import StrategyOptimization
        db = SessionLocal()
        strat = Strategy(name="dsfw-strat", entry_rules=[], exit_rules=[])
        db.add(strat)
        db.commit()
        db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="dsfw-opt", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {"backtest_id": 1, "enabled_instruments": ["AAPL"],
                                              "experts": [{"class": "DeterministicScorer", "settings": {}}],
                                              "account_settings": {}, "warmup_days": 30, "seed": 42}},
            parameter_ranges=ranges, all_results=[], best_params={}, best_fitness=1.0, status="completed")
        db.add(opt)
        db.commit()
        db.refresh(opt)
        bt = Backtest(name="TOP1-dsfw", expert_name="DeterministicScorer", engine_type="daily_expert",
                      strategy_params=strategy_params, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=opt.id)
        return db, bt

    def test_gene_run_row_that_lost_its_genes_is_refused(self):
        from fastapi import HTTPException
        from app.api.backtests import _derive_export_payload
        db, bt = self._opt_and_bt({"model:theta_buy": 0.3}, {"model:tw_mom": {"type": "float"}})
        try:
            with pytest.raises(HTTPException) as e:
                _derive_export_payload(bt, "expert_settings", db)
            assert "factor" in str(e.value.detail)
        finally:
            db.close()

    def test_older_run_row_is_unaffected(self):
        from app.api.backtests import _derive_export_payload
        db, bt = self._opt_and_bt({"model:theta_buy": 0.3, "expertFixedSettings": {}},
                                  {"model:theta_buy": {"type": "float"}})
        try:
            assert _derive_export_payload(bt, "expert_settings", db)["settings"]["expert_params"]["theta_buy"] == 0.3
        finally:
            db.close()


# ----------------------------------------------------------------------------- capband driver
_DRIVER = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("capband_dsfw", _DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDriver:
    def test_forwarded_to_deterministic_scorer_jobs_only(self, driver):
        on = SimpleNamespace(search_ds_factor_weights=True)
        off = SimpleNamespace(search_ds_factor_weights=False)
        assert driver.ds_factor_gene_tokens(on, "DeterministicScorer") == ["--search-ds-factor-weights"]
        for e in ("FMPRating", "FMPEarningsDrift", "FactorRanker"):
            assert driver.ds_factor_gene_tokens(on, e) == []
        assert driver.ds_factor_gene_tokens(off, "DeterministicScorer") == []

    @staticmethod
    def _run(*argv):
        import subprocess
        return subprocess.run([sys.executable, _DRIVER, *argv, "--dry-run"], capture_output=True, text=True)

    def test_requires_the_dsfw_name_token(self):
        r = self._run("--search-ds-factor-weights", "--name-suffix", "plain")
        assert r.returncode != 0 and "dsfw" in r.stderr


# ----------------------------------------------------------------------------- analyst legs (aw_targets / aw_grades)
_ANALYST = {**_RECIPE, "model:w_analyst": 0.8, "model:aw_targets": 1.0, "model:aw_grades": 0.0,
            "model:analyst_target_window_days": 60, "model:analyst_min_targets": 5}


class TestAnalystLegs:
    def test_analyst_targets_recipe_is_untouched(self):
        assert neutralise_ds_factor_genes(_ANALYST) is _ANALYST

    def test_both_legs_zero_switches_the_analyst_section_off(self):
        out = neutralise_ds_factor_genes({**_ANALYST, "model:aw_targets": 0.0})
        assert out["model:w_analyst"] == 0.0
        assert not any(k in out for k in ("model:aw_targets", "model:aw_grades",
                                          "model:analyst_target_window_days", "model:analyst_min_targets"))

    def test_zero_analyst_weight_removes_legs_and_details(self):
        out = neutralise_ds_factor_genes({**_ANALYST, "model:w_analyst": 0.0})
        assert not any(k in out for k in ("model:aw_targets", "model:aw_grades", "model:analyst_min_targets"))

    def test_zero_targets_leg_kills_only_the_target_details(self):
        out = neutralise_ds_factor_genes({**_ANALYST, "model:aw_targets": 0.0, "model:aw_grades": 0.5})
        assert out["model:w_analyst"] == 0.8 and out["model:aw_grades"] == 0.5
        assert "model:analyst_target_window_days" not in out and "model:analyst_min_targets" not in out

    def test_gene_space_and_trial_config(self):
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        try:
            sp = _M._ds_factor_gene_space()
        finally:
            _M._resolve_search_ds_factor_weights(False, "optimize")
        for k in ("aw_targets", "aw_grades"):
            assert sp[k] == {"optimize": True, "min": 0.0, "max": 1.0, "step": 0.1, "type": "float"}
        s = TestTrialConfig._cfg(_ANALYST)["experts"][0]["settings"]
        assert (s["aw_targets"], s["aw_grades"], s["w_analyst"]) == (1.0, 0.0, 0.8)
        assert s["analyst_target_window_days"] == 60 and s["analyst_min_targets"] == 5

    def test_export_carries_the_canonical_phenotype(self):
        ex = TestExport._export
        ep = ex({**_ANALYST, "expertFixedSettings": {}})
        assert ep["aw_targets"] == 1.0 and ep["aw_grades"] == 0.0 and ep["w_analyst"] == 0.8
        raw = {**_ANALYST, "model:aw_targets": 0.0}
        ep = ex({**raw, "expertFixedSettings": {}})
        assert ep["w_analyst"] == 0.0 == decode_params(_STRAT, raw)["expert_overrides"]["w_analyst"]

    def test_memo_key_dedups_dead_legs(self):
        def key(flat):
            return trial_key({"engine": "daily", "seed": 1, "params": canonical_trial_params({}, flat)})
        a = {**_ANALYST, "model:w_analyst": 0.0}
        assert key(a) == key({**a, "model:aw_targets": 0.3})


# ----------------------------------------------------------------------------- confidence ceiling under dsfw
class TestConfidenceCeiling:
    def teardown_method(self):
        _M._resolve_search_ds_factor_weights(False, "optimize")

    def test_the_dsfw_ceiling_is_the_theoretical_bound_recomputed_from_the_scorer(self):
        import math
        from ba2_experts.DeterministicScorer import combine
        bound = 100.0 * math.tanh(1.0 / combine.DEF_K_COMPRESS)
        assert 93.0 <= bound < 94.0 and _M._DS_FACTOR_CONFIDENCE_CEILING == math.floor(bound)
        # the bound really holds: even with every section at +1 and no regime damping
        top = combine.final_score(1.0, 1.0, 1.0, None, {"w_technical": 1, "w_fundamental": 1, "w_analyst": 1,
                                                          "w_earnings": 1, "macro_mode": "off"},
                                  earnings=1.0)["final"]
        assert 100.0 * top <= bound + 1e-9 and 100.0 * top > 90.0

    def test_flag_off_keeps_50_and_flag_on_lifts_it(self):
        assert _M._confidence_ceiling_for("DeterministicScorer") == 50.0
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        assert _M._confidence_ceiling_for("DeterministicScorer") == 93.0
        assert _M._confidence_ceiling_for("FMPRating") is None          # other experts unaffected

    def test_the_clamp_follows_the_ceiling(self):
        def top_gate(flag):
            _M._resolve_search_ds_factor_weights(flag, "optimize", "DeterministicScorer")
            strat = _M._build_strategy("S2", "n", "DeterministicScorer")
            return collect_param_space(strat)["cond:gate_confidence:value"]["max"]
        assert top_gate(False) == 50.0
        assert top_gate(True) > 50.0                  # S2's own range (40..80) is no longer cut at 50
