"""goal2027 item 2.8: ``ba2-test optimize --screener --screen-preset xl`` (a FIXED screen, no screener genes).

What this pins:
* without the option the launch inputs are exactly what they always were (base JSON, the searched gene set, cap band);
* the preset pins every selection key, with no genes and no cap band, and the live simulation can serve it;
* it is refused with ``--screener-cap-band`` / ``--screener-base-json`` and without ``--screener``;
* the stamped run config carries the keys and the export / deploy mapping hands live the SAME keys (deploy parity);
* the capband driver expresses DS x SQM x xl (``--screen-preset xl`` instead of ``--screener-cap-band``).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TESTPLATFORM)
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_preset", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_preset"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def _args(**kw):
    base = dict(screen_preset=None, screener=True, screener_cap_band=None, screener_base_json=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestLaunchInputs:
    def test_without_the_option_nothing_changes(self):
        base, opt, band = _M._screener_launch_inputs(_args(screener_cap_band="mid"))
        assert base == {} and opt is _M._SCREENER_OPT and band == "mid"

    def test_the_preset_states_every_key_with_no_genes_and_no_band(self):
        base, opt, band = _M._screener_launch_inputs(_args(screen_preset="xl"))
        assert opt == {} and band is None
        assert base == _M._SCREEN_PRESETS["xl"]
        assert base["market_cap_min"] == 1e10 and base["market_cap_max"] == 0
        assert base["price_min"] == 10 and base["volume_min"] == 500000 and base["float_min"] == 10000000
        assert base["max_stocks"] == 50 and base["weinstein_stage2_only"] is False
        assert base["dollar_volume_min"] == 0 and base["price_drop_pct"] == 0

    def test_the_returned_preset_is_a_copy(self):
        base, _, _ = _M._screener_launch_inputs(_args(screen_preset="xl"))
        base["max_stocks"] = 1
        assert _M._SCREEN_PRESETS["xl"]["max_stocks"] == 50

    def test_refused_with_a_cap_band_a_base_json_or_without_screener(self):
        for kw, word in ((dict(screener_cap_band="large"), "mutually"), (dict(screener_base_json="x.json"), "mutually"),
                         (dict(screener=False), "--screener")):
            with pytest.raises(SystemExit) as e:
                _M._screener_launch_inputs(_args(screen_preset="xl", **kw))
            assert word in str(e.value)

    def test_unknown_preset_is_refused(self):
        with pytest.raises(SystemExit):
            _M._screener_launch_inputs(_args(screen_preset="xxl"))


class TestTheSimulationCanServeIt:
    def test_every_selection_key_is_stated_and_the_gate_accepts_it(self):
        from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES, missing_screener_keys
        from ba2_providers.screener import live_sim as ls
        from ba2_providers.screener import metric_store as msb
        base = {**SCREENER_OFF_VALUES, **msb.normalize_screener_settings(_M._SCREEN_PRESETS["xl"])}
        assert missing_screener_keys(base) == []
        ls.require_complete_settings(base)
        ls.check_settings(base, ls.POST_FIX)

    def test_the_exploration_dollar_floor_would_have_been_refused(self):
        from ba2_providers.screener import live_sim as ls
        with pytest.raises(ls.SimulationRefusal):
            ls.check_settings({**_M._SCREEN_PRESETS["xl"], "dollar_volume_min": 10e6}, ls.POST_FIX)

    def test_the_static_universe_follows_the_fixed_filters_with_no_ranges(self):
        from ba2_providers.screener import universe_superset as us
        (variant,) = us.loosest_filter_variants(dict(_M._SCREEN_PRESETS["xl"]), {})
        assert variant["market_cap_min"] == 1e10 and variant["price_min"] == 10 and variant["volume_min"] == 500000
        assert "max_stocks" not in variant                      # the superset carries no cut
        assert us.gene_ranges_from_opt({}) == {}


class TestExportAndDeployParity:
    @pytest.fixture(autouse=True, scope="class")
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _stamped_base():
        from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES
        from ba2_providers.screener import metric_store as msb
        return {**SCREENER_OFF_VALUES, **msb.normalize_screener_settings(_M._SCREEN_PRESETS["xl"])}

    def _payload(self):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.models.strategy import Strategy
        from app.models.strategy_optimization import StrategyOptimization
        db = SessionLocal()
        strat = Strategy(name="xl-strat", entry_rules=[], exit_rules=[])
        db.add(strat)
        db.commit()
        db.refresh(strat)
        so = {"store": "/stores/s", "base_settings": self._stamped_base(), "declared_ranges": {},
              "cadence_days": 7, "apply_to_expert_settings": False, "screen_preset": "xl"}
        opt = StrategyOptimization(
            strategy_id=strat.id, name="xl-opt", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {"backtest_id": 1, "enabled_instruments": ["AAPL"],
                                              "experts": [{"class": "DeterministicScorer", "settings": {}}],
                                              "account_settings": {}, "warmup_days": 30, "seed": 42,
                                              "screener_opt": so}},
            parameter_ranges={"model:theta_buy": {"type": "float"}}, all_results=[], best_params={},
            best_fitness=1.0, status="completed")
        db.add(opt)
        db.commit()
        db.refresh(opt)
        bt = Backtest(name="TOP1-xl", expert_name="DeterministicScorer", engine_type="daily_expert",
                      strategy_params={"model:theta_buy": 0.3, "expertFixedSettings": {}},
                      start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1), initial_capital=10_000.0,
                      optimization_id=opt.id)
        try:
            return _derive_export_payload(bt, "expert_settings", db)
        finally:
            db.close()

    def test_the_export_carries_the_preset_keys_as_the_screener_universe(self):
        uni = self._payload()["universe"]
        assert uni["mode"] == "screener"
        for k, v in _M._SCREEN_PRESETS["xl"].items():
            assert uni["screener_settings"][k] == v, k

    def test_live_gets_the_same_keys_under_the_names_it_reads(self):
        from ba2_common.core.deploy_parity import (LIVE_SCREENER_SETTINGS, complete_screener_settings,
                                                   live_settings_from_universe, unmapped_screener_keys)
        payload = self._payload()
        live = live_settings_from_universe(payload["universe"])
        assert unmapped_screener_keys(payload["universe"]) == []
        for k, v in _M._SCREEN_PRESETS["xl"].items():
            assert live[f"screener_{k}"] == v, k
        assert {k for k in live if k.startswith("screener_") and k != "screener_universe_source"
                } <= set(LIVE_SCREENER_SETTINGS)
        # every selection key reaches live explicitly (nothing left to StockScreener's defaults)
        full = complete_screener_settings(live)
        assert full["screener_price_min"] == 10 and full["screener_max_stocks"] == 50
        assert full["screener_weinstein_stage2_only"] is False
        # no screener gene leaked into the expert settings
        assert not [k for k in payload["settings"]["expert_params"] if k.startswith("screener:")]


_DRIVER = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("capband_preset", _DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDriver:
    def test_band_tokens(self, driver):
        assert driver.band_screener_tokens("xl") == ["--screen-preset", "xl"]
        for band in ("large", "mid", "small"):
            assert driver.band_screener_tokens(band) == ["--screener-cap-band", band]

    def test_xl_has_a_default_stress_and_the_launcher_knows_the_preset(self, driver):
        assert driver._parse_stress_spread("xl=4") == {"xl": 4.0}
        assert "xl" in driver.SCREEN_PRESET_BANDS and set(driver.SCREEN_PRESET_BANDS) <= set(_M._SCREEN_PRESETS)

    def test_sqm_is_ds_only_in_the_job_list(self, driver):
        jobs = list(driver._jobs(["xl"], ["S1", "SQM"], False))
        by_strategy = {}
        for _n, expert, strat, _b in jobs:
            by_strategy.setdefault(strat, set()).add(expert)
        assert by_strategy["SQM"] == {"DeterministicScorer"}
        assert "DeterministicScorer" in by_strategy["S1"] and len(by_strategy["S1"]) > 1

    def test_the_ds_sqm_xl_cell_is_one_job_with_the_preset_flag(self):
        import subprocess
        skip = "FMPRating,FMPEarningsDrift,FMPInsiderClusterBuy,FMPSenateTraderWeight,FactorRanker"
        r = subprocess.run([sys.executable, _DRIVER, "--bands", "xl", "--strategies", "SQM", "--skip-experts", skip,
                            "--name-suffix", "goal2027", "--dry-run"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]
        todo = [ln for ln in r.stdout.splitlines() if ln.strip().startswith(("TODO", "DONE"))]
        assert len(todo) == 1 and "scr-xl-DeterministicScorer-SQM" in todo[0], r.stdout[-800:]
