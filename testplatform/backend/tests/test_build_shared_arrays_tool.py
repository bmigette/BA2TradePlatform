"""``tools/build_shared_arrays.py`` -- the derived-cache prewarm/sweep tool (Task 7 of
docs/plans/2026-09-14-shared-arrays-across-workers.md).

WHAT THE TOOL IS FOR, AND THEREFORE WHAT THESE TESTS PIN. ``build_or_open`` serialises cold
builders per KEY, not per host, so 24 workers starting cold on 24 different underlyings run 24
concurrent multi-GB builds -- the OOM the shared-array plan exists to remove, relocated to cold
start. The tool builds the set ONCE, at a bounded ``--jobs``, before a grid launches. And it is
the only caller of ``sweep()`` anywhere: nothing collects an obsolete KEY at runtime.

So the properties under test are: a cold run BUILDS and a warm one OPENS WITHOUT RE-READING THE
PARQUET (the whole point -- an "opened" that silently re-parsed would prewarm nothing); ``--sweep``
removes an aged key and a stale-``ARRAYS_VERSION`` key while leaving the current one alone;
``--dry-run`` writes nothing; and ``sqlite`` is refused rather than quietly doing nothing.

NEVER AGAINST THE OPERATOR'S CACHE. Every test here pins the option root with
``BACKTEST_OPTIONS_PARQUET_ROOT`` and the OHLCV root with a ``CACHE_FOLDER`` monkeypatch, both
under ``tmp_path``; a test that forgot would build the REAL derived tree (tens of GB) as a side
effect of running the suite.

Run from the backend dir:
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe -m pytest tests/test_build_shared_arrays_tool.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import shared_arrays as SA
from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar

from app.services.backtest import parquet_options_provider as pq
from app.services.backtest import price_source as ps

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tools" / "build_shared_arrays.py"


def _tool():
    """The tool as a module. Imported by PATH (it is a script, not an installed package), and
    re-executed per call so a test cannot inherit another's module-level state."""
    spec = importlib.util.spec_from_file_location("build_shared_arrays", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_shared_arrays"] = m
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------------------------
# Fixtures. The option store is written through the REAL OptionHistoryParquetStore (the layout
# under test is the one tools/warm_options_history.py produces); the OHLCV tree is the native
# CACHE_FOLDER/<ProviderClassName>/<SYM>_<interval>.parquet `ba2-test fetch-cache` writes.
# --------------------------------------------------------------------------------------------
_BARS = {
    date(2023, 1, 20): [
        ("{u}230120C00100000", date(2023, 1, 3), 5.0, 5.4, 4.9, 5.2, 110, 900, 0.31),
        ("{u}230120C00100000", date(2023, 1, 5), 6.0, 6.4, 5.9, 6.2, 120, 950, 0.33),
        ("{u}230120P00100000", date(2023, 1, 3), 4.0, 4.4, 3.9, 4.2, 40, 500, 0.29),
    ],
    date(2023, 2, 17): [
        ("{u}230217C00110000", date(2023, 1, 10), 3.0, 3.4, 2.9, 3.2, 55, 700, 0.28),
    ],
}


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """A two-underlying TastyTrade-shaped parquet tree, pinned as THE option root via the env
    override the tool resolves through."""
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    root = str(tmp_path / "TastyTradeOptionsProvider")
    store = OptionHistoryParquetStore(root=root)
    for under in ("ZZ", "YY"):
        for expiry, rows in _BARS.items():
            store.write_partition(
                under, expiry,
                [OptionEodBar(occ_symbol=occ.format(u=under), bar_date=d, open=o, high=h,
                              low=lo, close=c, volume=v, open_interest=oi, iv=iv)
                 for (occ, d, o, h, lo, c, v, oi, iv) in rows],
                start=date(2023, 1, 1), end=date(2023, 3, 31))
    monkeypatch.setenv("BACKTEST_OPTIONS_PARQUET_ROOT", root)
    return root


class FMPOHLCVProvider:  # noqa: D401 - the NAME is load-bearing (native cache dir == class name)
    """A network-backed provider stand-in; ``cached_only=True`` must never call through."""

    def get_provider_name(self) -> str:
        return "fmp"

    def get_ohlcv_data(self, *a, **k):  # pragma: no cover - hermetic mode must never call this
        raise AssertionError("a prewarm run must not call the live provider")


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    """``CACHE_FOLDER/FMPOHLCVProvider/<SYM>_1d.parquet`` for AAA/BBB, with CACHE_FOLDER bound to
    tmp_path so neither the build nor the sweep can reach the operator's real cache."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    dates = pd.bdate_range("2024-01-01", periods=300)
    n = len(dates)
    d = tmp_path / "FMPOHLCVProvider"
    d.mkdir(exist_ok=True)
    for i, s in enumerate(("AAA", "BBB")):
        pd.DataFrame({
            "Date": dates,
            "Open": np.arange(n, dtype=float) + i,
            "High": np.arange(n, dtype=float) + i + 1.0,
            "Low": np.arange(n, dtype=float) + i - 1.0,
            "Close": np.arange(n, dtype=float) + i + 0.5,
            "Volume": np.arange(n, dtype=float) * 10.0,
        }).to_parquet(d / f"{s}_1d.parquet", index=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Every cache the tool touches is a PROCESS global, and the shared path is the production
    default -- an operator's ambient ``BA2_SHARED_ARRAYS=0`` would turn every assertion below
    into a test of the escape hatch."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    pq.clear_worker_parquet_options_cache()
    yield
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    pq.clear_worker_parquet_options_cache()


@pytest.fixture
def read_counter(monkeypatch):
    """Counts ``OptionHistoryParquetStore.read_underlying`` -- the parquet PARSE a warm derived
    set must skip. NOT ``_load_raw_underlying``, which runs on every open by design."""
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    calls: list = []
    original = OptionHistoryParquetStore.read_underlying

    def spy(self, underlying, parts=None):
        calls.append(underlying)
        return original(self, underlying, parts)

    monkeypatch.setattr(OptionHistoryParquetStore, "read_underlying", spy)
    return calls


def _done_dir(d: Path, marker_age_s: float = 0.0) -> Path:
    """A published signature directory written by hand, so --sweep can be aimed at it."""
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "close.npy", np.arange(64, dtype=float), allow_pickle=False)
    (d / SA.DONE_MARKER).write_text(
        json.dumps({"arrays": ["close"], "schema": SA.SCHEMA_VERSION}), encoding="utf-8")
    if marker_age_s:
        old = time.time() - marker_age_s
        os.utime(d / SA.DONE_MARKER, (old, old))
    return d


# --------------------------------------------------------------------------------------------
# 1. Options: cold builds, warm opens, and the warm open does NOT touch the parquet.
# --------------------------------------------------------------------------------------------
def test_options_cold_run_builds_and_warm_run_opens_without_re_reading_parquet(
        store_root, read_counter, capsys):
    tool = _tool()
    derived = Path(SA.derived_root_for(store_root))
    assert not derived.exists()

    assert tool.main(["--options-store", "tastytrade", "--symbols", "ZZ", "--jobs", "1"]) == 0

    out = capsys.readouterr().out
    assert "ZZ" in out and "built" in out
    assert "1 built / 0 opened / 0 empty" in out
    key = f"u_ZZ.v{pq._RawUnderlying.ARRAYS_VERSION}"
    sigs = [p for p in (derived / key).iterdir() if (p / SA.DONE_MARKER).is_file()]
    assert len(sigs) == 1 and (sigs[0] / "close.npy").is_file()
    assert read_counter == ["ZZ"], "the cold run is the one that parses the parquet"

    del read_counter[:]
    pq.clear_worker_parquet_options_cache()
    assert tool.main(["--options-store", "tastytrade", "--symbols", "ZZ", "--jobs", "1"]) == 0
    out = capsys.readouterr().out
    assert "0 built / 1 opened / 0 empty" in out
    assert read_counter == [], "a warm derived set must be MAPPED, never re-parsed"


def test_an_underlying_with_no_partitions_is_reported_empty_not_failed(store_root, capsys):
    """A universe file outruns the warm-up all the time. That is a coverage fact to report, not
    an error to exit non-zero on -- and nothing is written for it."""
    tool = _tool()
    assert tool.main(["--options-store", "tastytrade", "--symbols", "ZZ,NOPE",
                      "--jobs", "1"]) == 0
    out = capsys.readouterr().out
    assert "1 built / 0 opened / 1 empty" in out
    assert not (Path(SA.derived_root_for(store_root)) / "u_NOPE.v1").exists()


def test_a_symbol_whose_build_raises_is_reported_and_the_run_exits_non_zero(
        store_root, monkeypatch, capsys):
    """One bad underlying must not abandon the other 97, and must not let a grid launch on a
    half-warm tree believing it is warm."""
    tool = _tool()
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    original = OptionHistoryParquetStore.read_underlying

    def boom(self, underlying, parts=None):
        if underlying.upper() == "ZZ":
            raise RuntimeError("corrupt partition")
        return original(self, underlying, parts)

    monkeypatch.setattr(OptionHistoryParquetStore, "read_underlying", boom)
    assert tool.main(["--options-store", "tastytrade", "--symbols", "ZZ,YY", "--jobs", "1"]) != 0
    out = capsys.readouterr().out
    assert "corrupt partition" in out
    assert "1 built / 0 opened / 0 empty" in out and "1 error" in out


# --------------------------------------------------------------------------------------------
# 2. OHLCV: one preload in the parent builds every entry; a second run opens them.
# --------------------------------------------------------------------------------------------
def _ohlcv_argv(symbols="AAA,BBB"):
    return ["--ohlcv-provider", "FMPOHLCVProvider", "--symbols", symbols,
            "--interval", "1d", "--start", "2024-03-01", "--end", "2024-12-31",
            "--warmup-days", "30"]


def test_ohlcv_cold_run_builds_every_symbol_and_a_warm_run_opens_them(
        native_tree, monkeypatch, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "_ohlcv_inner", lambda name: FMPOHLCVProvider())

    assert tool.main(_ohlcv_argv()) == 0
    out = capsys.readouterr().out
    assert "2 built / 0 opened / 0 empty" in out
    derived = Path(SA.derived_root_for(str(native_tree / "FMPOHLCVProvider")))
    assert len([p for p in derived.rglob(SA.DONE_MARKER)]) == 2
    assert any(derived.rglob("c.npy"))

    assert tool.main(_ohlcv_argv()) == 0
    assert "0 built / 2 opened / 0 empty" in capsys.readouterr().out


def test_a_tolerated_uncached_ohlcv_symbol_is_named_and_writes_no_derived_entry(
        native_tree, monkeypatch, capsys):
    """preload's missing-symbol tolerance decides what happens to a symbol with no parquet; the
    tool's job is to SAY which symbols it could not warm, not to invent a second policy."""
    tool = _tool()
    monkeypatch.setattr(tool, "_ohlcv_inner", lambda name: FMPOHLCVProvider())
    monkeypatch.setattr(ps, "_MISSING_SYMBOL_MAX_FRAC", 0.9)   # tolerate, as a big universe does

    assert tool.main(_ohlcv_argv("AAA,GHOST")) == 0

    out = capsys.readouterr().out
    assert "DROPPED 1 uncached symbol(s)" in out and "GHOST" in out
    assert "1 built / 0 opened / 1 empty" in out
    derived = Path(SA.derived_root_for(str(native_tree / "FMPOHLCVProvider")))
    assert not any(p.name.startswith("u_GHOST_") for p in derived.iterdir())


def test_an_untolerated_ohlcv_miss_exits_non_zero(native_tree, monkeypatch, capsys):
    """Past the tolerance preload raises BacktestCacheMiss -- and so must the prewarm, or a grid
    launches believing a universe it cannot actually run is warm."""
    tool = _tool()
    monkeypatch.setattr(tool, "_ohlcv_inner", lambda name: FMPOHLCVProvider())

    assert tool.main(_ohlcv_argv("AAA,GHOST")) != 0

    out = capsys.readouterr().out
    assert "BacktestCacheMiss" in out and "1 error(s)" in out


# --------------------------------------------------------------------------------------------
# 3. --sweep: the KEY-level collector nothing else has.
# --------------------------------------------------------------------------------------------
def test_sweep_removes_aged_and_stale_version_keys_and_keeps_the_current_one(
        store_root, capsys):
    tool = _tool()
    derived = Path(SA.derived_root_for(store_root))
    v = pq._RawUnderlying.ARRAYS_VERSION
    aged = _done_dir(derived / f"u_OLD.v{v}" / "sig1", marker_age_s=20 * 86400)
    stale_version = _done_dir(derived / "u_X.v0" / "sig1")
    current = _done_dir(derived / f"u_ZZ.v{v}" / "sig1")

    assert tool.main(["--options-store", "tastytrade", "--sweep",
                      "--sweep-max-age-days", "14"]) == 0

    out = capsys.readouterr().out
    assert not aged.parent.exists(), "a key nothing has opened in 20 days is garbage"
    assert not stale_version.parent.exists(), "a key below the reader's ARRAYS_VERSION is garbage"
    assert current.parent.is_dir(), "the CURRENT key must survive its own sweep"
    assert "2 key(s) removed" in out
    # A reclaimed figure that is always "0.0 B" would pass a substring check while reporting
    # nothing -- the number is the only evidence the eviction actually freed the disk.
    reclaimed = re.search(r"([\d.]+) (B|KB|MB|GB|TB) reclaimed", out)
    assert reclaimed and float(reclaimed.group(1)) > 0, out


def test_sweep_does_not_remove_a_key_that_is_old_but_still_in_daily_use(store_root, capsys):
    """The age rule is age-since-LAST-USE. ``_try_open`` restamps the marker on every successful
    open, so a key built months ago and mapped by every trial this morning is not garbage -- a
    build-time rule would delete the hottest key on the host and bill the next grid for it."""
    tool = _tool()
    derived = Path(SA.derived_root_for(store_root))
    key_dir = derived / f"u_ZZ.v{pq._RawUnderlying.ARRAYS_VERSION}"
    pq._load_raw_underlying(store_root, "ZZ")                 # cold: builds
    marker = next(key_dir.rglob(SA.DONE_MARKER))
    old = time.time() - 90 * 86400
    os.utime(marker, (old, old))

    pq._load_raw_underlying(store_root, "ZZ")                 # warm: OPENS, and restamps
    assert time.time() - marker.stat().st_mtime < 60

    assert tool.main(["--options-store", "tastytrade", "--sweep",
                      "--sweep-max-age-days", "14"]) == 0
    assert key_dir.is_dir(), "a key opened today must survive an age sweep"
    assert "0 key(s) removed" in capsys.readouterr().out


def test_sweep_of_the_ohlcv_root_uses_the_bar_stores_own_version(native_tree, capsys):
    """The two consumers carry SEPARATE ARRAYS_VERSIONs and spell the version differently in the
    key (``.v<n>`` vs ``_v<n>_<winsig>``); sweeping one root with the other's rule would either
    spare garbage or delete a live set."""
    tool = _tool()
    derived = Path(SA.derived_root_for(str(native_tree / "FMPOHLCVProvider")))
    v = ps.ARRAYS_VERSION
    stale = _done_dir(derived / "u_AAA_1d_2024-01-31_2024-12-31_v0_abc123def456" / "sig1")
    current = _done_dir(derived / f"u_AAA_1d_2024-01-31_2024-12-31_v{v}_abc123def456" / "sig1")

    assert tool.main(["--ohlcv-provider", "FMPOHLCVProvider", "--sweep"]) == 0

    assert not stale.parent.exists()
    assert current.parent.is_dir()
    assert "1 key(s) removed" in capsys.readouterr().out


def test_sweep_also_runs_the_stores_own_within_key_collection(store_root, capsys):
    """``--sweep`` is both passes: ``sweep()`` for superseded signatures WITHIN a key, then the
    key-level pass. A superseded signature under a current key has no other collector either."""
    tool = _tool()
    derived = Path(SA.derived_root_for(store_root))
    key = derived / f"u_ZZ.v{pq._RawUnderlying.ARRAYS_VERSION}"
    old_sig = _done_dir(key / "sig_old", marker_age_s=10_000)
    new_sig = _done_dir(key / "sig_new")

    assert tool.main(["--options-store", "tastytrade", "--sweep"]) == 0

    assert not old_sig.exists() and new_sig.is_dir()
    assert "1 superseded/orphan dir(s)" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# 4. --dry-run touches nothing. 5. sqlite is refused.
# --------------------------------------------------------------------------------------------
def test_dry_run_writes_nothing_at_all(store_root, native_tree, monkeypatch, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "_ohlcv_inner", lambda name: FMPOHLCVProvider())
    derived = Path(SA.derived_root_for(store_root))
    aged = _done_dir(derived / "u_OLD.v1" / "sig1", marker_age_s=20 * 86400)

    # ONE symbol list feeds both halves (there is one --symbols), so the list names an option
    # underlying and an OHLCV symbol and each half reports on the one it knows.
    assert tool.main(["--options-store", "tastytrade", *_ohlcv_argv("ZZ,AAA"),
                      "--sweep", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out and "ZZ" in out and "AAA" in out
    assert aged.is_dir(), "--dry-run must not sweep"
    assert not (derived / "u_ZZ.v1").exists(), "--dry-run must not build"
    ohlcv_derived = Path(SA.derived_root_for(str(native_tree / "FMPOHLCVProvider")))
    assert not ohlcv_derived.exists()


def test_sqlite_is_refused_with_a_reason(capsys):
    tool = _tool()
    with pytest.raises(SystemExit) as exc:
        tool.main(["--options-store", "sqlite", "--symbols", "ZZ"])
    assert exc.value.code != 0
    assert "no derived" in capsys.readouterr().err.lower()


def test_a_run_that_asks_for_nothing_is_refused(capsys):
    """Neither a store nor a provider nor a sweep: exiting 0 having done nothing is how a
    mistyped prewarm command lets a cold grid launch."""
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.main([])


def test_symbols_and_universe_file_resolve_to_the_same_list(tmp_path):
    tool = _tool()
    f = tmp_path / "u.txt"
    f.write_text("AAPL\n# a comment\n\n  MSFT  \n", encoding="utf-8")
    assert tool._symbols(str(f), None) == ["AAPL", "MSFT"]
    assert tool._symbols(None, "aapl, msft") == ["AAPL", "MSFT"]


# --------------------------------------------------------------------------------------------
# 6. The pool path. Slow (a spawned child pays the whole backend import), so it is marked and
#    runs the real script end to end -- the one thing an in-process call cannot prove.
# --------------------------------------------------------------------------------------------
@pytest.mark.slow
def test_bootstrap_enters_the_backend_even_when_app_is_already_importable(store_root):
    """THE REMOTE227 DEFECT, pinned.

    The worker wrapper exports a PYTHONPATH containing ``testplatform/backend``, so ``import
    app.models.database`` succeeds in a bare interpreter that has entered nothing. A bootstrap
    that treats "importable" as "already bootstrapped" then skips the chdir, the .env load, the
    ba2_common DB binding and the FMP-key mirroring -- and ``--ohlcv-provider`` dies on "FMP API
    key not configured" as an unhandled traceback on the one host this tool exists for.

    Not expressible in-process (this session IS under pytest and HAS entered the backend), so it
    runs the script the way the wrapper does and reads back the line the bootstrap prints.
    """
    env = dict(os.environ, BACKTEST_OPTIONS_PARQUET_ROOT=store_root,
               PYTHONPATH=str(_REPO / "testplatform" / "backend"))
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--options-store", "tastytrade", "--symbols", "ZZ",
         "--jobs", "1", "--dry-run"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "bootstrap: backend entered" in proc.stdout, proc.stdout + proc.stderr


@pytest.mark.slow
def test_the_spawn_pool_path_builds_every_symbol(store_root, tmp_path):
    env = dict(os.environ, BACKTEST_OPTIONS_PARQUET_ROOT=store_root, BA2_SHARED_ARRAYS="1")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--options-store", "tastytrade",
         "--symbols", "ZZ,YY", "--jobs", "2"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 built / 0 opened / 0 empty" in proc.stdout
    derived = Path(SA.derived_root_for(store_root))
    assert len([p for p in derived.rglob(SA.DONE_MARKER)]) == 2
