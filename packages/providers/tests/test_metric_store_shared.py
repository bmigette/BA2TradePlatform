"""The screener metric store, rebuilt per process from the HOST-shared derived array cache.

WHAT IS BEING PINNED. ``load_store`` no longer parses the parquet partitions in every worker:
it asks ``shared_arrays.DerivedArrayStore`` for one ``.npy`` per numeric column plus integer
category CODES for the object columns, and rebuilds the DataFrame with
``pd.DataFrame(mapping, copy=False)`` — the one construction that does not copy (measured in
reports/strategy_research/metric_store_sharing_spike_2026-09-14.md §3.1). ~1.0 GB of private RAM
per worker, and ``symbol``/``date`` become ``category`` dtype in BOTH modes so the escape hatch
(``BA2_SHARED_ARRAYS=0``) stays byte-for-byte comparable.
"""
import glob
import os

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import shared_arrays as _sa
from ba2_providers.screener import metric_store as ms

_FLOAT_COLS = ("close", "market_cap", "relative_volume", "price_drop_pct", "volume")


def _make_store(tmp_path, name="mstore", n_symbols=20, bump=0.0):
    """A 3-month store: ``n_symbols`` symbols x 3 scan dates, 5 float columns + sector."""
    store = str(tmp_path / name)
    symbols = [f"S{i:02d}" for i in range(n_symbols)]
    dates = ["2023-01-31", "2023-02-28", "2023-03-31"]
    rows = []
    for di, day in enumerate(dates):
        for si, sym in enumerate(symbols):
            rows.append({
                "symbol": sym,
                "date": day,
                "close": 10.0 + si + di + bump,
                "market_cap": 1e9 * (si + 1) + bump,
                "relative_volume": 0.5 + si / 10.0,
                "price_drop_pct": float(si % 7),
                "volume": 1e5 * (si + 1),
                "sector": ("Tech", "Energy", "Health")[si % 3],
            })
    df = pd.DataFrame(rows)
    # price is read by the screen gates; keep the frame shaped like a real store.
    df["price"] = df["close"]
    ms.write_partitions(store, df)
    return store, df


def _parquet_frame(store):
    parts = sorted(glob.glob(os.path.join(store, "ym=*", "*.parquet")))
    return pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)


@pytest.fixture(autouse=True)
def _clear_memo():
    ms.clear_store_memo()
    yield
    ms.clear_store_memo()


def _shared_on(monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "1")


def _shared_off(monkeypatch):
    monkeypatch.setenv("BA2_SHARED_ARRAYS", "0")


# --------------------------------------------------------------- the pure array codec


def test_arrays_round_trip_preserves_values_dtypes_and_column_order(tmp_path):
    _, src = _make_store(tmp_path)
    arrays = ms._store_arrays_from_frame(src)
    out = ms._frame_from_arrays(arrays)

    assert list(out.columns) == list(src.columns)          # original order, not a sorted dict
    assert str(out["symbol"].dtype) == "category"
    assert str(out["date"].dtype) == "category"
    assert out["date"].cat.ordered is True                 # `dates <= day` needs an order
    assert out["symbol"].cat.ordered is False
    for c in _FLOAT_COLS:
        assert out[c].dtype == src[c].dtype
        np.testing.assert_array_equal(out[c].to_numpy(), src[c].to_numpy())
    assert list(out["symbol"].astype(str)) == list(src["symbol"])
    assert list(out["date"].astype(str)) == list(src["date"])
    assert list(out["sector"].astype(str)) == list(src["sector"])


def test_category_code_dtype_is_the_one_pandas_implies_so_from_codes_shares(tmp_path):
    """The whole point of the exercise: an int32 code file would silently COPY (spike §3.4)."""
    _, src = _make_store(tmp_path, n_symbols=20)
    arrays = ms._store_arrays_from_frame(src)
    assert arrays["symbol"].dtype == np.int8            # 20 categories -> int8
    assert arrays["date"].dtype == np.int8              # 3 categories  -> int8
    out = ms._frame_from_arrays(arrays)
    assert np.shares_memory(out["symbol"].cat.codes.to_numpy(), arrays["symbol"])


def test_code_dtype_widens_to_int16_past_the_int8_cardinality(tmp_path):
    _, src = _make_store(tmp_path, n_symbols=200)
    arrays = ms._store_arrays_from_frame(src)
    assert arrays["symbol"].dtype == np.int16
    out = ms._frame_from_arrays(arrays)
    assert np.shares_memory(out["symbol"].cat.codes.to_numpy(), arrays["symbol"])


def test_newline_in_a_category_is_refused_not_silently_shifted(tmp_path):
    _, src = _make_store(tmp_path, n_symbols=3)
    src.loc[0, "sector"] = "Te\nch"
    with pytest.raises(ValueError, match="newline"):
        ms._store_arrays_from_frame(src)


def test_an_all_empty_string_category_round_trips(tmp_path):
    """``_utf8_join([""])`` is a ZERO-BYTE blob, exactly like zero categories; the count decides."""
    _, src = _make_store(tmp_path, n_symbols=4)
    src["sector"] = ""
    out = ms._frame_from_arrays(ms._store_arrays_from_frame(src))
    assert list(out["sector"].cat.categories) == [""]
    assert list(out["sector"].astype(str)) == [""] * len(src)


def test_non_string_categories_are_refused_not_stringified(tmp_path):
    _, src = _make_store(tmp_path, n_symbols=3)
    src["sector"] = [1, 2, 3] * 3                      # object-ish column holding ints
    src["sector"] = src["sector"].astype(object)
    with pytest.raises(TypeError, match="sector"):
        ms._store_arrays_from_frame(src)


def test_a_windows_reserved_column_name_is_refused(tmp_path):
    """Array names are published as ``<name>.npy``; NTFS refuses ``AUX.npy`` outright."""
    _, src = _make_store(tmp_path, n_symbols=3)
    src["AUX"] = 1.0
    with pytest.raises(ValueError, match="AUX"):
        ms._store_arrays_from_frame(src)


def test_nullable_extension_dtype_is_refused_loudly(tmp_path):
    _, src = _make_store(tmp_path, n_symbols=3)
    src["shares"] = pd.array([1, 2, None] * 3, dtype="Int64")
    with pytest.raises(TypeError, match="shares"):
        ms._store_arrays_from_frame(src)


# ------------------------------------------------------------------- load_store itself


def test_load_store_maps_the_columns_and_keeps_them_shared(tmp_path, monkeypatch):
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)

    # The arrays the load ITSELF opened: a second build_or_open would be a second mmap of the
    # same file at a different address, which shares_memory correctly reports as unshared.
    seen = {}
    real = ms._frame_from_arrays

    def _spy(arrays):
        seen.update(arrays)
        return real(arrays)

    monkeypatch.setattr(ms, "_frame_from_arrays", _spy)
    df = ms.load_store(store)

    for c in _FLOAT_COLS:
        assert np.shares_memory(df[c].to_numpy(), seen[c]), c
    assert np.shares_memory(df["symbol"].cat.codes.to_numpy(), seen["symbol"])
    assert np.shares_memory(df["date"].cat.codes.to_numpy(), seen["date"])
    # a plain ndarray VIEW over the mapping, never an np.memmap subclass (33-43% slower on
    # scalar reads) -- but really mapped, not a private build
    assert type(seen["close"]) is np.ndarray
    assert isinstance(seen["close"].base, np.memmap)


def test_screen_and_metric_reads_leave_the_columns_shared(tmp_path, monkeypatch):
    """The §3.2 regression: nothing a consumer does may consolidate the block manager."""
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    before = df["close"].to_numpy()

    ms.screen_universe_for_day(df, "2023-02-28", {"market_cap_min": 2e9, "max_stocks": 5})
    ms.screen_universe_as_of(df, "2023-03-05", {"max_stocks": 5})
    ms.screened_symbol_union(df, "2023-01-01", "2023-04-15", {"max_stocks": 5})
    ms.metrics_as_of(df, "2023-03-05", ["close"])

    assert np.shares_memory(df["close"].to_numpy(), before)


def test_escape_hatch_gives_an_identical_frame_and_writes_nothing(tmp_path, monkeypatch):
    store, _ = _make_store(tmp_path)
    derived_root = _sa.derived_root_for(store)

    _shared_off(monkeypatch)
    private = ms.load_store(store).copy()
    assert not os.path.exists(derived_root)

    ms.clear_store_memo()
    _shared_on(monkeypatch)
    mapped = ms.load_store(store)
    assert os.path.isdir(derived_root)

    pd.testing.assert_frame_equal(private, mapped)


def test_values_equal_the_parquet_frame_column_for_column(tmp_path, monkeypatch):
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    raw = _parquet_frame(store)
    assert list(df.columns) == list(raw.columns)
    for c in raw.columns:
        np.testing.assert_array_equal(df[c].astype(raw[c].dtype).to_numpy(), raw[c].to_numpy())


def test_a_rewritten_partition_invalidates_the_derived_set(tmp_path, monkeypatch):
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    first = ms.load_store(store)["close"].to_numpy().copy()

    _, src2 = _make_store(tmp_path, bump=1000.0)        # same paths, rewritten content
    for p in sorted(glob.glob(os.path.join(store, "ym=*", "*.parquet"))):
        st = os.stat(p)
        os.utime(p, (st.st_atime + 100, st.st_mtime + 100))
    ms.clear_store_memo()
    second = ms.load_store(store)["close"].to_numpy()

    assert not np.array_equal(first, second)
    np.testing.assert_array_equal(second, _parquet_frame(store)["close"].to_numpy())


# ------------------------------------------------------ consumer semantics on categoricals


def test_scan_dates_identical_to_the_string_keyed_frame(tmp_path, monkeypatch):
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    assert ms.scan_dates(df) == ms.scan_dates(_parquet_frame(store))
    assert ms.scan_dates(df) == ["2023-01-31", "2023-02-28", "2023-03-31"]


def test_groupby_symbol_observed_matches_the_string_keyed_frame(tmp_path, monkeypatch):
    """The one semantic trap: a categorical groupby keeps EMPTY categories unless observed=True."""
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    raw = _parquet_frame(store)
    sub = df[df["symbol"] != "S00"]
    raw_sub = raw[raw["symbol"] != "S00"]
    got = sub.groupby("symbol", observed=True).size().sort_index()
    want = raw_sub.groupby("symbol").size().sort_index()
    assert got.to_dict() == want.to_dict()
    assert "S00" not in got.index


def test_screens_and_metrics_agree_with_the_string_keyed_frame(tmp_path, monkeypatch):
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    raw = _parquet_frame(store)
    settings = {"market_cap_min": 3e9, "max_stocks": 6, "sort_metric": "market_cap"}

    assert ms.screen_universe_for_day(df, "2023-02-28", settings) == \
        ms.screen_universe_for_day(raw, "2023-02-28", settings)
    # an as-of day that is NOT a scan date — the categorical `<=` trap
    assert ms.screen_universe_as_of(df, "2023-03-05", settings) == \
        ms.screen_universe_as_of(raw, "2023-03-05", settings)
    assert ms.screen_universe_as_of(df, "2022-12-01", settings) == []
    assert ms.metrics_as_of(df, "2023-03-05", ["close"]) == \
        ms.metrics_as_of(raw, "2023-03-05", ["close"])
    assert ms.screened_symbol_union(df, "2023-01-15", "2023-04-15", settings) == \
        ms.screened_symbol_union(raw, "2023-01-15", "2023-04-15", settings)
    assert ms.screened_symbol_union(df, "2023-02-01", "2023-02-20", settings) == \
        ms.screened_symbol_union(raw, "2023-02-01", "2023-02-20", settings)
    # end_day BEFORE the first scan date: no window at all, on either dtype
    assert ms.screened_symbol_union(df, "2022-11-01", "2022-12-01", settings) == \
        ms.screened_symbol_union(raw, "2022-11-01", "2022-12-01", settings) == []


def test_as_of_resolve_reads_the_rows_present_not_just_the_categories(tmp_path, monkeypatch):
    """A row-filtered frame keeps the FULL category set; the naive answer would be a date that
    the slice no longer contains."""
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    january = df[df["date"] == "2023-01-31"]
    assert list(january["date"].cat.categories) == ms.scan_dates(df)      # categories survive
    assert ms._latest_scan_date_le(january, "2023-06-01") == "2023-01-31"
    assert ms._latest_scan_date_le(january, "2022-12-31") is None
    assert ms._latest_scan_date_le(df, "2023-06-01") == "2023-03-31"
    # and it agrees with the string-keyed frame it replaces
    raw = _parquet_frame(store)
    assert ms._latest_scan_date_le(raw, "2023-03-05") == ms._latest_scan_date_le(df, "2023-03-05")


def test_per_date_top_n_groupby_matches_the_string_keyed_frame(tmp_path, monkeypatch):
    """The call ``screened_symbol_union`` actually changed: groupby('date', observed=True).head."""
    _shared_on(monkeypatch)
    store, _ = _make_store(tmp_path)
    df = ms.load_store(store)
    raw = _parquet_frame(store)
    window = df[df["date"] != "2023-01-31"].sort_values("market_cap", ascending=False)
    raw_window = raw[raw["date"] != "2023-01-31"].sort_values("market_cap", ascending=False)
    got = window.groupby("date", sort=False, observed=True).head(3)
    want = raw_window.groupby("date", sort=False).head(3)
    assert list(got["symbol"].astype(str)) == list(want["symbol"])
    assert sorted(set(got["date"].astype(str))) == ["2023-02-28", "2023-03-31"]
