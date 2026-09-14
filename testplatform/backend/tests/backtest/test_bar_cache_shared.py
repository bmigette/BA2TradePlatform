"""OHLCV bars shared across worker processes through the derived .npy store (Task 5 of
docs/plans/2026-09-14-shared-arrays-across-workers.md).

WHAT IS SHARED AND WHAT IS NOT. The five float64 OHLCV columns (40 of the 48 bytes a bar costs)
are memory-mapped from ``<CACHE_FOLDER>/_derived/<Provider>/u_<SYM>_<interval>_<from>_<to>/<sig>/``
so the OS page cache holds ONE copy per host instead of one per worker. The int64-ns KEYS stay a
PRIVATE ``array('q')`` per process: reading a scalar out of an ndarray key buffer measured 26x
slower than out of an array('q') (see AsOfPriceSource._bind_arrays), and that buffer is on the
engine's hottest path (~200k lookups/backtest). 8 B/bar private is the price of not re-opening
that regression.

The escape hatch ``BA2_SHARED_ARRAYS=0`` restores the pre-2026-09-14 private path verbatim, so
every test here that asserts a shared-path property has a bit-identity twin proving the values did
not move.
"""
from array import array
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.backtest import price_source as ps


class FMPOHLCVProvider:  # noqa: D401 - the NAME is load-bearing (native cache dir == class name)
    """A network-backed provider stand-in.

    ``MemoizedOHLCVProvider(cached_only=True)`` reads
    ``CACHE_FOLDER/<type(inner).__name__>/<SYM>_<interval>.parquet`` and raises
    ``BacktestCacheMiss`` on an absent file only for providers that look network-backed
    (``get_provider_name``) — exactly what a hermetic run does.
    """

    def get_provider_name(self) -> str:
        return "fmp"

    def get_ohlcv_data(self, *a, **k):  # pragma: no cover - hermetic mode must never call this
        raise AssertionError("hermetic backtest must not call the live provider")


class _CountingMemo(ps.MemoizedOHLCVProvider):
    """Counts ``read_window`` calls — the parquet parse a warm cache (worker bar cache or the
    host-shared derived set) is supposed to skip."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads: list = []

    def read_window(self, symbol, start, end, interval):
        self.reads.append(symbol)
        return super().read_window(symbol, start, end, interval)


@pytest.fixture(autouse=True)
def _clean():
    """The bar cache, the full-series memo and the individual counter are process globals."""
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    ps._TRIAL_SEQ = 0
    yield
    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    ps._TRIAL_SEQ = 0


def _frame(n: int, offset: float, dates) -> pd.DataFrame:
    return pd.DataFrame({
        "Date": dates,
        "Open": np.arange(n, dtype=float) + offset,
        "High": np.arange(n, dtype=float) + offset + 1.0,
        "Low": np.arange(n, dtype=float) + offset - 1.0,
        "Close": np.arange(n, dtype=float) + offset + 0.5,
        "Volume": np.arange(n, dtype=float) * 10.0,
    })


def _native_tree(tmp_path, monkeypatch, syms=("AAA", "BBB"), n=300, interval="1d",
                 messy=()):
    """A ``CACHE_FOLDER/FMPOHLCVProvider/<SYM>_<interval>.parquet`` tree, like
    ``ba2-test fetch-cache`` writes. ``messy`` names symbols whose frame is written UNSORTED and
    with a duplicated date, to exercise _store's argsort/dedup (keep the LAST of equal keys)."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    d = tmp_path / "FMPOHLCVProvider"
    d.mkdir(exist_ok=True)
    if interval == "1d":
        dates = pd.bdate_range("2024-01-01", periods=n)
    else:
        dates = pd.date_range("2024-01-02 14:30", periods=n, freq="5min")
    for i, s in enumerate(syms):
        df = _frame(n, float(i), dates)
        if s in messy:
            # one duplicated key (a later row must win) + a shuffled row order
            dup = df.iloc[[7]].copy()
            dup["Close"] = 999.5
            dup["Open"] = 999.0
            df = pd.concat([df, dup], ignore_index=True)
            df = df.iloc[np.argsort(np.arange(len(df)) % 5, kind="stable")].reset_index(drop=True)
        df.to_parquet(d / f"{s}_{interval}.parquet", index=False)
    return tmp_path


def _source(tmp_path, interval="1d", counting=False):
    cls = _CountingMemo if counting else ps.MemoizedOHLCVProvider
    prov = cls(FMPOHLCVProvider(), datetime(2023, 1, 1), datetime(2026, 12, 31),
               interval=interval, cached_only=True)
    return ps.AsOfPriceSource(ohlcv_provider=prov, interval=interval), prov


def _preload(src, syms, interval="1d"):
    if interval == "1d":
        src.preload(list(syms), datetime(2024, 3, 1), datetime(2024, 12, 31), warmup_days=30)
    else:
        src.preload(list(syms), datetime(2024, 1, 2), datetime(2024, 1, 5), warmup_days=1)


def _derived_root(tmp_path):
    from ba2_common.core import shared_arrays as SA
    return Path(SA.derived_root_for(str(tmp_path / "FMPOHLCVProvider")))


def _dump(src, syms):
    """Everything the store holds for ``syms``, in a form that compares NaN-safely (repr of the
    exact float list, not a numeric comparison)."""
    return {s: repr((list(src._keys[s]), src._o[s].tolist(), src._h[s].tolist(),
                     src._l[s].tolist(), src._c[s].tolist(), src._v[s].tolist()))
            for s in syms}


# ---------------------------------------------------------------------------------------------


def test_preload_maps_ohlcv_columns_and_keeps_keys_private(tmp_path, monkeypatch):
    """The columns come from the mapped .npy set; the keys are still a private array('q')."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    src, _ = _source(_native_tree(tmp_path, monkeypatch))
    _preload(src, ["AAA", "BBB"])

    k = src._keys["AAA"]
    assert isinstance(k, array) and k.typecode == "q"
    for col in (src._o, src._h, src._l, src._c, src._v):
        arr = col["AAA"]
        assert type(arr) is np.ndarray, "must be np.asarray-wrapped, not the np.memmap subclass"
        assert arr.base is not None, "column is a private copy, not a view over the mapping"
        assert not arr.flags.writeable, "a shared mapping must never be writeable"
    d = _derived_root(tmp_path)
    assert any(d.rglob("c.npy")), f"no derived close array under {d}"
    assert any(p.name.startswith("u_AAA_1d_") for p in d.iterdir()), \
        "the key must be prefixed (Windows reserved device names) and name the window"


def test_shared_and_private_preload_are_bit_identical(tmp_path, monkeypatch):
    """The whole point: same keys, same bars, whichever path served the arrays. BBB's frame is
    unsorted and carries a duplicated date, so the argsort/dedup half is exercised too."""
    got = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("BA2_SHARED_ARRAYS", flag)
        ps.clear_worker_bar_cache()
        ps.clear_ohlcv_memo()
        src, _ = _source(_native_tree(tmp_path, monkeypatch, messy=("BBB",)))
        _preload(src, ["AAA", "BBB"])
        got[flag] = _dump(src, ("AAA", "BBB"))
    assert got["0"] == got["1"]


def test_intraday_shared_and_private_preload_are_bit_identical(tmp_path, monkeypatch):
    """Intraday keys carry the full timestamp (no datetime64[D] truncation) — the one branch of
    _ohlcv_arrays_from_df the daily parity test cannot reach."""
    got = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("BA2_SHARED_ARRAYS", flag)
        ps.clear_worker_bar_cache()
        ps.clear_ohlcv_memo()
        src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",), n=200,
                                      interval="5min"), interval="5min")
        _preload(src, ["AAA"], interval="5min")
        got[flag] = _dump(src, ("AAA",))
    assert got["0"] == got["1"]
    # sanity: intraday keys are NOT midnight-truncated
    assert len({v % 86_400_000_000_000 for v in list(src._keys["AAA"])}) > 1


def test_hermetic_miss_still_raises_and_writes_no_derived_entry(tmp_path, monkeypatch):
    """A BacktestCacheMiss raised inside the build_fn must propagate unchanged and leave the
    derived store untouched for that symbol (shared_arrays writes nothing on exception)."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",)))
    with pytest.raises(ps.BacktestCacheMiss):
        _preload(src, ["AAA", "GONE"])          # 1 of 2 missing is far past the tolerance
    d = _derived_root(tmp_path)
    assert not any("GONE" in p.name for p in d.iterdir())
    assert all(k[0] != "GONE" for k in ps._WORKER_BAR_CACHE)


def test_shared_mode_never_flushes_between_individuals(tmp_path, monkeypatch):
    """Operator decision 2026-09-14: the bars are static and now HOST-shared, so the cache
    persists for the whole job instead of being flushed before every individual."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    root = _native_tree(tmp_path, monkeypatch, syms=("AAA", "BBB", "CCC", "DDD"))
    src1, prov = _source(root, counting=True)
    _preload(src1, ["AAA", "BBB"])
    src2 = ps.AsOfPriceSource(ohlcv_provider=prov, interval="1d")
    _preload(src2, ["CCC", "DDD"])              # a second individual, disjoint working set

    assert {k[0] for k in ps._WORKER_BAR_CACHE} == {"AAA", "BBB", "CCC", "DDD"}
    assert sorted(prov.reads) == ["AAA", "BBB", "CCC", "DDD"], "a symbol was parsed twice"


def test_shared_mode_still_honours_the_count_backstop(tmp_path, monkeypatch):
    """Persistence is not unbounded: BT_BAR_CACHE_MAX still caps the entry count."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_MAX", 1)
    src, _ = _source(_native_tree(tmp_path, monkeypatch))
    _preload(src, ["AAA", "BBB"])
    assert len(ps._WORKER_BAR_CACHE) == 1
    assert {k[0] for k in ps._WORKER_BAR_CACHE} == {"BBB"}       # oldest evicted
    assert len(ps._BAR_CACHE_LAST_USED) == 1


def test_memory_stats_reports_shared_bytes_and_mode(tmp_path, monkeypatch):
    """The governor reasons about PRIVATE bytes; the mapped pages are the host's and reclaimable,
    so they are reported separately instead of being counted as this process's footprint."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    # 20k bars, so the MB figures are above the 0.1 MB rounding the telemetry reports at.
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",), n=20_000,
                                  interval="5min"), interval="5min")
    src.preload(["AAA"], datetime(2024, 1, 2), datetime(2024, 4, 1), warmup_days=1)
    st = ps.memory_stats()["bar_cache"]
    assert st["shared_mb"] > 0
    assert st["mode"] == "persistent"
    # private = the int64 keys only (8 B/bar) against 5 x 8 B/bar shared
    assert 0 <= st["mb"] < st["shared_mb"]


def test_private_path_still_flushes_per_individual(tmp_path, monkeypatch):
    """BA2_SHARED_ARRAYS=0 restores today's behaviour exactly: flush-per-individual (the peak
    bound that mattered when every column was a private copy), and nothing on disk."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 0)
    root = _native_tree(tmp_path, monkeypatch, syms=("AAA", "BBB", "CCC", "DDD"))
    src1, prov = _source(root, counting=True)
    _preload(src1, ["AAA", "BBB"])
    src2 = ps.AsOfPriceSource(ohlcv_provider=prov, interval="1d")
    _preload(src2, ["CCC", "DDD"])

    assert {k[0] for k in ps._WORKER_BAR_CACHE} == {"CCC", "DDD"}
    assert ps.memory_stats()["bar_cache"]["mode"] == "flush"
    assert ps.memory_stats()["bar_cache"]["shared_mb"] == 0.0
    assert not (tmp_path / "_derived").exists(), "the escape hatch must write nothing"
    assert src2._c["CCC"].base is None and src2._c["CCC"].flags.writeable
