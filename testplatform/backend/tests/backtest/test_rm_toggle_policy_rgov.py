"""``--rm-toggle-policy atr-regime-searched`` -- goal2027's run policy (user decision 2026-11-03).

It is ``atr-searched`` (test_rm_toggle_policy_atr27.py) PLUS the SPY-stress regime overlay:
``regime_overlay_enabled`` is unpinned and searched 0/1, and the three ``regime_*_scale`` genes
stay in the space. Covered here: the persisted unpin list, decoding the overlay gene through the
trial-config builder (GA trial and re-run alike), the search-space shape, the ``-rgov`` name
guard, the checkpoint fingerprint, and the exporter refusing a policy row with no overlay gene.
The pinned and atr-searched policies are unchanged; their files are the controls.
"""
from __future__ import annotations

import pytest

from app.services.strategy_optimization_handler import (
    _build_daily_trial_config, checkpoint_fingerprint,
)
from app.services.strategy_param_space import collect_param_space, pinned_rm_toggles
from tests.backtest.test_rm_toggle_policy_atr27 import (  # noqa: F401 -- engine/db are fixtures
    _M, _bt_cfg, _decoded, _persist_policy_row, db, engine,
)

_POLICY = "atr-regime-searched"
_SCALES = ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale")


def _unpinned():
    return _M._rm_toggles_unpinned_for_policy(_POLICY)


class TestThePolicyUnpinsBothToggles:
    def test_the_persisted_unpin_list(self):
        assert _unpinned() == ["use_atr_stop", "regime_overlay_enabled"]

    def test_nothing_is_left_pinned(self):
        assert pinned_rm_toggles(_unpinned()) == {}
        assert _M._pinned_rm_toggles(_unpinned()) == {}

    def test_the_other_policies_are_unchanged(self):
        assert _M._rm_toggles_unpinned_for_policy("pinned") == []
        assert _M._rm_toggles_unpinned_for_policy("atr-searched") == ["use_atr_stop"]


class TestTheOverlayGeneDecodes:
    @pytest.mark.parametrize("gene,expected", [(1, True), (0, False)])
    def test_gene_reaches_the_trial_settings(self, gene, expected):
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool

        backtest_cfg = _bt_cfg(rm_toggles_unpinned=_unpinned())
        decoded = _decoded(use_atr_stop=1, regime_overlay_enabled=gene, regime_risk_scale=0.5)
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        settings = cfg["experts"][0]["settings"]
        assert coerce_bool(settings["regime_overlay_enabled"]) is expected
        assert coerce_bool(settings["use_atr_stop"]) is True
        assert settings["regime_risk_scale"] == 0.5

    def test_the_launcher_run_settings_carry_no_overlay_pin(self):
        settings = _M._expert_run_settings({"fixed_settings": {"sizing_mode": "risk_atr"}}, [],
                                           rm_toggles_unpinned=_unpinned())
        assert "regime_overlay_enabled" not in settings
        assert "use_atr_stop" not in settings


class TestSearchSpaceUnderThePolicy:
    def test_the_overlay_and_its_scales_are_searched(self):
        block = _M._rm_opt_for("S1", atr_searched=True, regime_searched=True)
        assert block["regime_overlay_enabled"] == {"optimize": True, "min": 0, "max": 1,
                                                   "step": 1, "type": "int"}
        for gene in _SCALES:
            assert block[gene] == _M._REGIME_OPT[gene]

    def test_everything_else_is_the_atr_searched_block(self):
        rgov = _M._rm_opt_for("S1", atr_searched=True, regime_searched=True)
        atr = _M._rm_opt_for("S1", atr_searched=True)
        assert {k: v for k, v in rgov.items() if k not in _SCALES + ("regime_overlay_enabled",)} \
            == {k: v for k, v in atr.items() if k != "regime_overlay_enabled"}

    def test_the_option_override_still_applies(self):
        block = _M._rm_opt_for("O_CSP", atr_searched=True, regime_searched=True)
        assert block["max_virtual_equity_per_instrument_percent"]["max"] == 50.0
        assert block["regime_overlay_enabled"]["optimize"] is True

    def test_regime_without_atr_is_refused(self):
        with pytest.raises(ValueError):
            _M._rm_opt_for("S1", regime_searched=True)

    def test_collected_param_space(self):
        space = collect_param_space(
            None, expert_cfg=_M._rm_opt_for("S1", atr_searched=True, regime_searched=True))
        assert space["model:regime_overlay_enabled"] == {"type": "int", "min": 0, "max": 1,
                                                         "step": 1}
        for gene in _SCALES + ("use_atr_stop",):
            assert f"model:{gene}" in space

    def test_policy_helpers(self):
        assert _M._policy_searches_atr(_POLICY) and _M._policy_searches_regime(_POLICY)
        assert _M._policy_searches_atr("atr-searched")
        assert not _M._policy_searches_regime("atr-searched")
        assert not _M._policy_searches_atr("pinned")
        assert _POLICY in _M._RM_TOGGLE_POLICIES


class TestTheNameGuard:
    @pytest.mark.parametrize("name", [None, "opt-DS-S1-rgov", "opt-DS-S1-atr27",
                                      "phase1-atr27-large-DS-S1"])
    def test_refuses_a_name_without_both_tokens(self, name):
        with pytest.raises(SystemExit):
            _M._refuse_atr_policy_without_job_name(_POLICY, name)

    def test_accepts_the_goal2027_name(self):
        _M._refuse_atr_policy_without_job_name(
            _POLICY, "g27-atr27-rgov-sizegene-mktprof-spyreg-large-DS-S1")


def test_checkpoint_fingerprint_differs_from_atr_searched():
    param_space = {"model:risk_per_trade_pct": {"type": "float", "min": 0.5, "max": 10.0, "step": 0.5}}
    ga = {"populationSize": 60, "generations": 20}
    assert (checkpoint_fingerprint(param_space, ga, None, _unpinned())
            != checkpoint_fingerprint(param_space, ga, None, ["use_atr_stop"]))


class TestTheExporterUnderThePolicy:
    def test_the_overlay_gene_is_carried(self, db):
        from app.api.backtests import _derive_export_payload

        bt = _persist_policy_row(
            db, model_genes={"model:use_atr_stop": 0, "model:regime_overlay_enabled": 1,
                             "model:regime_risk_scale": 0.75},
            rm_toggles_unpinned=_unpinned())
        params = _derive_export_payload(bt, "expert_settings", db)["settings"]["expert_params"]
        assert params["regime_overlay_enabled"] is True
        assert params["use_atr_stop"] is False

    def test_absent_overlay_gene_on_a_policy_row_raises(self, db):
        from app.api.backtests import _derive_export_payload

        bt = _persist_policy_row(db, model_genes={"model:use_atr_stop": 1},
                                 rm_toggles_unpinned=_unpinned())
        with pytest.raises(ValueError):
            _derive_export_payload(bt, "expert_settings", db)
