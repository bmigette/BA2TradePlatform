"""Grid gene-space wiring for the analyst_target_model price-target feature (2026-08-27,
commit a30794ac "opt-in fundamentals-only price-target model for 3 experts").

The feature ships default-off (expected_profit_mode='static' everywhere it's wired in), so
turning it on for the goal2020 grid requires the GA to actually be ABLE to select it:
  - FMPEarningsDrift already GA-tunes expected_profit_mode (static/dynamic) and
    max_expected_profit_percent (20-500, from the earlier profit-cap fix) -- the model just
    adds a third mode value.
  - FMPInsiderClusterBuy previously optimized neither its expected_profit knobs at all; the new
    feature is the first time this expert's profit sizing is GA-searchable.

Importlib-from-file pattern copied from test_launcher_premium_seller_grid.py.
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


def test_earnings_drift_expected_profit_mode_can_select_model():
    gene = mod._EXPERT_OPT["FMPEarningsDrift"]["expert_params"]["expected_profit_mode"]
    assert gene["type"] == "choice"
    assert "model" in gene["choices"]
    # Existing modes must not be dropped -- this is an addition, not a replacement.
    assert set(gene["choices"]) >= {"static", "dynamic", "model"}


def test_earnings_drift_max_expected_profit_percent_range_unchanged():
    """The feature's price cap reuses this EXISTING gene (per commit a30794ac's own docstring:
    'FMPEarningsDrift's existing max_expected_profit_percent cap is reused for the new mode
    too'). Must still be 20-500 -- nothing to add here, just guard against regressing it."""
    gene = mod._EXPERT_OPT["FMPEarningsDrift"]["expert_params"]["max_expected_profit_percent"]
    assert gene["optimize"] is True
    assert gene["min"] == 20.0
    assert gene["max"] == 500.0


def test_insider_cluster_buy_gains_expected_profit_mode_toggle():
    """FMPInsiderClusterBuy had no expected_profit_mode gene before this feature (it only ever
    had a flat expected_profit_percent, never GA-tuned). The 'on/off' toggle for the new model
    is this gene reaching 'model' vs 'static' -- there is no 'dynamic' mode for this expert."""
    gene = mod._EXPERT_OPT["FMPInsiderClusterBuy"]["expert_params"]["expected_profit_mode"]
    assert gene["type"] == "choice"
    assert set(gene["choices"]) == {"static", "model"}


def test_insider_cluster_buy_gains_price_cap_gene_matching_earnings_drift():
    """Same 20-500 range as FMPEarningsDrift's cap -- same feature, same failure mode
    (a sane anchor P/E can still compound into an unrealistic multi-bagger target)."""
    gene = mod._EXPERT_OPT["FMPInsiderClusterBuy"]["expert_params"]["max_expected_profit_percent"]
    assert gene["optimize"] is True
    assert gene["min"] == 20.0
    assert gene["max"] == 500.0
