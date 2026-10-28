"""Final whole-branch review of the ML exploration v2, part B (grid): the store guards (holdout, code version,
membership), unavailable regimes, the common-period column, the confounding note and the CPU hard deadline."""
import json
import os

import pytest

from app.services import ml_features as F
from app.services import ml_grid as G
from app.services import ml_holdout as HO
from tests.test_ml_grid import (SYMS, build_store, cell, payload, signal_world)  # noqa: F401

LOCKED = "AAA"
assert HO.is_locked_largecap(LOCKED)


@pytest.fixture
def world(tmp_path):
    """A private dev store (the module-scoped worlds are shared: a test here edits the meta)."""
    root = tmp_path / "w"
    d = build_store(root / "stores", "stocks", SYMS, root / "cache", 0.0, 3)
    return root / "stores", d, root / "cache"


def _edit_meta(d, **change):
    path = d / "features.parquet.meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for k, v in change.items():
        if k == "code":
            meta["ensure"]["inputs"]["code"] = v
        else:
            meta[k] = v
    before = path.stat().st_mtime_ns
    path.write_text(json.dumps(meta), encoding="utf-8")
    os.utime(path, ns=(before + 10**9, before + 10**9))          # G._meta_of caches on the mtime: make the edit visible


def test_the_synthetic_store_is_a_valid_dev_store(world):
    assert len(G.store_signature(world[0], SYMS)) == 16
    assert G.load_panel(world[0], tuple(SYMS), "selection", world[2]).symbols == tuple(SYMS)


@pytest.mark.parametrize("field", ["universe", "symbols"])
def test_a_store_listing_a_locked_symbol_is_refused_by_the_signature_and_the_panel(world, field):
    store, d, cache = world
    _edit_meta(d, **{field: list(SYMS) + [LOCKED]})
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.store_signature(store, SYMS)
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.load_panel(store, tuple(SYMS), "selection", cache)


def test_a_holdout_store_is_refused_for_dev_grids(world):
    store, d, cache = world
    _edit_meta(d, purpose="holdout")
    for call in (lambda: G.store_signature(store, SYMS), lambda: G.load_panel(store, tuple(SYMS), "selection", cache)):
        with pytest.raises(ValueError, match="holdout"):
            call()


def test_a_dev_purpose_store_is_accepted(world):
    store, d, cache = world
    _edit_meta(d, purpose="dev")
    assert G.store_signature(store, SYMS)


def test_a_membership_store_is_refused(world):
    store, d, cache = world
    _edit_meta(d, membership={"top_n": 100, "purpose": "dev", "symbols": 10, "digest": "x"})
    msg = "point-in-time membership stores are not supported by the stage-1 grid yet"
    with pytest.raises(ValueError, match=msg):
        G.store_signature(store, SYMS)
    with pytest.raises(ValueError, match=msg):
        G.load_panel(store, tuple(SYMS), "selection", cache)


def test_a_store_built_by_other_feature_code_is_refused_with_the_remedy(world):
    store, d, cache = world
    _edit_meta(d, code="0123456789abcdef")
    for call in (lambda: G.store_signature(store, SYMS), lambda: G.load_panel(store, tuple(SYMS), "selection", cache)):
        with pytest.raises(ValueError, match="rebuild the store with `ba2-test ml-features`") as ei:
            call()
        assert "0123456789abcdef" in str(ei.value) and F.code_version() in str(ei.value)


def test_precheck_grid_refuses_a_stale_or_locked_store_as_the_api_does(world):
    store, d, cache = world
    body = payload((store, d, cache), cells=[cell(store)])
    assert G.precheck_grid(body) == 1
    _edit_meta(d, code="0123456789abcdef")
    with pytest.raises(ValueError, match="rebuild the store"):
        G.precheck_grid(body)
    _edit_meta(d, code=F.code_version(), universe=list(SYMS) + [LOCKED])
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        G.precheck_grid(body)


# ================================================================================ B7: unavailable regimes, common period
from datetime import date  # noqa: E402

from app.services import ml_grid_report as R  # noqa: E402
from app.services import ml_walkforward as W  # noqa: E402
from tests.test_ml_grid_report import R6, baselines, cand, cells_of, mk, write_run  # noqa: E402,F401

PRIMARY_NAME = G.PRIMARY["selection"]


def _without_regimes(rows, *gone):
    """The rows of a cell whose model could not score ``gone`` at all (no block of those regimes was fitted)."""
    for r in rows:
        for g in gone:
            del r["regimes"][PRIMARY_NAME][g]
    return rows


def test_a_regime_no_seed_could_score_is_marked_not_available_and_the_rule_is_unchanged():
    rows = baselines() + _without_regimes(cand("late"), "2019", "2020crash")        # four regimes scored, all beaten
    c = cells_of(R.gate(rows))["late"]
    assert c["unavailable"] == ["2019", "2020crash"] and c["regimes_beaten"] == 4
    assert c["passed"] and c["reasons"] == []                                           # >= 4 of 6, incl. 2022bear
    assert [c["regimes"][r]["available"] for r in R6] == [False, False, True, True, True, True]
    assert c["regimes"]["2019"]["value"] is None and c["regimes"]["2019"]["counted"] is False
    full = cells_of(R.gate(baselines() + cand("full")))["full"]
    assert full["unavailable"] == [] and all(full["regimes"][r]["available"] for r in R6)


def test_the_reason_names_the_unavailable_regimes_when_the_rule_fails():
    rows = baselines() + _without_regimes(cand("short"), "2019", "2020crash", "2021")        # three left: fewer than four
    c = cells_of(R.gate(rows))["short"]
    assert not c["passed"] and c["unavailable"] == ["2019", "2020crash", "2021"]
    assert any("only 3 of 6" in r for r in c["reasons"])
    assert any(r == "regimes unavailable: 2019, 2020crash, 2021" for r in c["reasons"]), c["reasons"]


def test_a_regime_missing_for_one_seed_only_is_not_structural():
    seeds = cand("partial")
    _without_regimes([seeds[0]], "2019")                                                 # only seed 1 lacks 2019
    c = cells_of(R.gate(baselines() + seeds))["partial"]
    assert c["unavailable"] == [] and c["regimes"]["2019"]["available"] is True and c["regimes"]["2019"]["counted"] is False


def test_the_report_and_the_dashboard_show_not_available(tmp_path):
    out = tmp_path / "grid_na"
    write_run(out, baselines() + _without_regimes(cand("late"), "2019", "2020crash"))
    st = R.collect_state(out, live=False)
    md = R.render_report(st)
    row = next(line for line in md.split("## Per-regime control")[1].splitlines() if line.startswith("| late"))
    cols = [x.strip() for x in row.strip("|").split("|")]
    assert cols[2] == "n/a" and cols[3] == "n/a" and cols[4] != "n/a"                  # 2019, 2020crash n/a; 2021 scored
    gate_row = next(line for line in md.split("## Gate table")[1].splitlines() if line.startswith("| 1 |") and "late" in line)
    assert "4 of 6 (n/a: 2019, 2020crash)" in gate_row
    html = R.render_dashboard(st)
    assert html.count('class="dot na"') == 2 and 'title="2019: n/a"' in html


def _regs(rows, **values):
    """Set the per-regime primary mean of every row of a cell (regime name with '_' for '-': r2023_24)."""
    names = {"r2019": "2019", "r2020crash": "2020crash", "r2021": "2021", "r2022bear": "2022bear", "r2023_24": "2023-24",
             "r2025_26": "2025-26"}
    for r in rows:
        for k, v in values.items():
            r["regimes"][PRIMARY_NAME][names[k]]["mean"] = v
    return rows


def _two_families():
    """lgbm scored 2019 and the 2020 crash hugely (0.10), xgb could not score them at all; from 2020H2 on both earn 0.02."""
    early_good = _regs(cand("lgbm_a", reg=0.02, overall=0.047, family="lgbm"), r2019=0.10, r2020crash=0.10)
    late_only = _without_regimes(cand("xgb_a", reg=0.02, overall=0.02, family="xgb"), "2019", "2020crash")
    return early_good + late_only


def test_the_common_period_column_compares_families_on_the_same_period():
    fx = G.factor_effects(_two_families(), "selection")
    f = fx["factors"]
    assert f.loc[("family", "lgbm"), "mean"] > f.loc[("family", "xgb"), "mean"]              # the whole-period means differ
    assert f.loc[("family", "lgbm"), "common_mean"] == pytest.approx(0.02)                 # ... the common period does not
    assert f.loc[("family", "xgb"), "common_mean"] == pytest.approx(0.02)
    cells = fx["cells"].set_index("cell_key")
    assert cells.loc["lgbm_a", "common_mean"] == pytest.approx(0.02) and cells.loc["xgb_a", "common_mean"] == pytest.approx(0.02)
    assert fx["common_start"] == date(2020, 7, 1)


def test_the_common_period_is_a_pooled_mean_over_the_regimes_from_the_common_start():
    rows = _regs(cand("c", reg=0.0, overall=0.0), r2021=0.01, r2022bear=0.04, r2023_24=0.02, r2025_26=0.06)
    for r in rows:
        r["regimes"][PRIMARY_NAME]["2022bear"]["n"] = 150                                    # three times the daily rows of the others
    pooled = (50 * 0.01 + 150 * 0.04 + 50 * 0.02 + 50 * 0.06) / 300
    assert G.factor_effects(rows, "selection")["cells"].loc[0, "common_mean"] == pytest.approx(pooled)
    # a later common start drops 2021
    since_22 = G.factor_effects(rows, "selection", common_start=date(2022, 1, 1))
    assert since_22["cells"].loc[0, "common_mean"] == pytest.approx((150 * 0.04 + 50 * 0.02 + 50 * 0.06) / 250)
    assert since_22["common_start"] == date(2022, 1, 1)


def test_a_cell_that_cannot_score_the_whole_common_period_has_no_common_mean():
    rows = _without_regimes(cand("thin"), "2025-26")
    assert pd_isnan(G.factor_effects(rows, "selection")["cells"].loc[0, "common_mean"])


def pd_isnan(x):
    import math
    return isinstance(x, float) and math.isnan(x)


def test_the_common_start_must_be_a_regime_start():
    with pytest.raises(ValueError, match="regime start"):
        G.factor_effects(cand("c"), "selection", common_start=date(2020, 9, 1))
    assert W.regime_starts()[0] == date(2019, 1, 1) and date(2020, 7, 1) in W.regime_starts()
    assert W.regimes_from(date(2020, 7, 1)) == ("2021", "2022bear", "2023-24", "2025-26")


def test_the_report_has_a_common_period_column(tmp_path):
    out = tmp_path / "grid_cp"
    write_run(out, baselines() + _two_families())
    md = R.render_report(R.collect_state(out, live=False))
    table = md.split("## Factor effects")[1].split("## Gate table")[0]
    assert "common period from 2020-07-01" in table
    header = next(line for line in table.splitlines() if line.startswith("| factor |"))
    assert "common mean" in header
    row = next(line for line in table.splitlines() if line.startswith("| family | xgb |"))
    assert row.rstrip(" |").endswith("+0.0200")


# ================================================================================ B8: confounding note, within-family tables
def _grid_like():
    return (baselines() + cand("lgbm_t", family="lgbm", groups=("t",), months=12, reg=0.03) +
            cand("lgbm_tm", family="lgbm", groups=("t", "m"), months=12, reg=0.05) +
            cand("lgbm_t24", family="lgbm", groups=("t",), months=24, reg=0.01) +
            cand("xgb_t", family="xgb", groups=("t",), months=12, reg=0.02))


def test_factor_effects_tabulates_every_other_factor_within_each_family():
    by = G.factor_effects(_grid_like(), "selection")["by_family"]
    assert list(by.index.names) == ["family", "factor", "level"]
    assert {"n_cells", "mean", "common_mean"} <= set(by.columns)
    assert by.loc[("lgbm", "groups", "t"), "n_cells"] == 2 and by.loc[("lgbm", "groups", "t+m"), "n_cells"] == 1
    assert by.loc[("lgbm", "groups", "t+m"), "mean"] == pytest.approx(0.05)
    assert by.loc[("lgbm", "train_months", "12"), "n_cells"] == 2
    assert by.loc[("xgb", "groups", "t"), "n_cells"] == 1
    assert "family" not in set(by.index.get_level_values("factor"))              # family x family is not a table


def test_the_report_states_the_confounding_of_the_core_grid_and_has_the_within_family_tables(tmp_path):
    out = tmp_path / "grid_conf"
    write_run(out, _grid_like())
    md = R.render_report(R.collect_state(out, live=False))
    assert "## Reading the factor tables" in md
    note = md.split("## Reading the factor tables")[1].split("\n## ")[0]
    for phrase in ("unbalanced", "target x family availability", "confounded with tsai", "horizon is fixed"):
        assert phrase in note, phrase
    assert "## Factors within each family" in md
    within = md.split("## Factors within each family")[1].split("\n## ")[0]
    assert "lgbm" in within and "xgb" in within
    row = next(line for line in within.splitlines() if line.startswith("| groups | t+m |"))
    assert "+0.0500" in row


# ================================================================================ B10: cleanups
def test_the_vestigial_noisy_warnings_hook_is_gone_and_the_validators_are_not_duplicated():
    from app.services import ml_models as M
    assert not hasattr(G, "NOISY_WARNINGS")
    assert G._is_int is M._is_int and G._is_real is M._is_real


def test_the_module_docstring_lists_every_family_of_the_cell_schema():
    doc = G.__doc__
    assert '"tsai-<arch>"' in doc and '"darts-<arch>"' in doc
    assert "shuffle_time_within_block" in doc or "within-block" in doc          # the timing gate takes the stricter null
    assert "the store path as written is part of it" not in doc and "warms the panels of the pending tasks" not in doc
    from app.services import ml_controls
    assert "Three permutation controls" in ml_controls.__doc__ and "Two permutation" not in ml_controls.__doc__
