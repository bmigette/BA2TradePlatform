"""``screener_sort_metric`` as a searched CATEGORICAL gene (owner 2026-11-04): market_cap | relative_volume | price_drop_pct.

Pinned: the declaration, the choice-gene decoding (index -> value in ``screener_overrides``), every numeric reader of the screener
ranges skipping it explicitly (declared ranges, the genome range check, the static-universe superset, the cap band), the seed fill,
the bypass expert (no live-faithful ordering: not searched, said), the deploy export / live mapping, and the unsupported orderings
refusing.  The RANKING parity with ``StockScreener`` is pinned in packages/providers/tests/test_live_sim_parity.py.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_screener_universe_restriction as R  # noqa: E402
from test_screener_universe_restriction import env, _prep, _req  # noqa: E402,F401

from app.services.backtest import screener_launch as sl  # noqa: E402
from app.services.genetic import GeneticOptimizer  # noqa: E402
from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402
from ba2_providers.screener import live_sim as ls  # noqa: E402
from ba2_providers.screener import universe_superset as us  # noqa: E402

GENE = "screener_sort_metric"
CHOICES = ["market_cap", "relative_volume", "price_drop_pct"]


def _strategy():
    return types.SimpleNamespace(entry_rules=None, exit_rules=None)


# ------------------------------------------------------------------------------------------ declaration + decoding
def test_the_gene_is_declared_categorical_with_market_cap_first():
    spec = us.SCREENER_OPT[GENE]
    assert spec["type"] == "choice" and spec["choices"] == CHOICES and spec["optimize"] is True
    assert "min" not in spec and "max" not in spec
    assert tuple(us.SORT_METRIC_CHOICES) == ls.SIMULATED_SORT_METRICS == tuple(CHOICES)


def test_the_launcher_alias_is_the_canonical_dict():
    import ba2test_launcher as L
    assert L._SCREENER_OPT is us.SCREENER_OPT and GENE in L._SCREENER_OPT


def test_the_gene_space_holds_an_index_gene_that_decodes_to_the_value():
    space = collect_param_space(_strategy(), screener_cfg=dict(us.SCREENER_OPT))
    e = space[f"screener:{GENE}"]
    assert e["type"] == "choice" and e["choices"] == CHOICES and (e["min"], e["max"], e["step"]) == (0, 2, 1)
    assert len([k for k in space if k.startswith("screener:")]) == len(us.SCREENER_OPT) == 7
    opt = GeneticOptimizer(param_ranges=space, population_size=4, n_generations=1)
    names = list(space)
    for idx, want in enumerate(CHOICES):
        ind = [0] * len(names)
        for i, n in enumerate(names):
            ind[i] = space[n]["min"]
        ind[names.index(f"screener:{GENE}")] = idx
        decoded = decode_params(_strategy(), opt.decode_individual(ind))
        assert decoded["screener_overrides"][GENE] == want
    # an out-of-range index is a loud error, never a clamp
    ind[names.index(f"screener:{GENE}")] = 3
    with pytest.raises(ValueError):
        opt.decode_individual(ind)


# ------------------------------------------------------------------------------------------ numeric readers skip it explicitly
def test_declared_ranges_and_the_genome_check_skip_the_categorical_gene():
    declared = sl.declared_ranges_of(us.SCREENER_OPT)
    assert "sort_metric" not in declared and "market_cap_min" in declared
    # a string value for the categorical gene is not range-checked (and does not crash the numeric check)
    us.check_genes_in_declared_ranges({GENE: "price_drop_pct", "screener_max_stocks": 20}, declared)
    with pytest.raises(us.ScreenerGenomeOutOfRange):
        us.check_genes_in_declared_ranges({GENE: "price_drop_pct", "screener_max_stocks": 60}, declared)


def test_the_static_universe_ignores_the_ordering_gene(env):
    with_gene = _prep(env)
    without = _prep(env, scr_opt={k: v for k, v in us.SCREENER_OPT.items() if k != GENE})
    assert with_gene.enabled == without.enabled
    assert GENE in with_gene.scr_opt and GENE not in without.scr_opt


def test_cap_band_override_keeps_the_gene():
    opt, _ = us.apply_cap_band(us.SCREENER_OPT, {}, "mid")
    assert opt[GENE] == us.SCREENER_OPT[GENE]
    ranges = us.gene_ranges_from_opt(opt)
    assert GENE in ranges and us.loosest_filter_variants({"market_cap_max": 1e10}, ranges)


def test_loosest_variants_are_identical_with_and_without_the_gene():
    ranges = us.gene_ranges_from_opt(us.SCREENER_OPT)
    without = {k: v for k, v in ranges.items() if k != GENE}
    assert us.loosest_filter_variants({}, ranges) == us.loosest_filter_variants({}, without)


@pytest.mark.parametrize("spec", [{"type": "choice", "choices": ["market_cap", "composite"], "optimize": True},
                                  {"type": "choice", "choices": ["volume"], "optimize": True},
                                  {"min": 0, "max": 1, "step": 1, "type": "int", "optimize": True}])
def test_an_unsimulated_or_malformed_ordering_gene_refuses_the_launch(spec):
    with pytest.raises(us.ScreenerUniverseError):
        us.loosest_filter_variants({}, {GENE: spec})


def test_the_bypass_expert_does_not_search_the_ordering_and_says_so(env):
    msgs = []
    prep = sl.prepare_screener_launch(_req(env, bypass=True, panel=None), emit=msgs.append)
    assert GENE not in prep.scr_opt and "screener_max_stocks" in prep.scr_opt
    assert any("screener_sort_metric is NOT searched" in m for m in msgs)
    assert prep.use_sim is False


# ------------------------------------------------------------------------------------------ seed fill
def test_seed_fill_starts_at_market_cap_with_a_note():
    import ba2test_launcher as L
    space = collect_param_space(_strategy(), screener_cfg=dict(us.SCREENER_OPT))
    fill, notes = L._screener_seed_fill(space)
    assert fill[f"screener:{GENE}"] == "market_cap"
    assert any(GENE in n and "market_cap" in n for n in notes)
    # a bundle that states the ordering wins
    fill2, _ = L._screener_seed_fill(space, {f"screener:{GENE}": "price_drop_pct"})
    assert fill2[f"screener:{GENE}"] == "price_drop_pct"


# ------------------------------------------------------------------------------------------ deploy export / live mapping
def test_the_decoded_gene_reaches_the_live_screener_sort_metric_setting():
    from ba2_common.core.deploy_parity import LIVE_SCREENER_SETTINGS, live_settings_from_universe, unmapped_screener_keys
    base = {"sort_metric": "market_cap", "market_cap_max": 1e10}                   # the job's stored base (unprefixed vocabulary)
    for want in CHOICES:
        eff = {**base, **{GENE: want, "screener_max_stocks": 20}}                  # exactly backtest_export's overlay
        universe = {"mode": "screener", "screener_store": "s", "screener_settings": eff}
        live = live_settings_from_universe(universe)
        assert live["screener_sort_metric"] == want and "sort_metric" not in live
        assert unmapped_screener_keys(universe) == []
    assert "screener_sort_metric" in LIVE_SCREENER_SETTINGS


def test_the_export_overlay_lets_the_gene_beat_the_base_value():
    from ba2_common.export import backtest_export
    src = Path(backtest_export.__file__).read_text(encoding="utf-8")
    assert '{**(screener_opt.get("base_settings") or {}), **screener_overrides}' in src       # gene AFTER base: the gene wins


def test_every_searched_ordering_is_a_value_live_accepts():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    MarketExpertInterface._ensure_builtin_settings()
    valid = MarketExpertInterface._builtin_settings["screener_sort_metric"]["valid_values"]
    assert set(CHOICES) <= set(valid)


# ------------------------------------------------------------------------------------------ the gate refuses what it cannot rank
@pytest.mark.parametrize("sm", ["volume", "float_shares", "composite"])
def test_unsimulated_orderings_refuse(sm):
    with pytest.raises(ls.SimulationRefusal, match="not simulated"):
        ls.check_settings({"sort_metric": sm}, ls.POST_FIX)


def test_price_drop_ordering_needs_a_window():
    ls.check_settings({"sort_metric": "price_drop_pct", "price_drop_days": 5}, ls.POST_FIX)
    with pytest.raises(ls.SimulationRefusal, match="price_drop_days"):
        ls.check_settings({"sort_metric": "price_drop_pct", "price_drop_days": 0}, ls.POST_FIX)
