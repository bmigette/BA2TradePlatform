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


# --------------------------------------------------------------------------- #
# 2026-11-07: point-in-time methods only, and DeterministicScorer + Senate gain the model
# --------------------------------------------------------------------------- #
_MODEL_EXPERTS = ("FMPEarningsDrift", "FMPInsiderClusterBuy", "DeterministicScorer",
                  "FMPSenateTraderWeight")


def test_every_model_expert_searches_only_point_in_time_methods():
    """'forward'/'trailing' read FMP estimates (one snapshot of today's revisions) and a backtest
    refuses them; the GA must never draw one."""
    from ba2_experts.analyst_target_model import POINT_IN_TIME_METHODS
    for expert in _MODEL_EXPERTS:
        gene = mod._EXPERT_OPT[expert]["expert_params"]["model_target_method"]
        assert gene["type"] == "choice"
        assert set(gene["choices"]) == set(POINT_IN_TIME_METHODS), expert


def test_deterministic_scorer_searches_the_model_target_switch():
    params = mod._EXPERT_OPT["DeterministicScorer"]["expert_params"]
    assert params["use_model_target"]["choices"] == [False, True]
    assert (params["max_expected_profit_percent"]["min"], params["max_expected_profit_percent"]["max"]) == (20.0, 500.0)


def test_senate_weight_searches_the_model_expected_profit_mode():
    params = mod._EXPERT_OPT["FMPSenateTraderWeight"]["expert_params"]
    assert set(params["expected_profit_mode"]["choices"]) == {"static", "model"}
    assert (params["max_expected_profit_percent"]["min"], params["max_expected_profit_percent"]["max"]) == (20.0, 500.0)


def test_model_genes_are_declared_expert_settings():
    """Every new gene names a setting the expert declares, with matching choices."""
    from ba2_experts.DeterministicScorer import DeterministicScorer
    from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight
    for cls in (DeterministicScorer, FMPSenateTraderWeight):
        defs = cls.get_settings_definitions()
        for key in ("model_target_method", "max_expected_profit_percent"):
            assert key in defs, (cls.__name__, key)
        for choice in mod._EXPERT_OPT[cls.__name__]["expert_params"]["model_target_method"]["choices"]:
            assert choice in defs["model_target_method"]["choices"]


def test_option_jobs_keep_their_pre_existing_ds_and_senate_gene_space():
    """The new DS/Senate model genes are for the stock grids; an option kind drops them (the
    stage-2 option grid must not gain search dimensions), the O_STK equity control keeps them."""
    for expert in ("DeterministicScorer", "FMPSenateTraderWeight"):
        spec = mod._EXPERT_OPT[expert]
        new = set(spec["stock_only_params"])
        assert new <= set(spec["expert_params"])
        for kind in ("O_STYLE", "O_LC"):
            assert not new & set(mod._pin_style_dead_genes(kind, dict(spec["expert_params"]), spec=spec)), (expert, kind)
        assert new <= set(mod._pin_style_dead_genes("O_STK", dict(spec["expert_params"]), spec=spec))
        assert new <= set(mod._pin_style_dead_genes("S1", dict(spec["expert_params"]), spec=spec))
