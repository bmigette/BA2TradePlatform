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
    assert sorted(p.name for p in out.iterdir()) == ["coverage_report.csv", "coverage_report.parquet", "features.parquet",
                                                       "features.parquet.meta.json"]
    stored = pd.read_parquet(out / "features.parquet")
    direct = _build(work, GROUPS, symbols=SYMBOLS, sector_of=SECTOR_OF)
    pd.testing.assert_frame_equal(stored, direct, check_exact=True, check_freq=False)
    rep = pd.read_parquet(out / "coverage_report.parquet")
    pd.testing.assert_frame_equal(rep, F.coverage_report(direct, GROUPS), check_dtype=False)
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
    assert meta["symbols"] == SYMBOLS and set(res["refused"]) == {"HOLE", "STAL", "NOPE"}
    refused = meta["ensure"]["refused"]
    assert "consecutive" in refused["HOLE"]["reason"] and not refused["HOLE"]["transient"]
    assert refused["STAL"]["transient"] and "too short to count as ended" in refused["STAL"]["reason"]
    stored = pd.read_parquet(out / "features.parquet")
    assert set(stored.index.get_level_values("symbol")) == set(SYMBOLS)
    assert meta["membership"]["symbols"] == len(SYMBOLS)                        # filtered by the usable list, once


def test_a_stale_refusal_is_not_remembered_it_clears_after_a_cache_refresh(work, tmp_path):
    out = tmp_path / "store"
    s = _sessions()
    _write(work, "LATE5", _bars(s[:-5], 4))
    sector = {**SECTOR_OF, "LATE5": "Technology"}
    res = _ensure(work, out, symbols=SYMBOLS + ["LATE5"], sector_of=sector)
    assert "LATE5" in res["refused"] and _meta(out)["symbols"] == SYMBOLS
    _write(work, "LATE5", _bars(s, 4))                                           # the cache refresh brings the missing bars
    res = _ensure(work, out, symbols=SYMBOLS + ["LATE5"], sector_of=sector)
    assert res["status"] == "built" and "LATE5" not in res["refused"] and "LATE5" in _meta(out)["symbols"]


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


def test_the_launcher_command_builds_the_store(work, tmp_path, monkeypatch):
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
    symfile = tmp_path / "symbols.txt"
    symfile.write_text("aaa\nbbb\n", encoding="utf-8")
    out = tmp_path / "cli"
    rc = mod.main(["ml-features", "--symbols-file", str(symfile), "--start", str(FIRST), "--end", str(LAST),
                   "--groups", "t,mtf", "--out", str(out), "--chunk-size", "1"])
    assert rc == 0 and (out / "features.parquet").exists()
    assert _meta(out)["symbols"] == ["AAA", "BBB"] and set(_meta(out)["groups"]) == {"t", "mtf"}


def test_a_changed_coverage_definition_rebuilds_the_report(work, tmp_path, monkeypatch):
    out = tmp_path / "store"
    _ensure(work, out)
    assert _ensure(work, out)["status"] == "up_to_date"
    import dataclasses
    monkeypatch.setitem(F.GROUPS, "t", dataclasses.replace(F.GROUPS["t"], coverage_columns=("t_ret_1",)))
    assert _ensure(work, out)["status"] == "built"
