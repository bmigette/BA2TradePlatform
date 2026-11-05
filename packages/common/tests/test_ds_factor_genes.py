"""DeterministicScorer factor-gene canonicalisation (goal2027 2.8): the one function the GA decode and the deploy export share."""
from ba2_common.core.ds_factor_genes import DS_FACTOR_GENES, neutralise_ds_factor_genes

_RECIPE = {"model:w_technical": 0.7, "model:w_fundamental": 0.3, "model:w_analyst": 0.0,
           "model:tw_mom": 0.7, "model:tw_d200": 0.3, "model:tw_rsi": 0.0, "model:tw_don": 0.0,
           "model:fw_quality": 0.6, "model:fw_piotroski": 0.4, "model:fw_value": 0.0, "model:fw_growth": 0.0,
           "model:mom_lookback_days": 126}


def test_identity_without_factor_genes_and_for_the_recipe():
    flat = {"model:theta_buy": 0.3}
    assert neutralise_ds_factor_genes(flat) is flat
    assert neutralise_ds_factor_genes(_RECIPE) is _RECIPE


def test_zero_sum_section_is_switched_off_and_dead_genes_dropped():
    out = neutralise_ds_factor_genes({**_RECIPE, "model:tw_mom": 0.0, "model:tw_d200": 0.0})
    assert out["model:w_technical"] == 0.0
    assert not any(k in out for k in ("model:tw_mom", "model:tw_d200", "model:mom_lookback_days"))
    both = neutralise_ds_factor_genes({**_RECIPE, "model:w_analyst": 0.5, "model:aw_targets": 0.0,
                                       "model:aw_grades": 0.0})
    assert both["model:w_analyst"] == 0.0 and "model:aw_targets" not in both


def test_gene_list_covers_the_family():
    assert "model:aw_targets" in DS_FACTOR_GENES and "model:mom_lookback_days" in DS_FACTOR_GENES
