"""goal2027 item 2.1: the ``model:sizing_mode`` gene (``ba2-test optimize --search-sizing-mode``).

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.1; plan:
docs/plans/2026-10-25-goal2027-sizing-gene-plan.md.

What this pins:
* a genome carrying ``model:sizing_mode`` builds EXACTLY the trial config of the matching fixed
  ``--sizing-mode`` run (risk_atr and notional);
* under notional, genes that cannot act (``atr_risk_budget_pct``) are neutralised, so two genomes
  that differ only there share one trial-memo key; under risk_atr nothing is dropped;
* ``--search-sizing-mode`` and an explicit ``--sizing-mode`` refuse together, and the gene is
  refused on bypass experts and option strategies;
* the chosen mode survives persistence into the deploy export, and an OLD row (no gene) is unchanged.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

from app.services.strategy_optimization_handler import (
    _build_daily_trial_config, canonical_trial_params,
)
from app.services.strategy_param_space import (
    NOTIONAL_INERT_MODEL_GENES, collect_param_space, decode_params, neutralise_sizing_genes,
)
from app.services.trial_memo import trial_key

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_sizing_gene", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_sizing_gene"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def test_the_worktree_code_is_under_test():
    import app.services.strategy_param_space as sps
    assert os.path.realpath(sps.__file__).startswith(os.path.realpath(_TESTPLATFORM))
    import ba2_common
    print("ba2_common resolved to", ba2_common.__file__)

#: A strategy with no rule templates: decode_params only needs the attributes it reads.
_STRAT = SimpleNamespace(entry_rules=None, exit_rules=None)

_RM_GENES = {"model:risk_per_trade_pct": 2.5, "model:atr_multiplier": 4.0,
             "model:max_virtual_equity_per_instrument_percent": 15.0}


def _bt_cfg(settings):
    return {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {},
        "experts": [{"class": "FMPRating", "settings": settings}],
    }


def _trial(settings, flat):
    cfg = _build_daily_trial_config(_bt_cfg(settings), decode_params(_STRAT, flat), None,
                                    option_trade_records=False)
    cfg.pop("backtest_id")      # a per-call random suffix, not part of the configuration
    return cfg


def _fixed_settings(mode):
    return _M._expert_run_settings(_M._EXPERT_OPT["FMPRating"], ["AAPL"], {"sizing_mode": mode})


def _gene_settings():
    return _M._expert_run_settings(_M._EXPERT_OPT["FMPRating"], ["AAPL"], None,
                                   sizing_mode_searched=True)


# ----------------------------------------------------------------------------- neutralisation
class TestNeutralise:
    def test_notional_drops_the_budget_gene(self):
        flat = {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 1.75, **_RM_GENES}
        out = neutralise_sizing_genes(flat)
        assert "model:atr_risk_budget_pct" not in out
        assert out["model:sizing_mode"] == "notional"
        assert {k: v for k, v in out.items() if k in _RM_GENES} == _RM_GENES
        assert "model:atr_risk_budget_pct" in flat, "the input is never mutated"

    def test_risk_atr_keeps_everything_and_returns_the_same_object(self):
        flat = {"model:sizing_mode": "risk_atr", "model:atr_risk_budget_pct": 1.75, **_RM_GENES}
        assert neutralise_sizing_genes(flat) is flat

    def test_no_gene_is_a_no_op_for_every_older_job(self):
        flat = {"model:atr_risk_budget_pct": 1.75, **_RM_GENES}
        assert neutralise_sizing_genes(flat) is flat

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="sizing_mode"):
            neutralise_sizing_genes({"model:sizing_mode": "kelly"})

    def test_max_virtual_equity_is_searched_in_both_modes(self):
        assert "model:max_virtual_equity_per_instrument_percent" not in NOTIONAL_INERT_MODEL_GENES
        assert NOTIONAL_INERT_MODEL_GENES == ("model:atr_risk_budget_pct",)


# ----------------------------------------------------------------------------- memo key (test b)
class TestMemoKey:
    @staticmethod
    def _key(flat):
        return trial_key({"engine": "daily", "seed": 1, "params": canonical_trial_params({}, flat)})

    def test_two_notional_genomes_differing_only_in_budget_share_a_key(self):
        a = {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 0.5, **_RM_GENES}
        b = {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 2.75, **_RM_GENES}
        assert self._key(a) == self._key(b)

    def test_two_risk_atr_genomes_differing_in_budget_do_not(self):
        a = {"model:sizing_mode": "risk_atr", "model:atr_risk_budget_pct": 0.5, **_RM_GENES}
        b = {"model:sizing_mode": "risk_atr", "model:atr_risk_budget_pct": 2.75, **_RM_GENES}
        assert self._key(a) != self._key(b)

    def test_modes_never_collide(self):
        a = {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 0.5, **_RM_GENES}
        b = {**a, "model:sizing_mode": "risk_atr"}
        assert self._key(a) != self._key(b)

    def test_a_job_without_the_gene_keeps_the_identity_function(self):
        flat = {"model:atr_risk_budget_pct": 0.5, **_RM_GENES}
        assert canonical_trial_params({}, flat) is flat


# ----------------------------------------------------------------------------- trial config (test a)
class TestTrialConfigEqualsFixedRun:
    def test_risk_atr_genome_equals_fixed_risk_atr_run(self):
        genes = {"model:atr_risk_budget_pct": 1.25, **_RM_GENES}
        fixed = _trial(_fixed_settings("risk_atr"), genes)
        gene = _trial(_gene_settings(), {"model:sizing_mode": "risk_atr", **genes})
        assert gene == fixed

    def test_notional_genome_equals_fixed_notional_run(self):
        # The fixed notional run's space has no atr_risk_budget_pct gene (_rm_opt_for drops it).
        fixed = _trial(_fixed_settings("notional"), dict(_RM_GENES))
        gene = _trial(_gene_settings(),
                      {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 2.0,
                       **_RM_GENES})
        assert gene == fixed

    def test_the_gene_wins_over_a_stale_pin(self):
        # Even if a pin were present in the run settings, the gene (expert_overrides) wins.
        trial = _trial(_fixed_settings("risk_atr"), {"model:sizing_mode": "notional", **_RM_GENES})
        assert trial["experts"][0]["settings"]["sizing_mode"] == "notional"

    def test_gene_run_settings_carry_no_pin(self):
        assert "sizing_mode" not in _gene_settings()
        assert _fixed_settings("risk_atr")["sizing_mode"] == "risk_atr"


# ----------------------------------------------------------------------------- gene space + refusals (c)
class TestLauncherFlag:
    def teardown_method(self):
        _M._resolve_search_sizing_mode(False, None, "optimize")

    def test_gene_space_is_a_two_value_choice_only_when_on(self):
        assert _M._sizing_mode_gene_space() == {}
        _M._resolve_search_sizing_mode(True, None, "optimize")
        space = _M._sizing_mode_gene_space()
        assert space == {"sizing_mode": {"optimize": True, "type": "choice",
                                         "choices": ["risk_atr", "notional"]}}
        collected = collect_param_space(_STRAT, expert_cfg=space)
        assert collected["model:sizing_mode"]["choices"] == ["risk_atr", "notional"]

    def test_gene_and_explicit_sizing_mode_refuse_together(self):
        for explicit in ("notional", "risk_atr"):
            with pytest.raises(SystemExit) as e:
                _M._resolve_search_sizing_mode(True, explicit, "optimize")
            assert "--search-sizing-mode" in str(e.value) and "--sizing-mode" in str(e.value)

    def test_explicit_alone_and_gene_alone_are_fine(self):
        assert _M._resolve_search_sizing_mode(False, "notional", "optimize") is False
        assert _M._resolve_search_sizing_mode(True, None, "optimize") is True

    def test_refused_on_bypass_and_option_kinds(self):
        _M._resolve_search_sizing_mode(True, None, "optimize")
        with pytest.raises(SystemExit):
            _M._refuse_sizing_gene_unsupported("optimize", "FACTOR", bypass=True)
        with pytest.raises(SystemExit):
            _M._refuse_sizing_gene_unsupported("optimize", "O_LC", bypass=False)
        _M._refuse_sizing_gene_unsupported("optimize", "S3", bypass=False)   # no raise
        _M._resolve_search_sizing_mode(False, None, "optimize")
        _M._refuse_sizing_gene_unsupported("optimize", "FACTOR", bypass=True)  # flag off: no raise

    def test_effective_mode_is_unknown_under_the_gene_so_the_budget_gene_stays(self):
        spec = _M._EXPERT_OPT["FMPRating"]
        args = SimpleNamespace(sizing_mode=None)
        assert _M._effective_sizing_mode(spec, args) == "risk_atr"
        _M._resolve_search_sizing_mode(True, None, "optimize")
        assert _M._effective_sizing_mode(spec, args) is None
        assert "atr_risk_budget_pct" in _M._rm_opt_for("S1", None)
        assert "atr_risk_budget_pct" not in _M._rm_opt_for("S1", "notional")


# ----------------------------------------------------------------------------- persistence / export (d)
class TestExportKeepsTheChosenMode:
    @pytest.fixture(autouse=True, scope="class")
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _export(sp):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        bt = Backtest(name="TOP1-t", expert_name="FMPRating", engine_type="daily_expert",
                      strategy_params=sp, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0,
                      optimization_id=None)
        return _derive_export_payload(bt, "expert_settings", db=None)["settings"]["expert_params"]

    @pytest.mark.parametrize("mode", ["risk_atr", "notional"])
    def test_decoded_gene_reaches_the_deploy_settings(self, mode):
        flat = {"model:sizing_mode": mode, "model:atr_risk_budget_pct": 1.5, **_RM_GENES}
        # what _persist_top_backtests stores: the raw genes, plus the run's fixed settings
        # (which carry NO sizing_mode in a gene run)
        sp = {**flat, "expertFixedSettings": _gene_settings()}
        assert self._export(sp)["sizing_mode"] == mode

    def test_gene_beats_a_persisted_old_pin(self):
        sp = {"model:sizing_mode": "notional", "expertFixedSettings": {"sizing_mode": "risk_atr"}}
        assert self._export(sp)["sizing_mode"] == "notional"

    def test_old_row_without_the_gene_still_exports_its_pin(self):
        sp = {"model:risk_per_trade_pct": 2.0, "expertFixedSettings": {"sizing_mode": "risk_atr"}}
        assert self._export(sp)["sizing_mode"] == "risk_atr"

    def test_rerun_of_a_stored_row_decodes_the_same_mode(self):
        flat = {"model:sizing_mode": "notional", "model:atr_risk_budget_pct": 1.5, **_RM_GENES}
        decoded = decode_params(_STRAT, flat)
        assert decoded["expert_overrides"]["sizing_mode"] == "notional"
        assert "atr_risk_budget_pct" not in decoded["expert_overrides"]
