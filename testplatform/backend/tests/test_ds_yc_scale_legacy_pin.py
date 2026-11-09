"""DeterministicScorer yc_scale 0.005 -> 0.5 (2026-11-09): stored results stay reproducible.

No stored optimization block or backtest row states yc_scale: the launcher's DS spec writes only
``sizing_mode`` (+ the pinned RM toggles), and yc_scale is not a gene. Every one of them was scored
with the definition default of the day, 0.005. Without a pin, a re-run (``_build_daily_trial_config``
-> ``_expert_decision_settings``) and a deploy export (``derive_export_payload``) of such a row
would resolve the NEW definition default and silently measure / deploy a different strategy.

The rule (``ba2_common.core.legacy_setting_defaults``): an unstated changed key is the legacy
value; a NEW job states the current default, so it is never read as legacy.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import ba2test_launcher as L  # noqa: E402

_DS = "DeterministicScorer"


def _ds_cls():
    from ba2_experts.DeterministicScorer import DeterministicScorer
    return DeterministicScorer


def _legacy_block(settings=None):
    """What a pre-change launcher job persisted: the spec's fixed settings, no yc_scale."""
    return {"experts": [{"class": _DS, "settings": dict(settings if settings is not None else
                                                          L._expert_run_settings(L._EXPERT_OPT[_DS], ["AAPL"]))}],
            "backtest_id": "opt-1",
            "enabled_instruments": ["AAPL"], "start_date": "2024-03-01", "end_date": "2024-04-01",
            "initial_capital": 100000.0, "account_settings": {}, "execution_interval": "1d",
            "ohlcv_history_source": "alpaca", "estimates_source": "fmp"}


@pytest.fixture
def launcher_block(monkeypatch):
    """The block the REAL ``optimize`` CLI persists today (harness of the macro_short_side test)."""
    from tests.test_ds_macro_short_side_launcher import _run_optimize
    return _run_optimize("S2", monkeypatch)


def _as_pre_change(block):
    """The same block as a pre-change job stored it: identical but for the unstated yc_scale."""
    old = json.loads(json.dumps(block, default=str))
    del old["experts"][0]["settings"]["yc_scale"]
    return old


def _engine_settings(block, stored_row):
    """Trial config (as a remote worker receives it) -> the dict DeterministicScorer._process gets."""
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None,
               "sell_tree": None, "exit_rules": []}
    wire = json.loads(json.dumps(block, default=str))
    trial = _build_daily_trial_config(wire, decoded, option_trade_records=False, stored_row=stored_row)
    return _expert_decision_settings(_ds_cls(), trial["experts"][0]["settings"])


# --------------------------------------------------------------------------- #
# the finding: stored blocks do not carry the value
# --------------------------------------------------------------------------- #
def test_the_launcher_ds_spec_never_stated_yc_scale():
    spec = L._EXPERT_OPT[_DS]
    assert "yc_scale" not in spec["fixed_settings"]
    assert "yc_scale" not in (spec.get("option_fixed_settings") or {})
    for genes in ("expert_params", "stock_only_params"):            # not a gene either
        assert not any(k.endswith("yc_scale") for k in spec[genes])
    assert "yc_scale" not in L._expert_run_settings(spec, ["AAPL"])


# --------------------------------------------------------------------------- #
# the engine: legacy configs resolve 0.005, stated values win
# --------------------------------------------------------------------------- #
def test_the_definition_default_is_now_05():
    assert _ds_cls().get_settings_definitions()["yc_scale"]["default"] == 0.5


def test_an_unstated_yc_scale_resolves_the_legacy_value():
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    assert _expert_decision_settings(_ds_cls(), {})["yc_scale"] == 0.005
    assert _expert_decision_settings(_ds_cls(), {"yc_scale": 0.5})["yc_scale"] == 0.5
    assert _expert_decision_settings(_ds_cls(), {"yc_scale": 0.005})["yc_scale"] == 0.005


@pytest.mark.parametrize("stored_row", [True, False], ids=["rerun", "resumed-job"])
def test_a_pre_change_block_reruns_on_0005(launcher_block, stored_row):
    assert _engine_settings(_as_pre_change(launcher_block), stored_row)["yc_scale"] == 0.005


def test_other_experts_resolve_exactly_as_before():
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    from ba2_experts.FMPRating import FMPRating
    defs = FMPRating.get_settings_definitions()
    got = _expert_decision_settings(FMPRating, {})
    assert got == {k: defs[k]["default"] for k in FMPRating._SETTING_KEYS}


# --------------------------------------------------------------------------- #
# new jobs state 0.5 (launcher + APIs), and it survives every reader
# --------------------------------------------------------------------------- #
def test_a_new_launcher_job_states_the_current_default():
    spec = L._EXPERT_OPT[_DS]
    new = L._new_job_expert_run_settings(_DS, spec, ["AAPL"], strategy_kind="S1")
    assert new == {**L._expert_run_settings(spec, ["AAPL"], strategy_kind="S1"), "yc_scale": 0.5}


@pytest.mark.parametrize("stored_row", [False, True], ids=["new-trial", "rerun"])
def test_a_new_launcher_job_runs_and_reruns_on_05(launcher_block, stored_row):
    assert launcher_block["experts"][0]["settings"]["yc_scale"] == 0.5
    assert _engine_settings(launcher_block, stored_row)["yc_scale"] == 0.5


def test_a_new_launcher_job_for_another_expert_is_byte_identical():
    for name, spec in L._EXPERT_OPT.items():
        if name == _DS:
            continue
        assert L._new_job_expert_run_settings(name, spec, ["AAPL"]) == L._expert_run_settings(spec, ["AAPL"]), name


def test_new_job_expert_specs_shapes():
    from app.services.backtest.daily_backtest_handler import new_job_expert_specs
    assert new_job_expert_specs([_DS]) == [{"class": _DS, "settings": {"yc_scale": 0.5}}]
    assert new_job_expert_specs([{"class": _DS, "settings": {"yc_scale": 0.005}}]) == \
        [{"class": _DS, "settings": {"yc_scale": 0.005}}]
    other = {"class": "FMPRating", "settings": {"x": 1}}
    out = new_job_expert_specs(["FMPRating", other])
    assert out == ["FMPRating", other] and out[1] is other


def test_the_optimize_api_states_the_new_default():
    from app.api import strategies as S

    class _Q:
        def filter(self, *a): return self
        def first(self): return SimpleNamespace(exit_rules=[], entry_rules=[])

    class _Db:
        def query(self, *a): return _Q()

    cfg = {"backtest": {"experts": [{"class": _DS, "settings": {"sizing_mode": "risk_atr"}}, "FMPRating"],
                        "estimates_source": "fmp"}}
    out = S._stamp_new_optimization_source(cfg, 1, _Db())
    assert out["backtest"]["experts"] == [
        {"class": _DS, "settings": {"sizing_mode": "risk_atr", "yc_scale": 0.5}}, "FMPRating"]
    assert cfg["backtest"]["experts"][0]["settings"] == {"sizing_mode": "risk_atr"}   # not mutated
    # stamping twice (screener path + enqueue) changes nothing more
    assert S._stamp_new_optimization_source(out, 1, _Db())["backtest"]["experts"] == out["backtest"]["experts"]


# --------------------------------------------------------------------------- #
# GA checkpoint identity: a 0.5 job never resumes a 0.005 checkpoint, old fingerprints unchanged
# --------------------------------------------------------------------------- #
def test_checkpoint_identity():
    from app.services import strategy_optimization_handler as H
    assert H.checkpoint_expert_settings_identity(_legacy_block()) == {}
    assert H.checkpoint_expert_settings_identity(_legacy_block({"yc_scale": 0.005})) == {}
    assert H.checkpoint_expert_settings_identity(_legacy_block({"yc_scale": 0.5})) == \
        {"DeterministicScorer.yc_scale": 0.5}


# --------------------------------------------------------------------------- #
# deploy export
# --------------------------------------------------------------------------- #
def _bt(expert, strategy_params):
    return SimpleNamespace(id=7, name="row", expert_name=expert, engine_type="daily_expert",
                           strategy_params=strategy_params, start_date=datetime(2024, 3, 1),
                           end_date=datetime(2024, 4, 1), initial_capital=100000.0)


def _exported(expert, strategy_params, block):
    from ba2_common.export.backtest_export import derive_export_payload
    return derive_export_payload(_bt(expert, strategy_params), "expert_settings",
                                 opt_backtest_block=block)["settings"]["expert_params"]


def test_a_pre_change_optimization_row_deploys_0005():
    params = _exported(_DS, {"model:theta_buy": 0.3}, _legacy_block())
    assert params["yc_scale"] == 0.005
    assert params["theta_buy"] == 0.3


def test_a_pre_change_standalone_row_deploys_0005():
    assert _exported(_DS, {"expertSettings": {"macro_mode": "gate"}}, None)["yc_scale"] == 0.005
    # a pruned optimization: only the persisted fixed settings survive
    assert _exported(_DS, {"expertFixedSettings": {"sizing_mode": "risk_atr"},
                           "model:theta_buy": 0.3}, None)["yc_scale"] == 0.005


def test_a_new_row_deploys_what_it_stated():
    new = L._new_job_expert_run_settings(_DS, L._EXPERT_OPT[_DS], ["AAPL"])
    assert _exported(_DS, {"model:theta_buy": 0.3}, _legacy_block(new))["yc_scale"] == 0.5
    assert _exported(_DS, {"expertFixedSettings": new, "model:theta_buy": 0.3}, None)["yc_scale"] == 0.5


def test_other_experts_export_no_yc_scale():
    assert "yc_scale" not in _exported("FMPRating", {"model:x": 1}, None)
