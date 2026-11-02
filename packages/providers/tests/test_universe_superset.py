"""The static universe of a screener job is the true SUPERSET of what any genome can select.

Regression for the 2026-10-07 universe mismatch: the launcher froze the union of the loosest filters
WITH ``max_stocks=50`` (market-cap sorted) applied after the filters, so a genome with tighter filters
selected names the loosest top-50 never contained, and those picks were silently untradable.
"""
import pandas as pd
import pytest

from ba2_providers.screener import metric_store as ms
from ba2_providers.screener import universe_superset as us

SCANS = ["2023-03-04", "2023-03-11", "2023-03-18", "2023-03-25"]   # Saturdays (Friday-close scans)
N = 60


def _store(tmp_path, rows):
    path = str(tmp_path / "ms")
    ms.write_partitions(path, pd.DataFrame(rows))
    ms.clear_store_memo()
    return ms.load_store(path)


def _band_rows(hidden_rvol=2.4):
    """60 names in the 2-10B band, cap descending S00..S59. All fail an rvol>=1.5 gate except S55, which
    sits at cap rank 56 -- below a top-50-by-cap cut taken before any rvol filter."""
    rows = []
    for d in SCANS:
        for i in range(N):
            rows.append({"date": d, "symbol": f"S{i:02d}", "market_cap": 9.9e9 - i * 1e8, "price": 20.0,
                         "close": 20.0, "volume": 2e6, "sector": "T",
                         "relative_volume": hidden_rvol if i == 55 else 0.5,
                         "price_drop_pct": 20.0, "price_drop_pct_22": 20.0})
    return rows


def _mid_job():
    opt, base = us.apply_cap_band(us.SCREENER_OPT, {}, "mid")
    return base, us.gene_ranges_from_opt(opt)


# the genome whose tight filters pick the hidden name
GENOME = {"market_cap_min": 2e9, "market_cap_max": 1e10, "relative_volume_min": 1.5, "price_drop_pct": 13.0,
          "price_drop_days": 22, "max_stocks": 20}


def _old_rule_union(df, start, end, base, ranges):
    """The pre-fix launcher rule, verbatim: loosest values AND the gene's max_stocks ceiling."""
    loosest = dict(base)
    loosest.update({"market_cap_min": ranges["screener_market_cap_min"]["min"],
                    "relative_volume_min": ranges["screener_relative_volume_min"]["min"],
                    "price_drop_pct": ranges["screener_price_drop_pct"]["min"],
                    "weinstein_stage2_only": 0, "max_stocks": ranges["screener_max_stocks"]["max"]})
    return ms.screened_symbol_union(df, start, end, loosest)


def test_top50_cut_hides_a_name_a_tighter_genome_selects(tmp_path):
    df = _store(tmp_path, _band_rows())
    base, ranges = _mid_job()
    start, end = "2023-03-06", "2023-03-24"

    old = _old_rule_union(df, start, end, base, ranges)
    new = us.static_universe(df, start, end, base, ranges, intraday=True)

    assert "S55" not in old                       # the bug: cut by the top-50-by-cap before any filter
    assert len(old) == 50
    assert "S55" in new and len(new) == N         # the fix: every name passing the loosest FILTERS

    sel = us.gate_selections(df, start, end, GENOME, intraday=True)
    assert {s for v in sel.values() for s in v} == {"S55"}
    assert us.count_outside(sel, old)["outside_static_universe"] == len(sel)      # untradable before
    assert us.count_outside(sel, new)["outside_static_universe"] == 0             # tradable now


def test_superset_does_not_depend_on_ordering_or_cut_genes(tmp_path):
    df = _store(tmp_path, _band_rows())
    base, ranges = _mid_job()
    ref = us.static_universe(df, "2023-03-06", "2023-03-24", base, ranges, intraday=True)
    for mx in ({"min": 1, "max": 5}, {"min": 10, "max": 500}):
        r = dict(ranges)
        r["screener_max_stocks"] = {**ranges["screener_max_stocks"], **mx}
        r["screener_sort_metric"] = {"min": 0, "max": 2, "step": 1, "type": "int", "optimize": True}
        assert us.static_universe(df, "2023-03-06", "2023-03-24", base, r, intraday=True) == ref
    # and a base-level max_stocks / sort_metric is stripped too
    assert us.static_universe(df, "2023-03-06", "2023-03-24", {**base, "max_stocks": 3, "sort_metric": "x"},
                              ranges, intraday=True) == ref


def test_unknown_gene_or_unknown_range_refuses_the_launch(tmp_path):
    base, ranges = _mid_job()
    with pytest.raises(us.ScreenerUniverseError, match="no declared role"):
        us.loosest_filter_variants(base, {**ranges, "screener_mystery": {"min": 0, "max": 1}})
    with pytest.raises(us.ScreenerUniverseError, match="no 'max'"):
        us.loosest_filter_variants(base, {"screener_relative_volume_min": {"min": 0.0}})
    with pytest.raises(us.ScreenerUniverseError, match="min 5.0 > max 1.0"):
        us.loosest_filter_variants(base, {"screener_relative_volume_min": {"min": 5.0, "max": 1.0}})
    with pytest.raises(us.ScreenerUniverseError, match="not a number"):
        us.loosest_filter_variants(base, {"screener_price_drop_pct": {"min": "x", "max": 1}})


def test_loosest_values_follow_the_declared_ranges_by_role():
    (v,) = us.loosest_filter_variants(
        {"market_cap_max": 1e10, "max_stocks": 7},
        {"screener_market_cap_min": {"min": 2e9, "max": 1e10},        # lower bound: the minimum
         "screener_relative_volume_min": {"min": 0.0, "max": 3.0},    # range reaching 0 = filter off
         "screener_price_drop_pct": {"min": 4.0, "max": 25.0},        # lower bound with a positive floor
         "screener_weinstein_stage2_only": {"min": 0, "max": 1},      # flag range includes 0 = off
         "screener_max_stocks": {"min": 10, "max": 50},               # ordering: never in the superset
         "screener_price_max": {"min": 0, "max": 500}})               # upper bound reaching 0 = off
    assert v == {"market_cap_max": 1e10, "market_cap_min": 2e9, "relative_volume_min": 0.0,
                 "price_drop_pct": 4.0, "weinstein_stage2_only": 0}
    # a flag whose range excludes 0 is ON at its loosest; an upper bound with a positive floor takes the max
    (v,) = us.loosest_filter_variants({}, {"screener_weinstein_stage2_only": {"min": 1, "max": 1},
                                           "screener_price_max": {"min": 50, "max": 500}})
    assert v == {"weinstein_stage2_only": 1, "price_max": 500.0}


def test_enforced_drop_floor_is_a_union_over_every_window_column(tmp_path):
    """With a positive drop floor the threshold reads a DIFFERENT precomputed column per window Y, so the
    loosest set is the union over every Y of the gene's range (not one arbitrary column)."""
    ranges = {"screener_price_drop_pct": {"min": 5.0, "max": 25.0},
              "screener_price_drop_days": {"min": 2, "max": 4, "step": 1}}
    variants = us.loosest_filter_variants({}, ranges)
    assert [x["price_drop_days"] for x in variants] == [2, 3, 4]
    rows = []
    for d in SCANS:
        rows += [{"date": d, "symbol": s, "market_cap": 5e9, "price": 10.0, "close": 10.0, "volume": 1e6,
                  "sector": "T", "relative_volume": 1.0, "price_drop_pct": 0.0,
                  "price_drop_pct_2": a, "price_drop_pct_3": b, "price_drop_pct_4": c}
                 for s, (a, b, c) in {"W2": (9, 0, 0), "W3": (0, 9, 0), "W4": (0, 0, 9), "NONE": (0, 0, 0)}.items()]
    df = _store(tmp_path, rows)
    got = us.static_universe(df, "2023-03-06", "2023-03-24", {}, ranges, intraday=True)
    assert got == ["W2", "W3", "W4"]


def test_window_is_the_scans_the_gate_can_resolve_to_not_last_scan_le_start(tmp_path):
    """A Wednesday-dated scan on the first day of the run is NOT visible at that day's first intraday
    decision (the previous scan is); the old window started AT it and missed the previous scan."""
    rows = []
    for d, syms in {"2023-03-01": ["PREV"], "2023-03-08": ["ONSTART"], "2023-03-15": ["MID"]}.items():
        for s in syms:
            rows.append({"date": d, "symbol": s, "market_cap": 5e9, "price": 10.0, "close": 10.0,
                         "volume": 1e6, "sector": "T", "relative_volume": 1.0, "price_drop_pct": 0.0})
        rows.append({"date": d, "symbol": "ALWAYSOUT", "market_cap": 1e6, "price": 1.0, "close": 1.0,
                     "volume": 1e6, "sector": "T", "relative_volume": 1.0, "price_drop_pct": 0.0})
    df = _store(tmp_path, rows)
    settings = {"market_cap_min": 1e9}
    old = ms.screened_symbol_union(df, "2023-03-08", "2023-03-15", settings)
    intraday = ms.screened_symbol_union_visible(df, "2023-03-08", "2023-03-15", settings, intraday=True)
    daily = ms.screened_symbol_union_visible(df, "2023-03-08", "2023-03-15", settings, intraday=False)
    assert "PREV" not in old                                   # the start-day gap of the old window
    assert intraday == ["MID", "ONSTART", "PREV"]              # 03-08 09:30 sees the 03-01 scan
    assert daily == ["MID", "ONSTART"]                         # a daily clock reads the 03-08 scan itself
    assert ms.visible_scan_window(df, "2023-03-08", "2023-03-15", intraday=True) == ("2023-03-01", "2023-03-15")
    assert ms.visible_scan_window(df, "2000-01-01", "2000-01-02", intraday=True) is None


def test_cap_band_helper_matches_the_launcher_definition():
    opt, base = us.apply_cap_band(us.SCREENER_OPT, {"k": 1}, "small")
    assert base == {"k": 1, "market_cap_max": 2e9}
    assert opt["screener_market_cap_min"] == {"min": 5e7, "max": 2e9, "step": 1e8, "type": "float",
                                              "optimize": True}
    assert us.SCREENER_OPT["screener_market_cap_min"]["min"] == 2e9            # the canonical dict is untouched
    opt, base = us.apply_cap_band(us.SCREENER_OPT, {}, "large")
    assert base == {}                                                           # no ceiling for the large band
    assert us.apply_cap_band(us.SCREENER_OPT, {"k": 1}, None) == (us.SCREENER_OPT, {"k": 1})


def test_cached_ohlcv_missing_checks_every_interval(tmp_path):
    (tmp_path / "AAA_5min.parquet").write_bytes(b"")
    (tmp_path / "AAA_1d.parquet").write_bytes(b"")
    (tmp_path / "BBB_5min.parquet").write_bytes(b"")                  # no daily
    (tmp_path / "CCC_1d.parquet").write_bytes(b"")                    # no 5min
    (tmp_path / "BRK_B_5min.parquet").write_bytes(b"")                # '-' sanitised to '_'
    (tmp_path / "BRK_B_1d.parquet").write_bytes(b"")
    assert us.cached_ohlcv_missing(["AAA", "BBB", "CCC", "DDD", "BRK-B"], ["5min", "1d"], str(tmp_path)) == [
        "BBB", "CCC", "DDD"]
