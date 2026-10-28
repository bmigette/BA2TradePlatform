"""ensure_feature_store: the store build (Task 1.11)."""
import json
import os
import shutil

import pandas as pd
import pytest

from app.services import ml_features as F
from tests.test_ml_features import (FIRST, LAST, SECTOR_OF, _bars, _build, _sessions, _write, cache)  # noqa: F401

GROUPS = ["t", "mtf", "m", "x"]
SYMBOLS = ["AAA", "BBB", "CCC"]


@pytest.fixture(autouse=True)
def _small_cross_sections(monkeypatch):
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 2)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.0)


@pytest.fixture
def work(cache, tmp_path):
    """A private copy of the synthetic cache (a test may rewrite its files)."""
    root = tmp_path / "cache"
    shutil.copytree(cache, root)
    return root


def _ensure(root, out, symbols=SYMBOLS, groups=GROUPS, **kw):
    kw.setdefault("sector_overrides", {})
    kw.setdefault("sector_of", SECTOR_OF)
    kw.setdefault("chunk_size", 2)
    return F.ensure_feature_store(out, symbols, FIRST, LAST, groups, cache_root=root, **kw)


def _meta(out):
    return json.loads((out / "features.parquet.meta.json").read_text(encoding="utf-8"))


def test_the_store_equals_one_direct_build_and_writes_every_file(work, tmp_path):
    out = tmp_path / "store"
    res = _ensure(work, out)
    assert res["status"] == "built" and res["chunks"] == 2
    assert sorted(p.name for p in out.iterdir()) == ["column_coverage.csv", "column_coverage.parquet", "coverage_report.csv",
                                                       "coverage_report.parquet", "features.parquet", "features.parquet.meta.json"]
    stored = pd.read_parquet(out / "features.parquet")
    direct = _build(work, GROUPS, symbols=SYMBOLS, sector_of=SECTOR_OF)
    pd.testing.assert_frame_equal(stored, direct, check_exact=True, check_freq=False)
    rep = pd.read_parquet(out / "coverage_report.parquet")
    pd.testing.assert_frame_equal(rep, F.coverage_report(direct, GROUPS), check_dtype=False)
    cc = pd.read_parquet(out / "column_coverage.parquet")
    pd.testing.assert_frame_equal(cc, F.column_coverage(direct, GROUPS), check_dtype=False)
    assert ("m_rs_sector_20", 2023) in cc.index and ("t_rsi", 2023) in cc.index        # every column, every year
    meta = _meta(out)
    assert meta["symbols"] == SYMBOLS and set(meta["groups"]) == set(GROUPS) and meta["ensure"]["chunk_size"] == 2
    assert set(meta["source"]["daily"]) >= set(SYMBOLS) and meta["ensure"]["refused"] == {}


def test_rebuilds_only_when_an_input_changes(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    calls = []
    real = F.build_features
    monkeypatch.setattr(F, "build_features", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert _ensure(work, out)["status"] == "up_to_date" and not calls
    three = ["t", "mtf", "m"]
    assert _ensure(work, out, groups=three)["status"] == "built"                           # other groups
    assert _ensure(work, out, groups=three)["status"] == "up_to_date"
    assert _ensure(work, out, symbols=["AAA", "BBB"], groups=three)["status"] == "built"   # other symbols
    path = work / "AlpacaHistoryOHLCVProvider" / "AAA_1d.parquet"                          # a source fingerprint changes
    pd.read_parquet(path).to_parquet(path, index=False)
    os.utime(path, (1_900_000_000, 1_900_000_000))
    assert _ensure(work, out, symbols=["AAA", "BBB"], groups=three)["status"] == "built"
    assert _ensure(work, out, symbols=["AAA", "BBB"], groups=three)["status"] == "up_to_date"
    assert len(calls) > 0


def test_a_changed_membership_digest_rebuilds(work, tmp_path):
    out = tmp_path / "store"
    mem = pd.DataFrame(True, index=_sessions(FIRST, LAST), columns=SYMBOLS)
    _ensure(work, out, membership=mem)
    assert _ensure(work, out, membership=mem.copy())["status"] == "up_to_date"
    other = mem.copy()
    other.loc[other.index > pd.Timestamp("2023-06-01"), "CCC"] = False
    assert _ensure(work, out, membership=other)["status"] == "built"
    assert _meta(out)["membership"]["digest"] == F._membership_digest(other) != F._membership_digest(mem)


def test_refused_symbols_are_screened_once_recorded_with_reasons_and_leave_the_membership(work, tmp_path):
    out = tmp_path / "store"
    s = _sessions()
    _write(work, "HOLE", _bars(s, 3).drop(index=[300, 301, 302]))
    _write(work, "STAL", _bars(s[:-5], 4))
    cols = SYMBOLS + ["HOLE", "STAL", "NOPE"]
    mem = pd.DataFrame(True, index=_sessions(FIRST, LAST), columns=cols)
    sector = {**SECTOR_OF, "HOLE": "Technology", "STAL": "Technology", "NOPE": "Technology"}
    res = _ensure(work, out, symbols=cols, membership=mem, sector_of=sector)
    meta = _meta(out)
    assert meta["symbols"] == SYMBOLS and set(res["refused"]) == {"HOLE", "NOPE"} and set(res["transient"]) == {"STAL"}
    refused, transient = meta["ensure"]["refused"], meta["ensure"]["transient"]
    assert set(refused) == {"HOLE", "NOPE"} and "consecutive" in refused["HOLE"] and "no cached daily bars" in refused["NOPE"]
    assert set(transient) == {"STAL"} and "too short to count as ended" in transient["STAL"]     # a separate, informational list
    stored = pd.read_parquet(out / "features.parquet")
    assert set(stored.index.get_level_values("symbol")) == set(SYMBOLS)
    assert meta["membership"]["symbols"] == len(SYMBOLS)                        # filtered by the usable list, once


def test_a_stale_refusal_is_not_remembered_it_clears_after_a_cache_refresh(work, tmp_path):
    out = tmp_path / "store"
    s = _sessions()
    _write(work, "LATE5", _bars(s[:-5], 4))
    sector = {**SECTOR_OF, "LATE5": "Technology"}
    res = _ensure(work, out, symbols=SYMBOLS + ["LATE5"], sector_of=sector)
    assert "LATE5" in res["transient"] and "LATE5" not in res["refused"] and _meta(out)["symbols"] == SYMBOLS
    _write(work, "LATE5", _bars(s, 4))                                           # the cache refresh brings the missing bars
    res = _ensure(work, out, symbols=SYMBOLS + ["LATE5"], sector_of=sector)
    assert res["status"] == "built" and not res["transient"] and "LATE5" in _meta(out)["symbols"]
    assert _meta(out)["ensure"]["transient"] == {}


def test_load_feature_store_applies_the_coverage_mask(work, tmp_path):
    out = tmp_path / "store"
    _ensure(work, out)
    raw = F.load_feature_store(out, mask=False)
    masked = F.load_feature_store(out, mask=True, min_share=1.01)               # everything is under the threshold
    assert raw["t_rsi"].notna().any() and masked["t_rsi"].isna().all() and (masked["t_missing"] == 1.0).all()
    assert F.load_feature_store(out, mask=True)["t_rsi"].notna().any()
    assert raw.attrs["meta"]["symbols"] == SYMBOLS


def test_an_etf_store_declares_no_sector(work, tmp_path):
    out = tmp_path / "etf"
    res = F.ensure_feature_store(out, ["SPY", "QQQ"], FIRST, LAST, ["t", "mtf", "m"], cache_root=work,
                                 sector_overrides={"SPY": F.NO_SECTOR, "QQQ": F.NO_SECTOR}, chunk_size=1)
    assert res["status"] == "built"
    stored = pd.read_parquet(out / "features.parquet")
    assert stored.loc["SPY", "m_rs_sector_5"].isna().all() and stored.loc["SPY", "t_rsi"].notna().all()


def test_bad_arguments_refuse(work, tmp_path):
    with pytest.raises(ValueError, match="no usable symbol"):
        F.ensure_feature_store(tmp_path / "none", ["ZZZ"], FIRST, LAST, ["t"], cache_root=work)
    with pytest.raises(ValueError, match="unknown"):
        F.ensure_feature_store(tmp_path / "x", SYMBOLS, FIRST, LAST, ["t", "nope"], cache_root=work)


@pytest.fixture
def cli(work, tmp_path, monkeypatch):
    """The launcher module with the platform cache pointed at the private synthetic cache; returns run(symbols, *args)."""
    import importlib.util
    import sys
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.normpath(os.path.join(root_dir, "..", "ba2test_launcher.py"))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    spec = importlib.util.spec_from_file_location("ba2test_launcher_mlf", launcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from ba2_common import config as bc
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(work))
    counter = []

    def run(symbols, *args, groups="t,mtf"):
        symfile = tmp_path / f"symbols{len(counter)}.txt"
        symfile.write_text("\n".join(symbols) + "\n", encoding="utf-8")
        out = tmp_path / f"cli{len(counter)}"
        counter.append(1)
        argv = ["ml-features", "--symbols-file", str(symfile), "--start", str(FIRST), "--end", str(LAST), "--out", str(out),
                "--chunk-size", "1"] + (["--groups", groups] if groups else []) + list(args)
        return mod.main(argv), out
    run.mod = mod
    return run


def test_the_launcher_command_builds_the_store(cli):
    rc, out = cli(["aaa", "bbb"])
    assert rc == 0 and (out / "features.parquet").exists()
    assert _meta(out)["symbols"] == ["AAA", "BBB"] and set(_meta(out)["groups"]) == {"t", "mtf"}


def test_every_default_launcher_group_exists(cli):
    groups = [g for g in cli.mod.ML_FEATURES_DEFAULT_GROUPS.split(",")]
    assert groups == ["t", "mtf", "m", "x", "mac", "f", "ag", "at", "i", "s"]
    assert all(g in F.GROUPS for g in groups)
    assert set(F.GROUPS) == set(groups)                                  # and no registered group is left out


def test_the_launcher_membership_option(cli):
    rc, out = cli(["bbb", "ccc"], "--membership-top-n", "3", "--purpose", "dev", "--sector-override", "BBB=Financial Services",
                  "--sector-override", "CCC=Technology", groups="t,x")
    assert rc == 0
    rec = _meta(out)["membership"]
    assert rec["top_n"] == 3 and rec["purpose"] == "dev" and rec["symbols"] == 2      # AAA is hash-locked: not in a dev universe
    assert set(pd.read_parquet(out / "features.parquet").index.get_level_values("symbol")) == {"BBB", "CCC"}


def test_the_launcher_no_sector_and_override_options(cli):
    rc, out = cli(["spy", "qqq"], "--no-sector", groups="t,mtf,m")
    assert rc == 0 and _meta(out)["sectorless"] == ["QQQ", "SPY"]
    rc, out = cli(["aaa"], "--sector-override", "AAA=Financial Services", groups="t,mtf,m")
    assert rc == 0 and _meta(out)["sector_overrides"] == {"AAA": "Financial Services"}
    assert pd.read_parquet(out / "features.parquet").loc["AAA", "m_rs_sector_20"].notna().all()


def test_bad_launcher_sector_options_refuse(cli):
    with pytest.raises(SystemExit, match="SYM=Sector"):
        cli(["aaa"], "--sector-override", "AAA", groups="t")
    with pytest.raises(SystemExit, match="--no-sector"):
        cli(["aaa"], "--no-sector", "--sector-override", "AAA=Technology", groups="t")


def test_the_launcher_exits_non_zero_when_a_symbol_is_refused_permanently(cli, capsys):
    rc, out = cli(["aaa", "nope"])
    assert rc == 1 and _meta(out)["symbols"] == ["AAA"]                        # the rest is still built
    assert "refused NOPE" in capsys.readouterr().out


def test_the_launcher_min_share_is_stored_and_part_of_the_key(cli):
    rc, out = cli(["aaa", "bbb"], "--min-share", "0.9")
    assert rc == 0 and _meta(out)["ensure"]["min_share"] == 0.9


def test_a_changed_coverage_definition_rebuilds_the_report(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    assert _ensure(work, out)["status"] == "up_to_date"
    import dataclasses
    monkeypatch.setitem(F.GROUPS, "t", dataclasses.replace(F.GROUPS["t"], coverage_columns=("t_ret_1",)))
    assert _ensure(work, out)["status"] == "built"


def test_sectorless_symbols_are_recorded_and_exempt_in_the_stored_coverage(work, tmp_path):
    out = tmp_path / "etf"
    F.ensure_feature_store(out, ["SPY", "QQQ"], FIRST, LAST, ["t", "mtf", "m"], cache_root=work,
                           sector_overrides={"SPY": F.NO_SECTOR, "QQQ": F.NO_SECTOR}, chunk_size=1)
    assert _meta(out)["sectorless"] == ["QQQ", "SPY"]
    rep = pd.read_parquet(out / "coverage_report.parquet")
    assert rep.loc[("m", 2023), "share"] == 1.0                                  # the sector columns do not count for ETFs
    assert pd.read_parquet(out / "column_coverage.parquet").loc[("m_rs_sector_5", 2023), "share"] == 0.0


def test_a_code_change_rebuilds_without_force(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    assert _ensure(work, out)["status"] == "up_to_date"
    monkeypatch.setattr(F, "code_version", lambda: "0123456789abcdef")           # the feature code changed
    assert _ensure(work, out)["status"] == "built"
    assert _meta(out)["ensure"]["inputs"]["code"] == "0123456789abcdef"
    assert _ensure(work, out)["status"] == "up_to_date"


def test_the_code_version_hashes_the_feature_modules():
    import hashlib
    from pathlib import Path
    from app.services import ml_panel, ml_universe
    v = F.code_version()
    assert len(v) == 16 and v == F.code_version()
    h = hashlib.sha256()
    for mod in (F, ml_universe, ml_panel):
        h.update(Path(mod.__file__).read_bytes())
    assert v == h.hexdigest()[:16]


def test_min_share_and_the_package_versions_are_part_of_the_rebuild_key(work, tmp_path, monkeypatch):
    import ba2_common.version
    out = tmp_path / "store"
    _ensure(work, out)
    assert _ensure(work, out)["status"] == "up_to_date"
    assert _ensure(work, out, min_share=0.5)["status"] == "built"                  # another threshold, another store
    assert _ensure(work, out, min_share=0.5)["status"] == "up_to_date"
    keys = _meta(out)["ensure"]["inputs"]
    assert keys["min_share"] == 0.5 and set(keys["packages"]) == {"ba2_common", "ba2_providers", "ba2_experts"}
    for pkg in ("ba2_common", "ba2_providers", "ba2_experts"):
        mod = __import__(f"{pkg}.version", fromlist=["PACKAGE_VERSION"])
        monkeypatch.setattr(mod, "PACKAGE_VERSION", "2099.01.00001")
        assert _ensure(work, out, min_share=0.5)["status"] == "built", pkg
        monkeypatch.undo()
        assert _ensure(work, out, min_share=0.5)["status"] == "built"             # back to the real version: rebuilt again


# ---------------------------------------------------------------------------------------------- sources, atomicity, fast path
@pytest.fixture
def rich(work, tmp_path, monkeypatch):
    """A private cache with every kind of source: FMP files (statements, analysts, insiders), FRED, the metric store and a
    market-condition store. Returns (root, groups, ensure(out))."""
    import dataclasses  # noqa: F401
    from tests.test_ml_features_analyst import _grade_rows, _target_rows, write_fmp_rows
    from tests.test_ml_features_fund import _write_fmp
    from tests.test_ml_features_insider import _rows as insider_rows
    from tests.test_ml_features_macro import _synthetic_fred, point_fred_at
    from tests.test_ml_features_structure import write_mc_store
    for sym in ("AAA", "BBB"):
        write_fmp_rows(work, "analyst_grades", sym, _grade_rows(sym))
        write_fmp_rows(work, "price_target", sym, _target_rows(sym))
        write_fmp_rows(work, "insider_v2", sym, insider_rows(sym))
        _write_fmp(work, sym)
    _synthetic_fred(work)
    point_fred_at(monkeypatch, work)
    monkeypatch.setattr(F, "FRED_PINS", {})
    digests = write_mc_store(work, ("AAA", "BBB"))
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in digests.items()})
    groups = ["t", "x", "mac", "f", "ag", "at", "i", "s"]
    return work, groups, digests


def _rich_ensure(rich, out):
    root, groups, _ = rich
    return F.ensure_feature_store(out, ["AAA", "BBB"], FIRST, LAST, groups, cache_root=root, sector_overrides={},
                                  sector_of=SECTOR_OF, chunk_size=2)


def _touch(path, bump=100):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1_000_000_000))


def test_a_changed_source_of_every_kind_rebuilds(rich, tmp_path, monkeypatch):
    root, groups, digests = rich
    out = tmp_path / "store"
    assert _rich_ensure(rich, out)["status"] == "built"
    assert _rich_ensure(rich, out)["status"] == "up_to_date"
    changes = {
        "fmp_history (analyst grades)": lambda: _touch(root / "fmp_history" / "analyst_grades__AAA.json"),
        "fmp_history (insiders)": lambda: _touch(root / "fmp_history" / "insider_v2__BBB.json"),
        "fmp_history (statements)": lambda: _touch(root / "fmp_history" / "balance_sheet_annual__AAA.json"),
        "the metric store": lambda: _touch(next((root / "screener" / "metric_store_alpaca_20261022").glob("ym=*/*.parquet"))),
        "a FRED file": lambda: _touch(root / "fred" / "VIXCLS.json"),
        "a daily bar file": lambda: _touch(root / "AlpacaHistoryOHLCVProvider" / "SPY_1d.parquet"),
    }
    for name, change in changes.items():
        change()
        assert _rich_ensure(rich, out)["status"] == "built", name
        assert _rich_ensure(rich, out)["status"] == "up_to_date", name
    # the S manifest list changes: another (valid) store content
    from tests.test_ml_features_structure import write_mc_store
    other = write_mc_store(root, ("AAA", "BBB"), universe_tag="other")
    assert other != digests
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in other.items()})
    assert _rich_ensure(rich, out)["status"] == "built"
    assert _rich_ensure(rich, out)["status"] == "up_to_date"


def test_a_failed_build_leaves_the_previous_store_untouched_and_no_temp_directory(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    real = F.save_features

    def boom(df, path):
        real(df, path)                                                   # the files ARE written (into the temp directory) ...
        raise RuntimeError("disk full")                                 # ... and then the build dies
    monkeypatch.setattr(F, "save_features", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        _ensure(work, out, groups=["t", "mtf", "m"])
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("store")) == ["store"]


def test_the_store_is_swapped_in_whole_and_every_expected_file_is_required(work, tmp_path):
    out = tmp_path / "store"
    _ensure(work, out)
    assert set(F.STORE_FILES) == {p.name for p in out.iterdir()}
    for name in F.STORE_FILES:
        (out / name).unlink()
        res = _ensure(work, out)
        assert res["status"] == "built", name                            # an incomplete store is never "up to date"
        assert set(F.STORE_FILES) == {p.name for p in out.iterdir()}
    assert not [p for p in tmp_path.iterdir() if ".building-" in p.name or ".old-" in p.name]


def test_the_up_to_date_path_does_not_screen_the_symbols(work, tmp_path, monkeypatch):
    """A fingerprint-only fast path: unchanged requests and files are recognised before any symbol is loaded."""
    out = tmp_path / "store"
    _ensure(work, out)

    def boom(*a, **k):
        raise AssertionError("the up-to-date path screened the symbols")
    monkeypatch.setattr(F, "usable_symbols", boom)
    monkeypatch.setattr(F, "load_daily", boom)
    res = _ensure(work, out)
    assert res["status"] == "up_to_date" and res["symbols"] == SYMBOLS
    path = work / "AlpacaHistoryOHLCVProvider" / "BBB_1d.parquet"          # a changed file DOES go through the screening
    _touch(path)
    with pytest.raises(AssertionError, match="screened"):
        _ensure(work, out)


# ---------------------------------------------------------------------------------------------- typed stale, memo, meta merge
def test_a_stale_series_is_a_typed_error_not_a_message_match(tmp_path, work, monkeypatch):
    s = _sessions()
    _write(work, "STAL", _bars(s[:-5], 4))
    assert issubclass(F.StaleSeriesError, F.PanelDataError)
    with pytest.raises(F.StaleSeriesError):
        F.load_daily(["STAL"], FIRST, LAST, work)
    usable, permanent, transient = F.screen_symbols(["AAA", "STAL", "NOPE"], FIRST, LAST, work)
    assert usable == ["AAA"] and set(permanent) == {"NOPE"} and set(transient) == {"STAL"}
    # classification is by TYPE: a permanent refusal whose text happens to say "stale series" stays permanent
    real = F.load_daily

    def liar(symbols, *a, **k):
        if list(symbols) == ["AAA"]:
            raise F.PanelDataError("AAA: bad file (not a stale series, just wrong)")
        return real(symbols, *a, **k)
    monkeypatch.setattr(F, "load_daily", liar)
    usable, permanent, transient = F.screen_symbols(["AAA", "BBB"], FIRST, LAST, work)
    assert usable == ["BBB"] and set(permanent) == {"AAA"} and not transient


def test_the_store_lists_check_is_typed_too(work):
    with pytest.raises(F.StaleSeriesError):
        F._refuse_if_store_lists_ended({"AAA": "2020-01-01"}, work / "screener" / "metric_store_alpaca_20261022")


def test_chunked_builds_compute_the_shared_tables_once(rich, tmp_path, monkeypatch):
    """The macro table, the per-symbol X columns and the S manifests are memoised across chunks of one store build."""
    from ba2_common.core.market_condition_store import MarketConditionStore
    root, _groups, _ = rich
    calls = {"levels": 0, "own": 0, "manifest": 0}
    real_levels, real_own, real_read = F.macro_levels, F._own_columns, MarketConditionStore.read_manifest

    def levels(*a, **k):
        calls["levels"] += 1
        return real_levels(*a, **k)

    def own(d):
        calls["own"] += 1
        return real_own(d)

    def read(self, *a, **k):
        calls["manifest"] += 1
        return real_read(self, *a, **k)
    monkeypatch.setattr(F, "macro_levels", levels)
    monkeypatch.setattr(F, "_own_columns", own)
    monkeypatch.setattr(MarketConditionStore, "read_manifest", read)
    res = F.ensure_feature_store(tmp_path / "store", ["AAA", "BBB"], FIRST, LAST, ["x", "mac", "s"], cache_root=root,
                                 sector_overrides={}, sector_of=SECTOR_OF, chunk_size=1)
    assert res["chunks"] == 2
    assert calls["levels"] == len(F.MAC_SERIES)                  # once per series, not once per chunk
    assert calls["own"] == 2                                     # once per universe member (AAA, BBB), not per chunk
    assert calls["manifest"] == 2                                # one pinned manifest per profile, read once
    stored = pd.read_parquet(tmp_path / "store" / "features.parquet")
    direct = _build(root, ["x", "mac", "s"], symbols=["AAA", "BBB"], sector_of=SECTOR_OF)
    pd.testing.assert_frame_equal(stored.drop(columns="label_bar_filled"), direct.drop(columns="label_bar_filled"),
                                  check_exact=True, check_freq=False)


def test_the_meta_merges_required_context_and_context_symbols_across_chunks(work, tmp_path):
    out = tmp_path / "store"
    F.ensure_feature_store(out, ["AAA", "BBB"], FIRST, LAST, ["t", "m"], cache_root=work, sector_overrides={},
                           sector_of=SECTOR_OF, chunk_size=1)          # AAA: Technology (XLK), BBB: Financial Services (XLF)
    meta = _meta(out)
    assert {"XLK", "XLF"} <= set(meta["required_context"]) and {"XLK", "XLF"} <= set(meta["context_symbols"])
    assert "AAA" not in meta["context_symbols"] and set(F.MARKET_ETFS) <= set(meta["required_context"])


# ---------------------------------------------------------------------------------------------- swap failures, sweep, memo
def test_a_failed_swap_keeps_the_finished_build_directory(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    def boom(built, target):
        raise OSError("target is in use")
    monkeypatch.setattr(F, "_swap_in", boom)
    with pytest.raises(OSError, match=r"target is in use.*store\.building-"):
        _ensure(work, out, groups=["t", "mtf", "m"])
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before                  # the old store is intact
    kept = [p for p in tmp_path.iterdir() if ".building-" in p.name]
    assert len(kept) == 1 and set(F.STORE_FILES) == {p.name for p in kept[0].iterdir()}      # the finished build survives


def _dead_pid():
    """A pid that belonged to a process that has finished."""
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_stale_build_directories_of_dead_processes_are_swept(work, tmp_path):
    out = tmp_path / "store"
    dead = _dead_pid()
    stale = [tmp_path / f"store.building-{dead}", tmp_path / f"store.old-{dead}"]
    keep = [tmp_path / "other.building-99999", tmp_path / "store2.old-5", tmp_path / "unrelated"]
    for d in stale + keep:
        d.mkdir()
        (d / "x").write_text("x")
    _ensure(work, out)
    assert not any(d.exists() for d in stale)
    assert all(d.exists() for d in keep)                                               # only this store's leftovers


def test_a_store_lost_between_the_two_renames_is_restored_from_the_old_directory(work, tmp_path):
    out = tmp_path / "store"
    _ensure(work, out)
    (tmp_path / "store.old-4242").mkdir()
    for p in out.iterdir():
        shutil.move(str(p), str(tmp_path / "store.old-4242" / p.name))
    out.rmdir()                                                                         # the crash window: no store, an .old dir
    res = _ensure(work, out)
    assert set(F.STORE_FILES) == {p.name for p in out.iterdir()} and res["status"] == "up_to_date"


def test_the_x_cross_sections_are_computed_once_per_build(work, tmp_path, monkeypatch):
    calls = {"rank": 0, "z": 0}
    real_rank, real_z = F._cross_rank, F._cross_z
    monkeypatch.setattr(F, "_cross_rank", lambda *a, **k: (calls.__setitem__("rank", calls["rank"] + 1), real_rank(*a, **k))[1])
    monkeypatch.setattr(F, "_cross_z", lambda *a, **k: (calls.__setitem__("z", calls["z"] + 1), real_z(*a, **k))[1])
    res = _ensure(work, tmp_path / "store", groups=["t", "x"], chunk_size=1)
    assert res["chunks"] == 3
    assert calls["rank"] == len(F.X_RANKED) + 1 and calls["z"] == len(F.X_RANKED)       # 7 features + the momentum rank, ONCE
    direct = _build(work, ["t", "x"], symbols=SYMBOLS, sector_of=SECTOR_OF)
    stored = pd.read_parquet(tmp_path / "store" / "features.parquet")
    pd.testing.assert_frame_equal(stored.drop(columns="label_bar_filled"), direct.drop(columns="label_bar_filled"),
                                  check_exact=True, check_freq=False)


def test_the_request_key_is_checked_explicitly_not_through_get(work, tmp_path):
    """A store written before the request key existed (no "request" in its meta) is simply rebuilt."""
    out = tmp_path / "store"
    _ensure(work, out)
    meta = _meta(out)
    del meta["ensure"]["request"]
    (out / "features.parquet.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert _ensure(work, out)["status"] == "built"                                      # a store without the key is rebuilt once
    assert _ensure(work, out)["status"] == "up_to_date" and "request" in _meta(out)["ensure"]
    import inspect
    assert '.get("request")' not in inspect.getsource(F.ensure_feature_store)


# ---------------------------------------------------------------------------------------------- retry of a failed swap
def _failed_swap(work, out, monkeypatch, **kw):
    """Run a build whose swap fails; returns (kept build dir, the kwargs used)."""
    def boom(built, target):
        raise OSError("target is in use")
    with monkeypatch.context() as m:
        m.setattr(F, "_swap_in", boom)
        with pytest.raises(OSError, match="target is in use"):
            _ensure(work, out, **kw)
    kept = [p for p in out.parent.iterdir() if ".building-" in p.name]
    assert len(kept) == 1
    return kept[0]


def test_a_retry_swaps_the_kept_build_in_instead_of_rebuilding(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    kw = {"groups": ["t", "mtf", "m"]}
    kept = _failed_swap(work, out, monkeypatch, **kw)
    assert set(json.loads((kept / "features.parquet.meta.json").read_text())["groups"]) == {"t", "mtf", "m"}
    calls = []
    real = F.build_features
    monkeypatch.setattr(F, "build_features", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    res = _ensure(work, out, **kw)                                                      # the same request again
    assert res["status"] == "built" and res["recovered"] == str(kept) and not calls   # swapped in, nothing recomputed
    assert not kept.exists() and set(_meta(out)["groups"]) == {"t", "mtf", "m"}
    assert res["symbols"] == SYMBOLS and res["rows"] == _meta(out)["rows"]
    assert _ensure(work, out, **kw)["status"] == "up_to_date"
    assert not [p for p in tmp_path.iterdir() if ".building-" in p.name or ".old-" in p.name]


def test_a_kept_build_of_another_request_is_deleted_not_swapped_in(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    kept = _failed_swap(work, out, monkeypatch, groups=["t", "mtf", "m"])
    res = _ensure(work, out, groups=["t"])                                              # a different request
    assert res["status"] == "built" and "recovered" not in res and not kept.exists()
    assert set(_meta(out)["groups"]) == {"t"}


def test_a_build_directory_of_a_live_process_is_left_alone_a_dead_ones_is_deleted(work, tmp_path):
    import subprocess
    import sys
    out = tmp_path / "store"
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mine = [tmp_path / f"store.building-{live.pid}", tmp_path / f"store.old-{live.pid}"]
        gone = [tmp_path / f"store.building-{_dead_pid()}"]
        for d in mine + gone:
            d.mkdir()
            (d / "x").write_text("x")
        _ensure(work, out)
        assert all(d.exists() for d in mine)                 # another process may be building / swapping right now
        assert not any(d.exists() for d in gone)
    finally:
        live.kill()
        live.wait()
    _ensure(work, out, groups=["t"])                          # the process is dead now: its leftovers go
    assert not any(d.exists() for d in mine)


def test_a_kept_complete_build_is_not_restored_over_an_up_to_date_store(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    kept = _failed_swap(work, out, monkeypatch, groups=["t", "mtf", "m"])
    _ensure(work, out, groups=["t", "mtf", "m"])              # recovers
    _ensure(work, out)                                         # back to the original request: rebuilt, nothing stale around
    assert not [p for p in tmp_path.iterdir() if ".building-" in p.name]
    assert not kept.exists()
