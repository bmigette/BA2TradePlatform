"""``--pin-ds-score-weights`` (name token ``dspin``): the DeterministicScorer score coefficients are FIXED SETTINGS, not
genes. The alternative to ``--search-ds-factor-weights`` (``dsfw``), mutually exclusive with it.

Why (owner decision 2026-11-14): a 470k-observation signal study (2020-2025, large caps) found that the GA-chosen DS
weights have no out-of-sample predictive power in 2024-2025; only momentum, the 200-day trend and Piotroski held up.
The pin is equal thirds of those three plus a small, grades-dominant analyst contribution. The GA then tunes only the
thresholds, exits, sizing and the other non-score genes.

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.8, "Pinned score weights".

What this pins:
* the pinned dict and what DS computes from it (effective weights measured on the real section functions);
* the pinned settings reach the run settings, the GA trial config, the TOP-N / walk-forward re-run (stored block) and
  the deploy export; the pinned genes are absent from the search space;
* the flag is refused together with ``--search-ds-factor-weights`` and for any expert but DeterministicScorer;
* the capband driver forwards it to DS jobs only, requires ``dspin``, and leaves every other job name unchanged.
"""
from __future__ import annotations

import importlib.util
import math
import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.services.strategy_optimization_handler import _build_daily_trial_config, job_param_space
from app.services.strategy_param_space import collect_param_space, decode_params

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TESTPLATFORM)
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")
_DS = "DeterministicScorer"


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_dspin", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_dspin"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()
_STRAT = SimpleNamespace(entry_rules=None, exit_rules=None)

#: The owner-approved intent, mapped onto the DS settings (see the design doc for the reasoning per value).
_EXPECTED_PIN = {
    "w_technical": 0.6, "w_fundamental": 0.3, "w_analyst": 0.1, "w_earnings": 0.0,
    "tw_mom": 0.5, "tw_d200": 0.5, "tw_rsi": 0.0, "tw_don": 0.0,
    "fw_piotroski": 1.0, "fw_quality": 0.0, "fw_value": 0.0, "fw_growth": 0.0,
    "aw_grades": 0.75, "aw_targets": 0.25,
    "w_macro": 0.2, "mw_trend_index": 0.43, "mw_vix": 0.21, "mw_credit": 0.14, "mw_yield_curve": 0.14,
    "mw_sahm": 0.08,
}


@pytest.fixture(autouse=True)
def _reset_flags():
    yield
    _M._resolve_search_ds_factor_weights(False, "optimize")
    _M._resolve_pin_ds_score_weights(False, "optimize")


def _pin():
    _M._resolve_search_ds_factor_weights(False, "optimize", _DS)
    assert _M._resolve_pin_ds_score_weights(True, "optimize", _DS) is True


def _pinned_spec():
    _pin()
    return _M._apply_ds_score_pin(_M._EXPERT_OPT[_DS], _DS, "optimize")


# ----------------------------------------------------------------------------- the pinned values
class TestPinnedValues:
    def test_the_pinned_dict(self):
        assert _M._DS_PINNED_SCORE_WEIGHTS == _EXPECTED_PIN

    def test_every_pinned_key_is_a_real_ds_setting(self):
        from ba2_experts.DeterministicScorer import DeterministicScorer
        defs = DeterministicScorer.get_settings_definitions()
        for k in _M._DS_PINNED_SCORE_WEIGHTS:
            assert k in defs, k
            assert defs[k]["type"] == "float", k

    def test_macro_weights_are_the_settings_defaults(self):
        from ba2_experts.DeterministicScorer import DeterministicScorer
        defs = DeterministicScorer.get_settings_definitions()
        for k in ("w_macro", "mw_trend_index", "mw_vix", "mw_credit", "mw_yield_curve", "mw_sahm"):
            assert _M._DS_PINNED_SCORE_WEIGHTS[k] == defs[k]["default"], k

    def test_section_weights_give_equal_thirds_plus_a_tenth_for_the_analyst(self):
        from ba2_experts.DeterministicScorer import combine
        p = _M._DS_PINNED_SCORE_WEIGHTS
        s = {**p, "macro_mode": "off"}
        # all four sections present: the weighted average DS computes, before tanh
        r = combine.final_score(0.8, -0.4, 0.5, None, s, earnings=1.0)
        assert r["raw"] == pytest.approx(0.6 * 0.8 + 0.3 * -0.4 + 0.1 * 0.5)       # earnings weight 0: never read
        assert set(r["components"]) == {"technical", "fundamental", "analyst"}
        # the analyst section missing (sparse history): technical 2/3, fundamental 1/3
        r = combine.final_score(0.8, -0.4, None, None, s)
        assert r["raw"] == pytest.approx((2 * 0.8 - 0.4) / 3)
        assert r["final"] == pytest.approx(math.tanh(r["raw"] / combine.DEF_K_COMPRESS))

    def test_technical_section_is_half_momentum_half_trend(self):
        from ba2_experts.DeterministicScorer.technical import technical_score
        rng = np.random.default_rng(7)
        close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, 400)))
        df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                           "Volume": 1e6}, index=pd.bdate_range("2022-01-03", periods=400))
        for adx_gate in (0.0, 1e9):                  # trending AND not trending (the RSI boost multiplies 0)
            t = technical_score(df, {**_M._DS_PINNED_SCORE_WEIGHTS, "adx_gate": adx_gate})
            c = t["components"]
            assert c["rsi_meanrev"]["weight"] == 0.0 and c["donchian_breakout"]["weight"] == 0.0
            mom, d200 = c["momentum_vol_adj"]["normalized"], c["dist_sma_trend"]["normalized"]
            assert t["score"] == pytest.approx((mom + d200) / 2)

    def test_fundamental_section_is_piotroski_only(self):
        from ba2_experts.DeterministicScorer.fundamental import fundamental_score
        snap = {"fscore": 8, "quality_norm": -0.9, "value_norm": -0.9, "rev_accel": -0.5, "eps_accel": -0.5}
        f = fundamental_score(snap, _M._DS_PINNED_SCORE_WEIGHTS)
        assert f["score"] == pytest.approx((8 - 4.5) / 4.5)

    def test_analyst_section_is_three_quarters_grades(self):
        p = _M._DS_PINNED_SCORE_WEIGHTS
        assert p["aw_grades"] / (p["aw_grades"] + p["aw_targets"]) == pytest.approx(0.75)
        assert p["w_analyst"] / (p["w_technical"] + p["w_fundamental"] + p["w_analyst"]) == pytest.approx(0.1)


# ----------------------------------------------------------------------------- launcher flag + gene space
class TestLauncherFlag:
    def test_off_by_default_and_the_spec_is_untouched(self):
        assert _M._resolve_pin_ds_score_weights(False, "optimize", _DS) is False
        spec = _M._EXPERT_OPT[_DS]
        assert _M._apply_ds_score_pin(spec, _DS, "optimize") is spec

    def test_refused_together_with_the_search_flag(self):
        _M._resolve_search_ds_factor_weights(True, "optimize", _DS)
        with pytest.raises(SystemExit) as e:
            _M._resolve_pin_ds_score_weights(True, "optimize", _DS)
        assert "--search-ds-factor-weights" in str(e.value) and "--pin-ds-score-weights" in str(e.value)

    def test_refused_for_every_other_expert(self):
        for expert in ("FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy", "FMPSenateTraderWeight",
                       "FactorRanker", "PullbackReversion"):
            with pytest.raises(SystemExit) as e:
                _M._resolve_pin_ds_score_weights(True, "optimize", expert)
            assert _DS in str(e.value)

    def test_global_resets_on_every_command(self):
        _pin()
        _M._resolve_pin_ds_score_weights(False, "optimize-batch")
        spec = _M._EXPERT_OPT[_DS]
        assert _M._apply_ds_score_pin(spec, _DS, "optimize") is spec

    def test_pinned_genes_are_absent_from_the_search_space(self):
        spec = _pinned_spec()
        base = _M._EXPERT_OPT[_DS]["expert_params"]
        pinned_genes = [k for k in base if k in _M._DS_PINNED_SCORE_WEIGHTS]
        assert sorted(pinned_genes) == ["w_analyst", "w_earnings", "w_fundamental", "w_technical"]
        collected = collect_param_space(_STRAT, expert_cfg={**spec["expert_params"], **_M._ds_factor_gene_space()})
        for k in _M._DS_PINNED_SCORE_WEIGHTS:
            assert f"model:{k}" not in collected, k
        # every other DS gene is still searched, in its original order
        assert list(spec["expert_params"]) == [k for k in base if k not in _M._DS_PINNED_SCORE_WEIGHTS]
        for k in ("macro_mode", "theta_buy", "theta_sell", "k_stop", "k_target"):
            assert f"model:{k}" in collected, k

    def test_the_shared_spec_is_not_mutated(self):
        before = dict(_M._EXPERT_OPT[_DS]["expert_params"])
        fixed_before = dict(_M._EXPERT_OPT[_DS]["fixed_settings"])
        _pinned_spec()
        assert _M._EXPERT_OPT[_DS]["expert_params"] == before
        assert _M._EXPERT_OPT[_DS]["fixed_settings"] == fixed_before

    def test_the_pin_reaches_the_run_settings_for_every_ds_strategy(self):
        spec = _pinned_spec()
        for kind in ("S1", "S2", "S6", "S8", "S9", "SQM"):
            s = _M._expert_run_settings(spec, ["AAPL"], None, strategy_kind=kind)
            for k, v in _EXPECTED_PIN.items():
                assert s[k] == v, (kind, k)
            assert s["sizing_mode"] == "risk_atr"                       # the spec's own fixed setting is kept
        assert _M._expert_run_settings(spec, ["AAPL"], None, strategy_kind="SQM")["min_stop_loss_pct"] == 15.0

    def test_the_guard_refuses_a_pin_that_did_not_apply(self):
        spec = _pinned_spec()
        settings = _M._expert_run_settings(spec, ["AAPL"], None, strategy_kind="S2")
        _M._check_ds_score_pin_applied("optimize", _DS, spec["expert_params"], settings)       # applied: silent
        with pytest.raises(SystemExit) as e:                          # a pinned key came back as a gene
            _M._check_ds_score_pin_applied("optimize", _DS, {**spec["expert_params"], "w_analyst": {"optimize": True}},
                                           settings)
        assert "w_analyst" in str(e.value)
        with pytest.raises(SystemExit) as e:                          # a run setting disagrees with the pin
            _M._check_ds_score_pin_applied("optimize", _DS, spec["expert_params"], {**settings, "tw_rsi": 0.15})
        assert "tw_rsi" in str(e.value)
        with pytest.raises(SystemExit):                               # a pinned setting is missing
            _M._check_ds_score_pin_applied("optimize", _DS, spec["expert_params"],
                                           {k: v for k, v in settings.items() if k != "fw_piotroski"})

    def test_a_spec_that_already_pins_a_different_value_is_refused(self):
        _pin()
        spec = {**_M._EXPERT_OPT[_DS], "fixed_settings": {"sizing_mode": "risk_atr", "w_analyst": 0.3}}
        with pytest.raises(SystemExit) as e:
            _M._apply_ds_score_pin(spec, _DS, "optimize")
        assert "w_analyst" in str(e.value)

    def test_the_optimize_gene_space_of_every_ds_strategy_passes_the_guard(self):
        """``expert_params`` composed the way ``_cmd_optimize`` composes it (minus the screener genes), and collected
        over the real built strategy: no pinned key anywhere in the GA space. (S8/S9 build only under
        ``--benchmark-regime-gates``; their expert genes are composed exactly like S1's.)"""
        spec = _pinned_spec()
        for kind in ("S1", "S2", "S6", "SQM"):
            strat = _M._build_strategy(kind, f"dspin-{kind}", _DS)
            expert_params = _M._pin_style_dead_genes(kind, spec=spec, expert_params={
                **_M._with_estimates_methods(spec["expert_params"], "alphavantage"),
                **_M._rm_opt_for(kind, None, atr_searched=True, regime_searched=True),
                **_M._weekday_schedule_genes(spec), **_M._sl_loosen_gene_space(),
                **_M._sizing_mode_gene_space(), **_M._ds_factor_gene_space()})
            settings = _M._expert_run_settings(spec, ["AAPL"], None, strategy_kind=kind)
            _M._check_ds_score_pin_applied("optimize", _DS, expert_params, settings)
            space = job_param_space(strat, expert_params, False)
            assert not [k for k in space if k.split(":", 1)[-1] in _M._DS_PINNED_SCORE_WEIGHTS], kind
            assert "model:theta_buy" in space, kind

    def test_confidence_ceiling_is_the_theoretical_bound_under_the_pin(self):
        assert _M._confidence_ceiling_for(_DS) == 50.0
        _pin()
        assert _M._confidence_ceiling_for(_DS) == _M._DS_FACTOR_CONFIDENCE_CEILING == 93.0
        assert _M._confidence_ceiling_for("FMPRating") is None


# ----------------------------------------------------------------------------- trial, re-run and export
def _run_block(settings):
    return {"backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
            "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0, "seed": 42,
            "account_settings": {}, "experts": [{"class": _DS, "settings": dict(settings)}]}


#: a genome of the pinned job's own space (only non-pinned genes)
_GENOME = {"model:theta_buy": 0.25, "model:k_stop": 2.0, "model:macro_mode": "gate"}

#: the results blob shape _persist_results requires (as tests/test_persist_top_backtests.py)
_RESULTS = {
    "total_trades": 3, "winning_trades": 2, "losing_trades": 1, "win_rate": 66.67,
    "total_return": 5.0, "annualized_return": 12.3, "buy_hold_return": 0.0,
    "sharpe_ratio": 1.1, "sortino_ratio": 1.4, "calmar_ratio": 0.9, "volatility": 8.2,
    "max_drawdown": -4.55, "avg_drawdown": -2.1, "max_drawdown_duration": 1.0,
    "profit_factor": 4.0, "expectancy": 2.0, "sqn": 0.7, "avg_trade": 2.0,
    "best_trade": 5.0, "worst_trade": -2.0, "avg_trade_duration": 2.0, "exposure_time": 33.3,
    "final_equity": 10_500.0, "equity_peak": 11_000.0,
    "equity_curve": [{"date": "2024-01-02", "equity": 10_000.0}, {"date": "2024-01-04", "equity": 10_500.0}],
    "drawdown_curve": [{"date": "2024-01-02", "drawdown": 0.0}, {"date": "2024-01-04", "drawdown": -4.55}],
    "trades": [{"symbol": "AAPL", "entry_time": "2024-01-02", "exit_time": None, "direction": "buy",
                "entry_price": 100.0, "exit_price": 0.0, "size": 10.0, "pnl": 500.0, "pnl_pct": 5.0,
                "bars_held": 2, "exit_reason": "unknown"}],
}


class TestTrialConfig:
    def test_pinned_settings_reach_the_ga_trial_expert(self):
        spec = _pinned_spec()
        settings = _M._new_job_expert_run_settings(_DS, spec, ["AAPL"], None, strategy_kind="S2")
        cfg = _build_daily_trial_config(_run_block(settings), decode_params(_STRAT, _GENOME), None,
                                        option_trade_records=False)
        s = cfg["experts"][0]["settings"]
        for k, v in _EXPECTED_PIN.items():
            assert s[k] == v, k
        assert s["theta_buy"] == 0.25 and s["macro_mode"] == "gate"       # the genes still apply


class TestStoredRowPaths:
    """TOP-N persist, walk-forward OOS re-run, rerun_handler and the deploy export all read the run block stored on
    the optimization (and ``expertFixedSettings`` stashed on each persisted row)."""

    @pytest.fixture(autouse=True, scope="class")
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _opt_and_row(settings, strategy_params):
        from datetime import datetime
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.models.strategy import Strategy
        from app.models.strategy_optimization import StrategyOptimization
        db = SessionLocal()
        strat = Strategy(name="dspin-strat", entry_rules=[], exit_rules=[])
        db.add(strat)
        db.commit()
        db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="dspin-opt", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": _run_block(settings)},
            parameter_ranges={k: {"type": "float"} for k in _GENOME}, all_results=[], best_params=dict(_GENOME),
            best_fitness=1.0, status="completed")
        db.add(opt)
        db.commit()
        db.refresh(opt)
        bt = Backtest(name="TOP1-dspin", expert_name=_DS, engine_type="daily_expert",
                      strategy_params=strategy_params, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 2, 1), initial_capital=10_000.0, optimization_id=opt.id)
        return db, opt, bt

    def test_rerun_handler_and_export_carry_the_pin(self):
        from app.api.backtests import _derive_export_payload
        from app.services.backtest.rerun_handler import rebuild_config_for_backtest
        spec = _pinned_spec()
        settings = _M._new_job_expert_run_settings(_DS, spec, ["AAPL"], None, strategy_kind="S2")
        # _persist_top_backtests stashes the run's full expert settings as expertFixedSettings on every row
        db, _opt, bt = self._opt_and_row(settings, {**_GENOME, "expertFixedSettings": dict(settings)})
        try:
            rerun = rebuild_config_for_backtest(bt, db)["experts"][0]["settings"]
            ep = _derive_export_payload(bt, "expert_settings", db)["settings"]["expert_params"]
        finally:
            db.close()
        for k, v in _EXPECTED_PIN.items():
            assert rerun[k] == v, ("rerun", k)
            assert ep[k] == v, ("export", k)
        assert ep["theta_buy"] == 0.25

    def test_export_of_a_row_whose_optimization_was_pruned_still_carries_the_pin(self):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        spec = _pinned_spec()
        settings = _M._new_job_expert_run_settings(_DS, spec, ["AAPL"], None, strategy_kind="S2")
        bt = Backtest(name="TOP1-dspin-orphan", expert_name=_DS, engine_type="daily_expert",
                      strategy_params={**_GENOME, "expertFixedSettings": dict(settings)},
                      start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1), initial_capital=10_000.0,
                      optimization_id=None)
        ep = _derive_export_payload(bt, "expert_settings", db=None)["settings"]["expert_params"]
        for k, v in _EXPECTED_PIN.items():
            assert ep[k] == v, k

    def test_persist_top_backtests_and_the_walk_forward_window_rerun_carry_the_pin(self, monkeypatch):
        """Drive the REAL ``_persist_top_backtests`` (TOP-N persist) and its ``window=`` form (the walk-forward
        out-of-sample re-run, ``tools/run_walk_forward.py``) with the engine stubbed (same pattern as
        test_persist_top_backtests.py): capture the trial config each re-run is given and the row it stores."""
        from concurrent.futures import ThreadPoolExecutor
        import app.services.strategy_optimization_handler as soh
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.services import sync_client
        spec = _pinned_spec()
        settings = _M._new_job_expert_run_settings(_DS, spec, ["AAPL"], None, strategy_kind="S2")
        db, opt, _bt = self._opt_and_row(settings, {})
        opt.all_results = [{"params": dict(_GENOME), "fitness": 1.0, "trades": 10}]
        db.commit()
        opt_id = opt.id
        db.close()
        seen = []
        pools = []

        def new_pool(max_workers):
            pools.append(ThreadPoolExecutor(max_workers=max_workers))
            return pools[-1]

        def fake_worker(cfg):
            seen.append(cfg)
            return {"ok": True, "results": dict(_RESULTS)}

        monkeypatch.setattr(_M, "_new_local_pool", new_pool)
        monkeypatch.setattr(soh, "_persist_trial_worker", fake_worker)
        monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
        try:
            for window, prefix in ((None, "TOP"), (("2024-01-01", "2024-01-31"), "WF1-OOS-R")):
                seen.clear()
                n = _M._persist_top_backtests(opt_id, _DS, n=1, parallel=1, use_remote_workers=False,
                                              window=window, name_prefix=prefix)
                assert n == 1 and len(seen) == 1, (window, n)
                s = seen[0]["experts"][0]["settings"]
                for k, v in _EXPECTED_PIN.items():
                    assert s[k] == v, (window, k)
                assert s["theta_buy"] == 0.25
                if window is not None:
                    assert str(seen[0]["start_date"])[:10] == "2024-01-01"
                db = SessionLocal()
                try:
                    row = db.query(Backtest).filter(Backtest.optimization_id == opt_id,
                                                    Backtest.name.like(f"{prefix}1-%")).one()
                    fixed = row.strategy_params["expertFixedSettings"]
                finally:
                    db.close()
                for k, v in _EXPECTED_PIN.items():
                    assert fixed[k] == v, (prefix, k)
                assert not any(f"model:{k}" in row.strategy_params for k in _EXPECTED_PIN)
        finally:
            for p in pools:
                p.shutdown(wait=True, cancel_futures=True)


# ----------------------------------------------------------------------------- capband driver
_DRIVER = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("capband_dspin", _DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run(tmp_path, *argv):
    env = {**os.environ, "BA2_HOME": str(tmp_path)}
    return subprocess.run([sys.executable, _DRIVER, *argv, "--dry-run"], capture_output=True, text=True, env=env)


def _names(tmp_path, *argv):
    r = _run(tmp_path, *argv)
    assert r.returncode == 0, r.stderr[-800:]
    return [ln.split()[1] for ln in r.stdout.splitlines() if ln.strip().startswith(("TODO", "DONE"))]


class TestDriver:
    def test_forwarded_to_deterministic_scorer_jobs_only(self, driver):
        on = SimpleNamespace(pin_ds_score_weights=True)
        off = SimpleNamespace(pin_ds_score_weights=False)
        assert driver.ds_score_pin_tokens(on, _DS) == ["--pin-ds-score-weights"]
        for e in ("FMPRating", "FMPEarningsDrift", "FactorRanker", "PullbackReversion"):
            assert driver.ds_score_pin_tokens(on, e) == []
        assert driver.ds_score_pin_tokens(off, _DS) == []

    def test_requires_the_dspin_name_token(self, tmp_path):
        r = _run(tmp_path, "--pin-ds-score-weights", "--name-suffix", "plain")
        assert r.returncode != 0 and "dspin" in r.stderr

    def test_refuses_a_dsfw_name_token(self, tmp_path):
        r = _run(tmp_path, "--pin-ds-score-weights", "--name-suffix", "goal2027-dsfw-dspin")
        assert r.returncode != 0 and "dsfw" in r.stderr

    def test_refused_together_with_the_search_flag(self, tmp_path):
        r = _run(tmp_path, "--pin-ds-score-weights", "--search-ds-factor-weights", "--name-suffix", "dsfw-dspin")
        assert r.returncode != 0 and "mutually exclusive" in r.stderr

    def test_the_flag_changes_the_ds_names_and_only_those(self, tmp_path):
        base = _names(tmp_path, "--name-suffix", "goal2027-dspin", "--strategies", "S1,S2")
        flagged = _names(tmp_path, "--name-suffix", "goal2027-dspin", "--strategies", "S1,S2", "--pin-ds-score-weights")
        ds = [n for n in base if _DS in n]
        ds_f = [n for n in flagged if _DS in n]
        assert ds and len(ds) == len(ds_f) and not set(ds) & set(ds_f)
        assert all("dspin" in n for n in ds_f)
        assert [n for n in base if _DS not in n] == [n for n in flagged if _DS not in n]
        assert len([n for n in base if _DS not in n]) > 3


# ----------------------------------------------------------------------------- grid wrapper
class TestGridScript:
    SH = os.path.join(_REPO, "tools", "grid_goal2027.sh")

    def _src(self):
        with open(self.SH, encoding="utf-8") as f:
            return f.read()

    def test_ds_steps_pin_by_default_with_a_search_switch(self):
        src = self._src()
        assert 'DS_WEIGHTS="${DS_WEIGHTS:-pin}"' in src
        assert "--pin-ds-score-weights" in src and "-dspin" in src
        assert "--search-ds-factor-weights" in src and "-dsfw" in src       # DS_WEIGHTS=search keeps the old cells

    def test_the_switch_travels_to_the_systemd_unit(self):
        line = next(ln for ln in self._src().splitlines() if "for v in PHASE ESTIMATES_SOURCE" in ln)
        assert "DS_WEIGHTS" in line.split("for v in", 1)[1].split(";")[0].split()

    def test_ds_weight_args(self, tmp_path):
        """Run the wrapper's own ``DS_WEIGHTS`` default and ``ds_weight_args`` function under bash."""
        import shutil
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash is not available on this host")
        src = self._src()
        default = next(ln for ln in src.splitlines() if ln.startswith("DS_WEIGHTS="))
        start = src.index("ds_weight_args() {")
        func = src[start:src.index("\n}\n", start) + 3]
        script = tmp_path / "dsw.sh"
        script.write_text(default + "\n" + func + 'ds_weight_args; echo "rc=$?"\n', encoding="utf-8", newline="\n")

        def run(**env):
            base = {k: v for k, v in os.environ.items() if k != "DS_WEIGHTS"}
            r = subprocess.run([bash, str(script)], capture_output=True, text=True, env={**base, **env})
            return r.stdout.strip().splitlines(), r.stderr

        assert run()[0] == ["--pin-ds-score-weights -dspin", "rc=0"]                  # the default is the pin
        assert run(DS_WEIGHTS="pin")[0] == ["--pin-ds-score-weights -dspin", "rc=0"]
        assert run(DS_WEIGHTS="search")[0] == ["--search-ds-factor-weights -dsfw", "rc=0"]
        out, err = run(DS_WEIGHTS="bogus")
        assert out == ["rc=2"] and "DS_WEIGHTS" in err
