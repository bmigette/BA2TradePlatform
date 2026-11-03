"""Stage-2 style ``risk`` block: the option risk manager and the per-instrument cap PER STYLE.

Plan docs/plans/2026-09-25-option-stage2-plan.md §3 items 8-9 (user decisions 2026-11-03 for job A):
the per-instrument cap is narrowed per style (5..25 for an all-debit style instead of the stage-1
5..50), and ``risk_manager_mode = classic_options`` is switched on with every required rail
declared -- some fixed, some searched. A style without a ``risk`` block keeps today's genes and
settings byte for byte.

The block lives on the STYLE, not on the expert spec: one expert spec (DeterministicScorer) serves
every grid, and ``test_no_shipped_expert_spec_selects_a_risk_manager_mode`` keeps pinning that no
expert spec selects a mode.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import ba2test_launcher as L  # noqa: E402

from tests.test_option_style_registration import _argv, _run  # noqa: E402

_STAGE2 = str(Path(__file__).resolve().parents[3] / "tools/option_styles.stage2.json")
_CAND = str(Path(__file__).resolve().parents[3] / "tools/option_styles.candidates.json")

_RISK = {
    "risk_manager_mode": "classic_options",
    "fixed": {"max_concurrent_structures": 20, "undefined_risk_max_pct": 0},
    "genes": {
        "max_deployment_pct": {"min": 30, "max": 80, "step": 10},
        "circuit_breaker_pct": {"min": 20, "max": 50, "step": 10},
        "max_notional_leverage": {"min": 2, "max": 10, "step": 2},
        "max_virtual_equity_per_instrument_percent": {"min": 5, "max": 25, "step": 5},
    },
}


@pytest.fixture(autouse=True)
def _no_style_leaks(monkeypatch):
    monkeypatch.setattr(L, "_STYLE_ACTIVE", None)
    yield


def _write(tmp_path, risk, members=({"key": "O_LC"},)):
    p = tmp_path / "styles.json"
    p.write_text(json.dumps({"schema": 1, "styles": {
        "s": {"status": "approved", "members": list(members), "risk": risk}}}))
    return str(p)


def _style(tmp_path, risk=_RISK):
    return L._load_style_spec(_write(tmp_path, risk), "s")


# --- validation -------------------------------------------------------------------------------
def test_a_complete_risk_block_validates(tmp_path):
    assert _style(tmp_path)["risk"] == _RISK


@pytest.mark.parametrize("rail", ["max_concurrent_structures", "max_deployment_pct",
                                  "max_notional_leverage", "circuit_breaker_pct"])
def test_every_required_rail_must_be_declared(tmp_path, rail):
    risk = json.loads(json.dumps(_RISK))
    risk["fixed"].pop(rail, None)
    risk["genes"].pop(rail, None)
    with pytest.raises(ValueError, match=rail):
        _style(tmp_path, risk)


def test_the_required_rails_are_the_risk_managers_own(tmp_path):
    from ba2_common.core.OptionRiskManagement import REQUIRED_RAIL_SETTINGS
    assert tuple(REQUIRED_RAIL_SETTINGS) == L._STYLE_RISK_REQUIRED
    assert set(REQUIRED_RAIL_SETTINGS) <= L._STYLE_RISK_KEYS


def test_a_rail_both_fixed_and_searched_is_refused(tmp_path):
    risk = json.loads(json.dumps(_RISK))
    risk["fixed"]["max_deployment_pct"] = 50
    with pytest.raises(ValueError, match="both fixed and searched"):
        _style(tmp_path, risk)


@pytest.mark.parametrize("bad", [
    {"risk_manager_mode": "smart"},
    {"unknown_field": 1},
])
def test_unknown_mode_or_field_is_refused(tmp_path, bad):
    risk = {**json.loads(json.dumps(_RISK)), **bad}
    with pytest.raises(ValueError, match="risk"):
        _style(tmp_path, risk)


def test_an_unknown_rail_key_is_refused(tmp_path):
    risk = json.loads(json.dumps(_RISK))
    risk["fixed"]["max_leverage"] = 2
    with pytest.raises(ValueError, match="max_leverage"):
        _style(tmp_path, risk)


@pytest.mark.parametrize("gene", [
    {"min": 30, "max": 20, "step": 10},
    {"min": 30, "max": 80, "step": 0},
    {"min": -5, "max": 80, "step": 10},
    {"min": 30, "max": 80},
    {"min": 30, "max": 80, "step": 10, "type": "int"},
])
def test_a_malformed_gene_band_is_refused(tmp_path, gene):
    risk = json.loads(json.dumps(_RISK))
    risk["genes"]["max_deployment_pct"] = gene
    with pytest.raises(ValueError, match="max_deployment_pct"):
        _style(tmp_path, risk)


@pytest.mark.parametrize("value", [-1, "20", True, None])
def test_a_malformed_fixed_value_is_refused(tmp_path, value):
    risk = json.loads(json.dumps(_RISK))
    risk["fixed"]["max_concurrent_structures"] = value
    with pytest.raises(ValueError, match="max_concurrent_structures"):
        _style(tmp_path, risk)


# --- the gene space and the settings ----------------------------------------------------------
def test_the_style_risk_genes_replace_the_option_cap_and_add_the_rails(tmp_path):
    L._STYLE_ACTIVE = _style(tmp_path)
    block = L._rm_opt_for("O_STYLE")
    cap = block["max_virtual_equity_per_instrument_percent"]
    assert (cap["optimize"], cap["min"], cap["max"], cap["step"]) == (True, 5.0, 25.0, 5.0)
    for rail, band in _RISK["genes"].items():
        g = block[rail]
        assert (g["optimize"], g["min"], g["max"], g["step"], g["type"]) == (
            True, float(band["min"]), float(band["max"]), float(band["step"]), "float")
    for fixed in _RISK["fixed"]:
        assert fixed not in block


def test_a_fixed_per_instrument_cap_drops_its_gene(tmp_path):
    risk = json.loads(json.dumps(_RISK))
    risk["genes"].pop("max_virtual_equity_per_instrument_percent")
    risk["fixed"]["max_virtual_equity_per_instrument_percent"] = 20
    L._STYLE_ACTIVE = _style(tmp_path, risk)
    assert "max_virtual_equity_per_instrument_percent" not in L._rm_opt_for("O_STYLE")
    settings = L._expert_run_settings({"fixed_settings": {}}, ["AAPL"], strategy_kind="O_STYLE")
    assert settings["max_virtual_equity_per_instrument_percent"] == 20


def test_the_style_writes_the_mode_and_the_fixed_rails_onto_the_expert_settings(tmp_path):
    L._STYLE_ACTIVE = _style(tmp_path)
    settings = L._expert_run_settings({"fixed_settings": {"a": 1}}, ["AAPL"],
                                      strategy_kind="O_STYLE")
    assert settings["risk_manager_mode"] == "classic_options"
    assert settings["max_concurrent_structures"] == 20
    assert settings["undefined_risk_max_pct"] == 0
    assert settings["a"] == 1
    for searched in _RISK["genes"]:
        assert searched not in settings or searched == "max_virtual_equity_per_instrument_percent"


def test_a_spec_naming_another_mode_conflicts_loudly(tmp_path):
    L._STYLE_ACTIVE = _style(tmp_path)
    with pytest.raises(ValueError, match="risk_manager_mode"):
        L._expert_run_settings({"fixed_settings": {}, "risk_manager_mode": "smart"}, ["AAPL"],
                               strategy_kind="O_STYLE")


def test_a_style_without_a_risk_block_changes_nothing():
    L._STYLE_ACTIVE = L._load_style_spec(_CAND, "short")
    assert L._rm_opt_for("O_STYLE") == L._rm_opt_for("O_LC")
    spec = {"fixed_settings": {"a": 1}}
    assert (L._expert_run_settings(spec, ["AAPL"], strategy_kind="O_STYLE")
            == L._expert_run_settings(spec, ["AAPL"], strategy_kind="O_LC"))


def test_other_keys_ignore_the_active_style(tmp_path):
    L._STYLE_ACTIVE = _style(tmp_path)
    assert L._rm_opt_for("O_LC")["max_virtual_equity_per_instrument_percent"]["max"] == 50.0
    assert "risk_manager_mode" not in L._expert_run_settings(
        {"fixed_settings": {}}, ["AAPL"], strategy_kind="O_LC")


# --- job A, as shipped ------------------------------------------------------------------------
def test_job_a_is_the_approved_member_order_with_the_user_rails():
    """User 2026-10-24 (order) and 2026-11-03 (cap 5..25, rails, no price cap)."""
    style = L._load_style_spec(_STAGE2, "mid")
    assert style["status"] == "approved"
    assert [m["key"] for m in style["members"]] == ["O_STRG", "O_STRD", "O_BULLCS", "O_BF", "O_LC"]
    assert style["price_cap_policy"] == "none"
    assert style["risk"] == _RISK


def test_job_a_reaches_the_persisted_optimization_config(monkeypatch):
    _, cfg = _run(_argv("mid", expert="DeterministicScorer", config=_STAGE2), monkeypatch)
    settings = cfg["backtest"]["experts"][0]["settings"]
    assert settings["risk_manager_mode"] == "classic_options"
    assert settings["max_concurrent_structures"] == 20
    genes = cfg["expert_params"]
    assert genes["max_virtual_equity_per_instrument_percent"]["max"] == 25.0
    assert genes["max_deployment_pct"]["min"] == 30.0
    assert genes["circuit_breaker_pct"]["max"] == 50.0
    assert genes["max_notional_leverage"]["step"] == 2.0
