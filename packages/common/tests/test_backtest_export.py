from datetime import datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.deploy_parity import BacktestRunFacts, forced_expert_settings
from ba2_common.export import backtest_export as be


def _bt(**kw):
    base = dict(id=11, name="TOP1", expert_name="FMPRating", engine_type="daily_expert",
                strategy_params={"model:profit_ratio": 1.2}, start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 6, 1), initial_capital=10_000.0)
    base.update(kw)
    return SimpleNamespace(**base)


GATES = forced_expert_settings(BacktestRunFacts(enable_short=False, hold_assigned_stock=False,
                                                entry_action=None))


def test_fallback_branch_expert_settings():
    p = be.derive_export_payload(_bt(), "expert_settings")
    assert p["backtest_id"] == 11 and p["expert"] == "FMPRating"
    assert p["settings"]["expert_params"] == {"profit_ratio": 1.2, **GATES}
    assert p["start_date"] == "2024-01-01T00:00:00"
    assert list(p.keys()) == ["backtest_id", "name", "expert", "engine_type", "settings",
                              "backtest_only", "execution", "universe", "execution_interval",
                              "start_date", "end_date", "initial_capital"]


def test_opt_block_static_universe_and_base_settings():
    block = {"experts": [{"class": "FMPRating", "settings": {"sizing_mode": "notional"}}],
             "account_settings": {"commission_per_trade": 1.0, "slippage_bps": 5},
             "enabled_instruments": ["AAPL"], "seed": 3, "warmup_days": 30}
    p = be.derive_export_payload(_bt(), "expert_settings", opt_backtest_block=block)
    assert p["settings"]["expert_params"]["sizing_mode"] == "notional"
    assert p["universe"] == {"mode": "static", "symbols": ["AAPL"]}
    assert p["execution"]["commission"] == 1.0 and p["execution"]["seed"] == 3


def test_bypass_overlay_only_when_bypass_check_true():
    block = {"experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
             "account_settings": {}, "screener_opt": {
                 "store": "sp500", "base_settings": {"min_mcap": 1}, "cadence_days": 7,
                 "apply_to_expert_settings": True}}
    bt = _bt(expert_name="FactorRanker", strategy_params={"screener:min_mcap": 5})
    on = be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block,
                                  bypass_check=lambda name: name == "FactorRanker")
    off = be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block)
    assert on["settings"]["expert_params"]["universe_source"] == "screener"
    assert on["settings"]["expert_params"]["min_mcap"] == 5
    assert off["settings"]["expert_params"]["universe_source"] == "static"
    assert on["universe"]["mode"] == "screener"


def test_ruleset_unified_rules_pass_through_normalized():
    p = be.derive_export_payload(_bt(strategy_params={"entryRules": [], "exitRules": []}),
                                 "ruleset")
    assert p == {"backtest_id": 11, "name": "TOP1", "entry_rules": [], "exit_rules": [],
                 "optimized_genes": {}}


def test_legacy_reconstruction_callback_used_only_for_gene_only_rows():
    calls = []
    def recon():
        calls.append(1)
        return None, None, [], []
    gene_only = _bt(strategy_params={"cond:c1:threshold": 3})
    assert be.needs_legacy_reconstruction(gene_only.strategy_params) is True
    be.derive_export_payload(gene_only, "ruleset", reconstruct_legacy_ruleset=recon)
    assert calls == [1]
    # An EMPTY exit list counts as absent (the original `not exits`), so use a buy tree.
    with_trees = _bt(strategy_params={"cond:c1:threshold": 3, "buyEntryConditions": {}})
    assert be.needs_legacy_reconstruction(with_trees.strategy_params) is False
    empty_exits = _bt(strategy_params={"cond:c1:threshold": 3, "exitConditions": []})
    assert be.needs_legacy_reconstruction(empty_exits.strategy_params) is True


def test_unsupported_kind():
    with pytest.raises(be.UnsupportedExportKind):
        be.derive_export_payload(_bt(), "nope")


def test_refused_ruleset_raises_export_refused(monkeypatch):
    def boom(rules, where):
        raise ValueError("unresolved mode gene")
    monkeypatch.setattr(be, "assert_market_conditions_resolved", boom)
    with pytest.raises(be.ExportRefused, match="unresolved mode gene"):
        be.derive_export_payload(_bt(strategy_params={"entryRules": []}), "ruleset")


def test_build_deploy_entry_key_order():
    e = be.build_deploy_entry(backtest_id=1, target_instance_id=None, account_id=None,
                              virtual_equity_pct=10.0, expert_name="FMPRating", label="x",
                              ruleset={"r": 1}, settings={"s": 1})
    assert list(e) == ["backtest_id", "target_instance_id", "account_id", "virtual_equity_pct",
                       "expert_name", "label", "ruleset", "settings"]
