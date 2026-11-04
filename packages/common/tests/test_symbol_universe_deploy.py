"""A restricted option job's candidate list is BACKTEST-ONLY: never exported, and a deploy clears ``screener_symbol_universe``."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.deploy_parity import live_settings_from_universe, unmapped_screener_keys
from ba2_common.export import backtest_export as be


def test_the_universe_block_maps_the_restriction_to_the_live_setting_without_a_warning():
    universe = {"mode": "screener", "screener_settings": {
        "market_cap_min": 1e10, "max_stocks": 20, "screener_symbol_universe": "AAA,BBB,CCC"}}
    out = live_settings_from_universe(universe)
    assert out["screener_symbol_universe"] == "AAA,BBB,CCC"
    assert out["screener_market_cap_min"] == 1e10 and out["instrument_selection_method"] == "screener"
    assert unmapped_screener_keys(universe) == []


# ---- the export, through derive_export_payload -------------------------------------------------------------------
def _export(restriction, *, expert="DeterministicScorer"):
    block = {"experts": [{"class": expert, "settings": {}}], "account_settings": {},
             "screener_opt": {"store": "s", "base_settings": {"market_cap_max": 0}, "cadence_days": 7,
                              **({"universe_restriction": restriction} if restriction is not None else {})}}
    bt = SimpleNamespace(id=1, name="t", expert_name=expert, engine_type="daily_expert",
                         strategy_params={"screener:screener_max_stocks": 20}, start_date=datetime(2024, 1, 1),
                         end_date=datetime(2024, 6, 1), initial_capital=10_000.0)
    return be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block)


def test_export_never_carries_the_backtest_only_restriction(caplog):
    """Owner 2026-11-04: the restriction is BACKTEST-ONLY; live screens without it, so nothing is exported."""
    p = _export({"symbols": ["AAA", "BBB", "CCC", "DDD"], "sha256": "x", "uncovered": ["BBB", "DDD"]})
    assert p["universe"]["mode"] == "screener"
    assert "screener_symbol_universe" not in p["universe"]["screener_settings"]
    out = live_settings_from_universe(p["universe"])
    assert "screener_symbol_universe" not in out and unmapped_screener_keys(p["universe"]) == []


def test_export_without_a_restriction_carries_no_key():
    p = _export(None)
    assert "screener_symbol_universe" not in p["universe"]["screener_settings"]


@pytest.mark.parametrize("bad", [{}, {"symbols": []}, {"symbols": "AAA"}])
def test_export_refuses_a_malformed_or_empty_restriction(bad):
    with pytest.raises(be.ExportRefused):
        _export(bad)


# ---- a later unrestricted deploy CLEARS the list ------------------------------------------------------------------
def test_deploy_writes_an_empty_list_when_the_payload_has_no_restriction():
    from ba2_common.core.deploy_parity import explicit_default_settings
    from ba2_experts.DeterministicScorer import DeterministicScorer
    from ba2_experts.FactorRanker import FactorRanker
    for cls in (DeterministicScorer, FactorRanker):
        assert explicit_default_settings(cls, {})["screener_symbol_universe"] == ""
        # a payload that carries one keeps it (the importer writes the payload's own value)
        assert "screener_symbol_universe" not in explicit_default_settings(cls, {"screener_symbol_universe": "AAA"})
