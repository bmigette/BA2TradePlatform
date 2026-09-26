"""tools/ba2_cache_export.py --exclude: top-level cache entries are left out of the export,
and a pattern that matches nothing is refused instead of silently exporting everything."""
import importlib.util
import os

import pytest

_TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "ba2_cache_export.py")
_spec = importlib.util.spec_from_file_location("ba2_cache_export", _TOOL)
cache_export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cache_export)


@pytest.fixture
def cache_dir(tmp_path):
    for rel in ("_derived/ThetaDataOptionsProvider/AAPL/a.npy",
                "_stale-noquotes-X-20260903/AAPL/p.parquet",
                "ThetaDataOptionsProvider/AAPL/exp=2024-01-19/p.parquet",
                "fmp_history/mc_stock_split__AAPL.json",
                "top_level_file.json"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return str(tmp_path)


def _arcs(cache_dir, excluded):
    return sorted(arc for _full, arc in cache_export._iter_cache_files(cache_dir, excluded))


def test_no_exclude_exports_everything(cache_dir):
    assert len(_arcs(cache_dir, [])) == 5


def test_excluded_folders_and_globs_are_left_out(cache_dir):
    excluded = cache_export._excluded_top_level(cache_dir, ["_derived", "_stale-*"])
    assert excluded == ["_derived", "_stale-noquotes-X-20260903"]
    arcs = _arcs(cache_dir, excluded)
    assert not any("_derived" in a or "_stale-" in a for a in arcs)
    assert any(a.endswith("ThetaDataOptionsProvider/AAPL/exp=2024-01-19/p.parquet") for a in arcs)
    assert any(a.endswith("fmp_history/mc_stock_split__AAPL.json") for a in arcs)
    assert len(arcs) == 3


def test_only_top_level_names_are_matched(cache_dir):
    # A nested folder with an excluded name is NOT excluded: the option is top-level only.
    nested = os.path.join(cache_dir, "ThetaDataOptionsProvider", "_derived")
    os.makedirs(nested)
    open(os.path.join(nested, "keep.txt"), "w").close()
    arcs = _arcs(cache_dir, cache_export._excluded_top_level(cache_dir, ["_derived"]))
    assert any(a.endswith("ThetaDataOptionsProvider/_derived/keep.txt") for a in arcs)


def test_a_pattern_matching_nothing_is_refused(cache_dir):
    with pytest.raises(SystemExit, match="matches no top-level entry"):
        cache_export._excluded_top_level(cache_dir, ["_derivd"])


def test_a_path_pattern_is_refused(cache_dir):
    with pytest.raises(SystemExit, match="only top-level"):
        cache_export._excluded_top_level(cache_dir, ["ThetaDataOptionsProvider/AAPL"])
