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
                 messy=(), dates=None):
    """A ``CACHE_FOLDER/FMPOHLCVProvider/<SYM>_<interval>.parquet`` tree, like
    ``ba2-test fetch-cache`` writes. ``messy`` names symbols whose frame is written UNSORTED and
    with a duplicated date, to exercise _store's argsort/dedup (keep the LAST of equal keys)."""
    import ba2_common.config as cfg
    from ba2_common.core import native_cache
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path), raising=False)
    d = tmp_path / "FMPOHLCVProvider"
    d.mkdir(exist_ok=True)
    if dates is None:
        dates = (pd.bdate_range("2024-01-01", periods=n) if interval == "1d"
                 else pd.date_range("2024-01-02 14:30", periods=n, freq="5min"))
    n = len(dates)
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


def _sig_dirs(tmp_path):
    """Every published signature directory under the derived root (any key)."""
    return [p for p in _derived_root(tmp_path).rglob("*") if p.is_dir() and (p / "_done.json").is_file()]


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


def test_two_windows_on_the_same_day_get_different_derived_sets(tmp_path, monkeypatch):
    """REGRESSION (review round 1). The derived key spelled the window as <start-date>_<end-date>
    and the signature covers only the parquet, so two windows that differ only in TIME OF DAY
    resolved to the same <key>/<sig>: the second preload was served the first one's arrays. On a
    one-day 5min series, 09:30->10:00 (7 bars) followed by 09:30->16:00 (79 bars) returned 7 bars
    BOTH times with sharing on, and 7 then 79 with it off. Wrong data, no error.
    """
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    day = pd.date_range("2024-01-02 09:30", "2024-01-02 16:00", freq="5min")
    root = _native_tree(tmp_path, monkeypatch, syms=("AAA",), interval="5min", dates=day)
    src1, prov = _source(root, interval="5min")
    src1.preload(["AAA"], datetime(2024, 1, 2, 9, 30), datetime(2024, 1, 2, 10, 0), warmup_days=0)
    src2 = ps.AsOfPriceSource(ohlcv_provider=prov, interval="5min")
    src2.preload(["AAA"], datetime(2024, 1, 2, 9, 30), datetime(2024, 1, 2, 16, 0), warmup_days=0)

    assert len(src1._keys["AAA"]) == 7
    assert len(src2._keys["AAA"]) == 79
    assert len([p for p in _derived_root(tmp_path).iterdir() if p.is_dir()]) == 2, \
        "the two windows must key two derived sets, not share one"


def test_a_refreshed_parquet_invalidates_the_derived_set(tmp_path, monkeypatch):
    """The signature is (name, size, mtime) of the source parquet: re-fetching a symbol's bars
    must make the next run see the new rows, not the arrays built from the old file."""
    import os

    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    root = _native_tree(tmp_path, monkeypatch, syms=("AAA",), n=300)
    src1, _ = _source(root)
    _preload(src1, ["AAA"])
    before = {p.name for p in _sig_dirs(tmp_path)}
    first_bars = len(src1._keys["AAA"])

    # Add a bar INSIDE the preloaded window (a Saturday, so it cannot already be in the
    # business-day series), then age the mtime forward -- the fixture writer can finish inside one
    # filesystem timestamp tick, which would leave the signature unchanged for the wrong reason.
    parquet = tmp_path / "FMPOHLCVProvider" / "AAA_1d.parquet"
    df = pd.read_parquet(parquet)
    extra = df.iloc[[-1]].copy()
    extra["Date"] = pd.Timestamp("2024-06-01")
    pd.concat([df, extra], ignore_index=True).to_parquet(parquet, index=False)
    os.utime(parquet, (os.path.getatime(parquet) + 100, os.path.getmtime(parquet) + 100))

    ps.clear_worker_bar_cache()
    ps.clear_ohlcv_memo()
    src2, _ = _source(root)
    _preload(src2, ["AAA"])
    after = {p.name for p in _sig_dirs(tmp_path)}
    assert len(src2._keys["AAA"]) == first_bars + 1, "the rebuilt set must carry the new bar"
    assert after - before, "a changed source must publish a NEW signature directory"


def test_a_provider_without_cached_path_builds_privately_and_writes_nothing(tmp_path, monkeypatch):
    """Fixture/in-memory providers have no parquet to sign, so there is nothing to share — they
    must keep working exactly as before and leave no derived cache behind."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")

    class _InMemory:
        def read_window(self, symbol, start, end, interval):
            return _frame(3, 0.0, pd.to_datetime(["2024-03-04", "2024-03-05", "2024-03-06"]))

    src = ps.AsOfPriceSource(ohlcv_provider=_InMemory(), interval="1d")
    _preload(src, ["AAA"])
    assert len(src._keys["AAA"]) == 3
    assert src._c["AAA"].base is None and src._c["AAA"].flags.writeable
    assert not (tmp_path / "_derived").exists()


def test_a_live_fetch_provider_never_signs_a_parquet_it_did_not_read(tmp_path, monkeypatch):
    """``cached_only=False`` serves the series from the network through get_ohlcv_data; a parquet
    sitting at the cache path is NOT what produced those arrays, so it must not key them."""
    _native_tree(tmp_path, monkeypatch, syms=("AAA",))
    live = ps.MemoizedOHLCVProvider(FMPOHLCVProvider(), datetime(2023, 1, 1),
                                    datetime(2026, 12, 31), interval="1d", cached_only=False)
    assert live.cached_path("AAA", "1d") is None
    cached = ps.MemoizedOHLCVProvider(FMPOHLCVProvider(), datetime(2023, 1, 1),
                                      datetime(2026, 12, 31), interval="1d", cached_only=True)
    assert cached.cached_path("AAA", "1d") is not None      # same tree, hermetic mode


def test_a_broken_cache_path_lookup_degrades_loudly_not_silently(tmp_path, monkeypatch, caplog):
    """An OSError out of the path lookup means "no shared source" — the run continues on private
    arrays — but it must SAY so once: the only other symptom is a shared_mb of 0, which is
    indistinguishable from sharing being off on purpose."""
    import logging

    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    from ba2_common.core import native_cache
    monkeypatch.setattr(ps, "_CACHED_PATH_WARNED", set())
    real = native_cache.find_timeseries_path
    calls = []

    def _flaky(*a, **k):
        # Fails the SIGNING lookup (the first call of a preload) and recovers for the read, so the
        # test isolates "no shared source" from "no data at all" -- a lookup that stays broken is
        # an ordinary hermetic cache miss, covered elsewhere.
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk gone")
        return real(*a, **k)

    monkeypatch.setattr(native_cache, "find_timeseries_path", _flaky)
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",)))
    with caplog.at_level(logging.WARNING):
        _preload(src, ["AAA"])
    assert len(src._keys["AAA"]) > 0                      # built privately, results unaffected
    assert src._c["AAA"].base is None
    assert any("falls back to PRIVATE" in r.message for r in caplog.records)


def test_an_unexpected_cache_path_error_propagates(tmp_path, monkeypatch):
    """Only ImportError/OSError degrade. A blanket ``except Exception -> None`` would turn any bug
    in the lookup into "sharing is silently off for the whole run"."""
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")
    from ba2_common.core import native_cache
    monkeypatch.setattr(native_cache, "find_timeseries_path",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    src, _ = _source(_native_tree(tmp_path, monkeypatch, syms=("AAA",)))
    with pytest.raises(RuntimeError, match="bug"):
        _preload(src, ["AAA"])


def test_every_array_dict_carries_exactly_the_published_names():
    """The dict keys ARE the .npy file names, so a name that drifts between the empty case, the
    built case and the reader is a wrong-data bug, not a typo."""
    empty = ps._empty_ohlcv_arrays()
    built = ps._ohlcv_arrays_from_df(
        _frame(3, 0.0, pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])), False)
    assert set(empty) == set(ps._ARRAY_NAMES)
    assert set(built) == set(ps._ARRAY_NAMES)
    assert built["keys_ns"].dtype == np.int64
