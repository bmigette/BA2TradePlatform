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
