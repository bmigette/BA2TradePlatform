"""PremiumSeller launcher optimization-grid registration (Task 5b): the BYPASS *options*
income expert joins ``_EXPERT_OPT`` with the options-cache seam (``options``), universe
injection into its own ``static_universe`` setting (``universe_setting``), and a bypass
gene space that opts OUT of the dead ``risk_per_trade_pct`` gene (``no_bypass_rm`` — its
OptionPortfolioManager owns its exits, so the engine stop pass has no reader for it).
Importlib-from-file pattern copied from test_launcher_parse_symbols.py.
"""
import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_premium_seller_grid_entry_flags():
    spec = mod._EXPERT_OPT["PremiumSeller"]
    assert spec["bypass"] is True
    assert spec["options"] is True
    assert spec["universe_setting"] == "static_universe"
    assert spec["no_bypass_rm"] is True
    assert spec["expert_params"], "PremiumSeller grid must carry optimizable expert params"
    for key, gene in spec["expert_params"].items():
        assert gene["optimize"] is True, key
        for field in ("min", "max", "step", "type"):
            assert field in gene, (key, field)


def test_expert_run_settings_injects_universe():
    spec = mod._EXPERT_OPT["PremiumSeller"]
    settings = mod._expert_run_settings(spec, ["AAPL", "MSFT"])
    # fixed_settings preserved, plus the run universe injected into static_universe.
    for k, v in spec["fixed_settings"].items():
        assert settings[k] == v
    assert settings["static_universe"] == "AAPL,MSFT"
    # A spec with no universe_setting (FactorRanker) yields its fixed_settings exactly.
    fr = mod._EXPERT_OPT["FactorRanker"]
    assert mod._expert_run_settings(fr, ["AAPL", "MSFT"]) == fr["fixed_settings"]


def test_apply_options_seam_sets_cache_for_options_expert():
    from app.services.backtest.daily_backtest_handler import default_options_cache_db

    block = {}
    mod._apply_options_seam(mod._EXPERT_OPT["PremiumSeller"], block)
    assert block["options_cache_db"] == default_options_cache_db()
    # Equity experts (no options flag) are untouched: no key appears at all.
    block2 = {}
    mod._apply_options_seam(mod._EXPERT_OPT["FactorRanker"], block2)
    assert block2 == {}


def test_bypass_gene_space_excludes_dead_rm_gene():
    ps_space = mod._bypass_gene_space(mod._EXPERT_OPT["PremiumSeller"])
    assert "risk_per_trade_pct" not in ps_space
    # PremiumSeller's own genes are all present.
    assert "target_delta" in ps_space
    assert "iv_rank_min" in ps_space
    # FactorRanker keeps the _BYPASS_RM_OPT gene (its per-name equity stop reads it).
    fr_space = mod._bypass_gene_space(mod._EXPERT_OPT["FactorRanker"])
    assert "risk_per_trade_pct" in fr_space
