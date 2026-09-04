"""On-disk parquet store for historical option bars — resume + interrupt safety.

The whole reason this store exists is a multi-HOUR download that will be interrupted
(Ctrl-C, a dropped socket, a sleeping laptop). So the properties under test here are not
"can it write a file" but:

  * a killed write can never be mistaken for a finished one,
  * "already downloaded" is distinguishable from "downloaded and genuinely EMPTY",
    so a contract with no bars is a recorded fact rather than an eternal retry,
  * a re-run skips finished work without re-fetching it,
  * ``iv`` and ``open_interest`` survive the round trip — ``open_interest`` is the field
    the switch was actually needed for (NULL across all 1,440,782 chain rows of the
    incumbent cache, with no bar column to recover it); the incumbent's ``iv`` is thin
    rather than absent (46% of chain rows, 88.2% of bar rows), so a vendor IV is an
    improvement there, not a rescue. See ``option_selector._publishes_spread``.

Time is frozen to a fixed instant (never ``today``) via an injected clock.
"""
import json
import os
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core.interfaces import OptionEodBar
from ba2_providers.options.parquet_store import (
    PartitionState, OptionHistoryParquetStore,
)

FROZEN = datetime(2026, 3, 4, 17, 45, 12, tzinfo=timezone.utc)
WINDOW = (date(2023, 1, 1), date(2026, 3, 1))


def _clock():
    return FROZEN


@pytest.fixture
def store(tmp_path):
    return OptionHistoryParquetStore(root=str(tmp_path / "opt"), clock=_clock)


def _bar(occ="AAPL230120C00150000", d=date(2023, 1, 3), *, iv=0.2841, oi=12345,
         close=7.25, bid=None, ask=None):
    return OptionEodBar(occ_symbol=occ, bar_date=d, open=7.0, high=7.5, low=6.8,
                        close=close, volume=911, bid=bid, ask=ask,
                        open_interest=oi, iv=iv)


# --------------------------------------------------------------------------- #
# layout — must MATCH the existing OHLCV parquet cache, not invent a new scheme
# --------------------------------------------------------------------------- #
def test_root_defaults_under_the_shared_cache_folder_beside_the_ohlcv_providers():
    """The OHLCV cache is ``CACHE_FOLDER/<ProviderClassName>/...`` (AlpacaOHLCVProvider,
    FMPOHLCVProvider). This store is a sibling of those, NOT a new tree — and it must read
    CACHE_FOLDER at CALL time so the providers conftest's temp-dir rebind wins."""
    import ba2_common.config as cfg
    s = OptionHistoryParquetStore()
    assert s.root == os.path.join(cfg.CACHE_FOLDER, "TastyTradeOptionsProvider")


def test_root_is_not_the_existing_ten_gigabyte_options_cache():
    """The incumbent cache is a 10 GB sqlite at CACHE_FOLDER/options/options_history.sqlite.
    This must be a NEW path — the warm-up rebuilds a superset and must never migrate or
    touch that file."""
    from ba2_common.config import OPTIONS_CACHE_DB
    s = OptionHistoryParquetStore()
    assert os.path.dirname(OPTIONS_CACHE_DB) != s.root
    assert "options_history.sqlite" not in s.root


def test_partition_layout_is_symbol_then_hive_style_expiry(store):
    """One parquet per (underlying, expiry): ``<root>/<SYM>/exp=YYYY-MM-DD/<SYM>_<exp>_1d.parquet``.
    ``<SYM>/`` mirrors the per-symbol OHLCV files; ``exp=`` mirrors the screener metric
    store's ``ym=`` hive partitions. Both conventions already exist in this repo."""
    p = store.bars_path("AAPL", date(2023, 1, 20))
    rel = os.path.relpath(p, store.root)
    assert rel == os.path.join("AAPL", "exp=2023-01-20", "AAPL_2023-01-20_1d.parquet")
    assert store.manifest_path("AAPL", date(2023, 1, 20)) == os.path.join(
        os.path.dirname(p), "_manifest.json")


def test_underlying_is_upper_cased_in_the_path(store):
    assert store.bars_path("aapl", date(2023, 1, 20)) == store.bars_path("AAPL", date(2023, 1, 20))


# --------------------------------------------------------------------------- #
# the three states resume depends on
# --------------------------------------------------------------------------- #
def test_never_fetched_partition_is_missing(store):
    assert store.partition_state("AAPL", date(2023, 1, 20), *WINDOW) is PartitionState.MISSING


def test_written_partition_is_complete_and_is_not_refetched(store):
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])
    assert store.partition_state("AAPL", date(2023, 1, 20), *WINDOW) is PartitionState.COMPLETE


def test_a_genuinely_empty_contract_set_is_recorded_as_EMPTY_not_missing(store):
    """A contract with no bars is a FACT worth recording. If empty and never-fetched were
    conflated the warm-up would re-request every dead strike on every run, forever."""
    store.write_partition("AAPL", date(2023, 1, 20), [], *WINDOW,
                          empty_contracts=["AAPL230120C00990000"])
    st = store.partition_state("AAPL", date(2023, 1, 20), *WINDOW)
    assert st is PartitionState.EMPTY
    assert st is not PartitionState.MISSING
    assert store.read_manifest("AAPL", date(2023, 1, 20))["empty_contracts"] == \
        ["AAPL230120C00990000"]


def test_empty_and_complete_both_count_as_done_so_neither_is_refetched(store):
    store.write_partition("AAPL", date(2023, 1, 20), [], *WINDOW, empty_contracts=["X"])
    store.write_partition("AAPL", date(2023, 1, 27), [_bar()], *WINDOW, empty_contracts=[])
    assert store.is_done("AAPL", date(2023, 1, 20), *WINDOW)
    assert store.is_done("AAPL", date(2023, 1, 27), *WINDOW)
    assert not store.is_done("AAPL", date(2023, 2, 3), *WINDOW)


def test_empty_partition_writes_no_parquet_only_a_manifest(store):
    store.write_partition("AAPL", date(2023, 1, 20), [], *WINDOW, empty_contracts=["X"])
    assert not os.path.exists(store.bars_path("AAPL", date(2023, 1, 20)))
    assert os.path.exists(store.manifest_path("AAPL", date(2023, 1, 20)))


def test_resume_skips_completed_partitions_and_returns_only_the_rest(store):
    done = [date(2023, 1, 20), date(2023, 1, 27)]
    wanted = done + [date(2023, 2, 3), date(2023, 2, 10)]
    for e in done:
        store.write_partition("AAPL", e, [_bar(d=date(2023, 1, 3))], *WINDOW, empty_contracts=[])
    todo = store.pending_partitions("AAPL", wanted, *WINDOW)
    assert todo == [date(2023, 2, 3), date(2023, 2, 10)]


# --------------------------------------------------------------------------- #
# interrupt safety
# --------------------------------------------------------------------------- #
def test_a_parquet_without_its_manifest_is_NOT_complete(store):
    """The manifest is written LAST. A process killed between the parquet rename and the
    manifest rename leaves data on disk that is not yet known to be whole — treating that
    as done would silently bake a truncated partition into the cache."""
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])
    os.remove(store.manifest_path("AAPL", date(2023, 1, 20)))
    assert os.path.exists(store.bars_path("AAPL", date(2023, 1, 20)))
    assert store.partition_state("AAPL", date(2023, 1, 20), *WINDOW) is PartitionState.MISSING


def test_a_manifest_without_its_parquet_is_NOT_complete(store):
    """The inverse: a manifest claiming rows whose parquet vanished (a botched copy, a
    half-restored backup) must re-fetch rather than serve nothing as if it were fine."""
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])
    os.remove(store.bars_path("AAPL", date(2023, 1, 20)))
    assert store.partition_state("AAPL", date(2023, 1, 20), *WINDOW) is PartitionState.MISSING


def test_kill_mid_parquet_write_leaves_nothing_readable_as_complete(store, monkeypatch):
    """Simulate SIGKILL between opening the temp file and renaming it."""
    exp = date(2023, 1, 20)

    real_replace = os.replace

    def die(src, dst):
        if str(dst).endswith(".parquet"):
            raise KeyboardInterrupt("simulated kill mid-write")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    monkeypatch.undo()

    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.MISSING
    assert not os.path.exists(store.bars_path("AAPL", exp))
    assert not os.path.exists(store.manifest_path("AAPL", exp))


def test_kill_between_parquet_and_manifest_is_recoverable_and_reretried(store, monkeypatch):
    exp = date(2023, 1, 20)
    real_replace = os.replace

    def die(src, dst):
        if str(dst).endswith("_manifest.json"):
            raise KeyboardInterrupt("simulated kill before the manifest lands")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    monkeypatch.undo()

    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.MISSING
    # ... and the re-run overwrites cleanly.
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.COMPLETE


def test_the_parquet_is_never_written_in_place(store, monkeypatch):
    """A direct ``to_parquet(final_path)`` is the bug this guards: a kill mid-write leaves a
    truncated file AT the real path, which the next run may or may not notice."""
    seen = []
    real_to_parquet = pd.DataFrame.to_parquet

    def spy(self, path, *a, **k):
        seen.append(str(path))
        return real_to_parquet(self, path, *a, **k)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", spy, raising=False)
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])

    final = store.bars_path("AAPL", date(2023, 1, 20))
    assert seen, "expected a parquet write"
    assert final not in seen, "parquet was written directly to its final path"
    assert all(p.endswith(".tmp") for p in seen), seen


def test_the_manifest_is_never_written_in_place(store):
    """Same argument for the manifest: it is the completion marker, so a torn JSON at the
    real path would be an unparseable 'done'."""
    exp = date(2023, 1, 20)
    opened = []
    import builtins
    real_open = builtins.open

    def spy(path, *a, **k):
        if "w" in str(a[0] if a else k.get("mode", "r")):
            opened.append(str(path))
        return real_open(path, *a, **k)

    import ba2_providers.options.parquet_store as ps
    orig = ps.open if hasattr(ps, "open") else None
    ps.open = spy  # type: ignore[attr-defined]
    try:
        store.write_partition("AAPL", exp, [], *WINDOW, empty_contracts=["X"])
    finally:
        if orig is None:
            del ps.open  # type: ignore[attr-defined]
        else:
            ps.open = orig  # type: ignore[attr-defined]

    assert opened, "expected a manifest write"
    assert store.manifest_path("AAPL", exp) not in opened
    assert all(p.endswith(".tmp") for p in opened), opened


def test_a_failed_rename_leaves_no_temp_file_behind(store, monkeypatch):
    """The cleanup in the ``finally``. Without it every killed write leaves a multi-MB
    orphan in the partition dir, and a long backfill quietly fills the disk with them."""
    exp = date(2023, 1, 20)
    real_replace = os.replace

    def die(src, dst):
        if str(dst).endswith(".parquet"):
            raise KeyboardInterrupt("simulated kill")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    monkeypatch.undo()

    leftovers = [f for f in os.listdir(store.partition_dir("AAPL", exp))
                 if f.endswith(".tmp")]
    assert leftovers == [], leftovers


def test_the_temp_name_is_unique_per_process_and_per_thread():
    """Two builders writing the same partition must not share a temp path — one would
    truncate the other's half-written file and then rename the wreck into place. The GA
    fans out over processes, so 'it is one process' is not an available assumption."""
    import threading

    from ba2_providers.options.parquet_store import _tmp_name

    path = "/tmp/x/AAPL_2023-01-20_1d.parquet"
    mine = _tmp_name(path)
    assert str(os.getpid()) in mine

    other = {}
    t = threading.Thread(target=lambda: other.setdefault("n", _tmp_name(path)))
    t.start()
    t.join()
    assert other["n"] != mine, "two threads produced the same temp path"


def test_a_partition_dir_with_only_a_temp_file_is_not_reported_as_completed(store):
    """``completed_expiries`` drives progress reporting and any downstream consumer; an
    interrupted directory is unfinished work, not a completed partition."""
    exp = date(2023, 2, 3)
    d = store.partition_dir("AAPL", exp)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "AAPL_2023-02-03_1d.parquet.7.7.tmp"), "wb") as f:
        f.write(b"half a parquet")
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])
    assert store.completed_expiries("AAPL") == [date(2023, 1, 20)]


def test_rewriting_a_partition_as_empty_removes_the_rows_it_used_to_hold(store):
    """Otherwise an EMPTY manifest sits beside a stale parquet, and ``read_underlying``
    serves rows the manifest says do not exist."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    assert store.read_partition("AAPL", exp) is not None

    store.write_partition("AAPL", exp, [], *WINDOW, empty_contracts=["AAPL230120C00150000"])
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.EMPTY
    assert store.read_partition("AAPL", exp) is None
    assert store.read_underlying("AAPL") is None


def test_the_on_disk_column_order_is_exactly_the_declared_schema(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    df = store.read_partition("AAPL", exp)
    assert list(df.columns) == list(OptionHistoryParquetStore.COLUMNS)


def test_a_leftover_temp_file_is_not_mistaken_for_data(store):
    exp = date(2023, 1, 20)
    d = os.path.dirname(store.bars_path("AAPL", exp))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "AAPL_2023-01-20_1d.parquet.13.99.tmp"), "wb") as f:
        f.write(b"not a parquet")
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.MISSING
    assert store.read_partition("AAPL", exp) is None


def test_a_corrupt_manifest_is_treated_as_missing_not_as_done(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    with open(store.manifest_path("AAPL", exp), "w") as f:
        f.write("{ truncated")
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.MISSING


# --------------------------------------------------------------------------- #
# window coverage — the metric-store bug class, ported
# --------------------------------------------------------------------------- #
def test_a_partition_fetched_over_a_narrower_window_is_stale_not_complete():
    """Exactly the bug the screener metric store hit: a cache keyed on symbol alone silently
    served a SHORTER range once a later build widened ``start``. The manifest records the
    window actually fetched, so widening the request re-fetches instead of lying."""
    import tempfile
    s = OptionHistoryParquetStore(root=tempfile.mkdtemp(), clock=_clock)
    s.write_partition("AAPL", date(2023, 1, 20), [_bar()],
                      date(2024, 1, 1), date(2026, 3, 1), empty_contracts=[])
    assert s.partition_state("AAPL", date(2023, 1, 20),
                             date(2024, 1, 1), date(2026, 3, 1)) is PartitionState.COMPLETE
    assert s.partition_state("AAPL", date(2023, 1, 20),
                             date(2023, 1, 1), date(2026, 3, 1)) is PartitionState.STALE
    assert not s.is_done("AAPL", date(2023, 1, 20), date(2023, 1, 1), date(2026, 3, 1))


def test_a_partition_fetched_over_a_WIDER_window_still_satisfies_a_narrower_request(store):
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()],
                          date(2022, 1, 1), date(2026, 6, 1), empty_contracts=[])
    assert store.partition_state("AAPL", date(2023, 1, 20),
                                 date(2023, 1, 1), date(2026, 3, 1)) is PartitionState.COMPLETE


def test_a_request_ending_later_than_the_fetched_window_is_stale_for_a_LIVE_expiry(store):
    """A contract that has NOT yet expired can still print new bars, so a later window end
    genuinely asks for data the partition does not have.

    The expiry here is deliberately AFTER both window ends. It used to be 2023-01-20 --
    two years BEFORE the fetched end -- which made this test assert the bug below rather
    than the rule it is named for."""
    store.write_partition("AAPL", date(2026, 6, 19), [_bar()],
                          date(2023, 1, 1), date(2025, 1, 1), empty_contracts=[])
    assert store.partition_state("AAPL", date(2026, 6, 19),
                                 date(2023, 1, 1), date(2026, 3, 1)) is PartitionState.STALE


def test_a_request_ending_after_an_EXPIRED_contract_does_NOT_restale_it(store):
    """THE CACHE-DESTROYING BUG, pinned. A contract cannot print a bar after it expires, so
    once a partition is fetched through its own expiry it is FINAL and a later window end
    asks for days on which the contract did not exist.

    Comparing against the raw window end instead is how the whole store re-downloaded
    itself: the warmup defaults its end to TODAY, so every run moved the end forward, every
    complete partition compared STALE, and the fetcher re-pulled everything -- measured
    2026-08-30, 820 of 857 symbols and 1,576 partitions rewritten in ~35 minutes.

    It is data LOSS, not just wasted bandwidth: write_partition DELETES the parquet when a
    re-fetch returns no rows, and dxfeed routinely declines to resolve long-dead contracts.
    Each pointless pass can therefore turn a good partition into status="empty"."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()],
                          date(2023, 1, 1), date(2025, 1, 1), empty_contracts=[])
    # End moved YEARS past the fetch, as a to-today default does every single day.
    assert store.partition_state("AAPL", exp,
                                 date(2023, 1, 1), date(2026, 3, 1)) is PartitionState.COMPLETE
    assert store.is_done("AAPL", exp, date(2023, 1, 1), date(2026, 3, 1))
    # And it stays skipped tomorrow, and every day after.
    assert store.pending_partitions("AAPL", [exp], date(2023, 1, 1), date(2030, 1, 1)) == []


def test_a_partition_not_yet_fetched_through_its_expiry_is_still_stale(store):
    """The other side of the same boundary -- the fix must not become "expired means done".
    Fetched only to 2023-01-10 for a contract living until 2023-01-20, so ten days of its
    life are genuinely missing and it must re-fetch."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()],
                          date(2023, 1, 1), date(2023, 1, 10), empty_contracts=[])
    assert store.partition_state("AAPL", exp,
                                 date(2023, 1, 1), date(2026, 3, 1)) is PartitionState.STALE


# --------------------------------------------------------------------------- #
# the payload: iv / open_interest must survive
# --------------------------------------------------------------------------- #
def test_iv_and_open_interest_survive_the_round_trip(store):
    """The ONLY reason this pipeline exists. delta selection and min_open_interest are both
    dead in backtest because these two are NULL everywhere in the incumbent cache."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [
        _bar(d=date(2023, 1, 3), iv=0.2841, oi=12345),
        _bar(d=date(2023, 1, 4), iv=0.31159, oi=13001),
    ], *WINDOW, empty_contracts=[])

    df = store.read_partition("AAPL", exp)
    assert list(df.columns).count("iv") == 1
    assert list(df.columns).count("open_interest") == 1
    got = df.sort_values("bar_date")
    assert [round(float(v), 5) for v in got["iv"]] == [0.2841, 0.31159]
    assert [int(v) for v in got["open_interest"]] == [12345, 13001]


def test_bid_and_ask_survive_the_round_trip(store):
    """2026-09-03: this store silently dropped bid/ask on every write (COLUMNS/_frame never
    named them), even though OptionEodBar carries both and ThetaData's EOD report populates
    them (TastyTrade never does -- dxfeed serves no historical NBBO for dead contracts, so
    its bars are always bid=None/ask=None here, same as before this fix)."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [
        _bar(d=date(2023, 1, 3), bid=7.10, ask=7.40),
        _bar(d=date(2023, 1, 4), bid=7.20, ask=7.55),
    ], *WINDOW, empty_contracts=[])

    df = store.read_partition("AAPL", exp)
    assert list(df.columns).count("bid") == 1
    assert list(df.columns).count("ask") == 1
    got = df.sort_values("bar_date")
    assert [round(float(v), 2) for v in got["bid"]] == [7.10, 7.20]
    assert [round(float(v), 2) for v in got["ask"]] == [7.40, 7.55]


def test_a_missing_bid_ask_stays_missing_and_does_not_become_zero(store):
    """TastyTrade partitions (bid/ask always None) must round-trip as a null column, not a
    fabricated 0.0 spread -- the same rule iv/open_interest already follow."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(bid=None, ask=None)], *WINDOW, empty_contracts=[])
    df = store.read_partition("AAPL", exp)
    assert df["bid"].isna().all()
    assert df["ask"].isna().all()


def test_open_interest_round_trips_as_an_integer_not_a_float(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(oi=12345)], *WINDOW, empty_contracts=[])
    df = store.read_partition("AAPL", exp)
    assert str(df["open_interest"].dtype) in ("Int64", "int64"), df.dtypes.to_dict()
    assert df["open_interest"].iloc[0] == 12345


def test_a_missing_iv_stays_missing_and_does_not_become_zero(store):
    """IV coverage floors out around October 2022; older bars legitimately have none. A
    silent 0.0 would look like a free option to every downstream selector."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [
        _bar(d=date(2023, 1, 3), iv=None, oi=None),
        _bar(d=date(2023, 1, 4), iv=0.25, oi=7),
    ], *WINDOW, empty_contracts=[])
    df = store.read_partition("AAPL", exp).sort_values("bar_date")
    assert pd.isna(df["iv"].iloc[0])
    assert pd.isna(df["open_interest"].iloc[0])
    assert float(df["iv"].iloc[1]) == 0.25


def test_every_declared_column_is_present_even_when_all_values_are_null(store):
    """An all-None column must not be dropped or typed away — a later concat across
    partitions would then fail or silently lose the field."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(iv=None, oi=None)], *WINDOW, empty_contracts=[])
    df = store.read_partition("AAPL", exp)
    assert set(OptionHistoryParquetStore.COLUMNS) <= set(df.columns)


def test_contract_identity_columns_are_derived_from_the_occ_symbol(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(occ="AAPL230120P00147500")], *WINDOW,
                          empty_contracts=[])
    r = store.read_partition("AAPL", exp).iloc[0]
    assert r["underlying"] == "AAPL"
    assert r["option_type"] == "put"
    assert float(r["strike"]) == 147.5
    assert str(r["expiry"]) == "2023-01-20"


def test_prices_and_dates_round_trip(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(d=date(2023, 1, 3), close=7.25)], *WINDOW,
                          empty_contracts=[])
    r = store.read_partition("AAPL", exp).iloc[0]
    assert (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) == \
        (7.0, 7.5, 6.8, 7.25)
    assert str(r["bar_date"]) == "2023-01-03"
    assert int(r["volume"]) == 911


# --------------------------------------------------------------------------- #
# manifest content
# --------------------------------------------------------------------------- #
def test_manifest_records_the_frozen_clock_window_counts_and_schema_version(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(), _bar(d=date(2023, 1, 4))], *WINDOW,
                          empty_contracts=["AAPL230120C00990000"])
    m = json.loads(open(store.manifest_path("AAPL", exp)).read())
    assert m["status"] == "complete"
    assert m["underlying"] == "AAPL" and m["expiry"] == "2023-01-20"
    assert m["start"] == "2023-01-01" and m["end"] == "2026-03-01"
    assert m["rows"] == 2 and m["contracts"] == 1
    assert m["empty_contracts"] == ["AAPL230120C00990000"]
    assert m["written_at"] == FROZEN.isoformat()
    assert m["schema_version"] == OptionHistoryParquetStore.SCHEMA_VERSION


def _rewrite_manifest(store, underlying, expiry, **changes):
    p = store.manifest_path(underlying, expiry)
    m = json.loads(open(p).read())
    for k, v in changes.items():
        if v is _DROP:
            m.pop(k, None)
        else:
            m[k] = v
    with open(p, "w") as f:
        json.dump(m, f)


_DROP = object()


@pytest.mark.parametrize("dropped", ["start", "end"])
def test_a_manifest_with_no_recorded_window_is_not_trusted(store, dropped):
    """Without a window there is no way to know what the partition covers, and "assume it
    covers whatever you asked for" is the screener metric store's bug all over again."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    _rewrite_manifest(store, "AAPL", exp, **{dropped: _DROP})
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.STALE
    assert not store.is_done("AAPL", exp, *WINDOW)


def test_a_manifest_with_an_unrecognised_status_is_not_trusted(store):
    """Only 'complete' and 'empty' mean done. Anything else — a future 'partial', a typo,
    a hand edit — must re-fetch rather than be waved through."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    _rewrite_manifest(store, "AAPL", exp, status="partial")
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.STALE
    assert not store.is_done("AAPL", exp, *WINDOW)


@pytest.mark.parametrize("body", ["[]", '["complete"]', '"complete"', "null", "42"])
def test_a_manifest_that_is_valid_json_but_not_an_object_is_not_trusted(store, body):
    """A JSON list would sail past a `json.load` guard and then blow up on `.get()` deep
    inside the resume check — mid-run, hours in."""
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    with open(store.manifest_path("AAPL", exp), "w") as f:
        f.write(body)
    assert store.read_manifest("AAPL", exp) is None
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.MISSING


def test_progress_separates_complete_empty_and_pending(store):
    """The progress line is what a multi-hour run is judged by; counting an already-known
    empty partition as outstanding work makes the ETA permanently wrong."""
    expiries = [date(2023, 1, 20), date(2023, 1, 27), date(2023, 2, 3), date(2023, 2, 10)]
    store.write_partition("AAPL", expiries[0], [_bar()], *WINDOW, empty_contracts=[])
    store.write_partition("AAPL", expiries[1], [], *WINDOW, empty_contracts=["X"])
    p = store.progress("AAPL", expiries, *WINDOW)
    assert (p.total, p.complete, p.empty, p.pending) == (4, 1, 1, 2)


def test_the_root_follows_a_rebound_cache_folder(store):
    """CACHE_FOLDER must be read at CALL time. Capturing it at import binds whatever the
    value was when the module first loaded, which sends every test write — and every
    BA2_HOME override — into the real ~/Documents cache tree."""
    import ba2_common.config as cfg
    s = OptionHistoryParquetStore()
    original = cfg.CACHE_FOLDER
    try:
        cfg.CACHE_FOLDER = "/tmp/some-other-cache-root"
        assert s.root == os.path.join("/tmp/some-other-cache-root",
                                      "TastyTradeOptionsProvider")
    finally:
        cfg.CACHE_FOLDER = original


def test_a_manifest_from_a_future_schema_version_is_not_trusted(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    p = store.manifest_path("AAPL", exp)
    m = json.loads(open(p).read())
    m["schema_version"] = OptionHistoryParquetStore.SCHEMA_VERSION + 1
    with open(p, "w") as f:
        json.dump(m, f)
    assert store.partition_state("AAPL", exp, *WINDOW) is PartitionState.STALE


# --------------------------------------------------------------------------- #
# aggregate reads / progress reporting
# --------------------------------------------------------------------------- #
def test_completed_partitions_lists_what_is_on_disk(store):
    store.write_partition("AAPL", date(2023, 1, 20), [_bar()], *WINDOW, empty_contracts=[])
    store.write_partition("AAPL", date(2023, 1, 27), [], *WINDOW, empty_contracts=["X"])
    store.write_partition("MSFT", date(2023, 1, 20), [_bar(occ="MSFT230120C00250000")],
                          *WINDOW, empty_contracts=[])
    assert store.completed_expiries("AAPL") == [date(2023, 1, 20), date(2023, 1, 27)]
    assert store.completed_expiries("MSFT") == [date(2023, 1, 20)]
    assert store.completed_expiries("TSLA") == []


def test_read_underlying_concatenates_every_partition(store):
    store.write_partition("AAPL", date(2023, 1, 20), [_bar(d=date(2023, 1, 3))],
                          *WINDOW, empty_contracts=[])
    store.write_partition("AAPL", date(2023, 1, 27), [
        _bar(occ="AAPL230127C00150000", d=date(2023, 1, 4)),
        _bar(occ="AAPL230127C00150000", d=date(2023, 1, 5)),
    ], *WINDOW, empty_contracts=[])
    df = store.read_underlying("AAPL")
    assert len(df) == 3
    assert set(df["occ_symbol"]) == {"AAPL230120C00150000", "AAPL230127C00150000"}


def test_read_underlying_on_an_unknown_symbol_returns_none(store):
    assert store.read_underlying("NOPE") is None


# --------------------------------------------------------------------------- #
# discovery cache — enumerating 3.5 years of chain is itself slow
# --------------------------------------------------------------------------- #
def _meta(underlying, expiry, strike, right="call"):
    from ba2_common.core.interfaces import OptionContractMeta
    from ba2_providers.options.tastytrade import occ_symbol
    return OptionContractMeta(occ_symbol(underlying, expiry, right[0], strike),
                              underlying, right, strike, expiry)


def test_the_contract_list_round_trips(store):
    cs = [_meta("AAPL", date(2023, 1, 20), 150.0),
          _meta("AAPL", date(2023, 1, 27), 147.5, "put")]
    store.write_contracts("AAPL", cs, *WINDOW)
    got = store.read_contracts("AAPL", *WINDOW)
    assert got == cs


def test_an_absent_contract_list_reads_as_none(store):
    assert store.read_contracts("AAPL", *WINDOW) is None


def test_a_contract_list_fetched_over_a_narrower_window_is_not_reused(store):
    store.write_contracts("AAPL", [_meta("AAPL", date(2023, 1, 20), 150.0)],
                          date(2024, 1, 1), date(2026, 3, 1))
    assert store.read_contracts("AAPL", date(2024, 1, 1), date(2026, 3, 1)) is not None
    assert store.read_contracts("AAPL", date(2023, 1, 1), date(2026, 3, 1)) is None


def test_a_corrupt_contract_list_reads_as_none_rather_than_raising(store):
    store.write_contracts("AAPL", [_meta("AAPL", date(2023, 1, 20), 150.0)], *WINDOW)
    with open(store.contracts_path("AAPL"), "w") as f:
        f.write("{ truncated")
    assert store.read_contracts("AAPL", *WINDOW) is None


def test_the_contract_list_is_written_atomically(store):
    exp_path = store.contracts_path("AAPL")
    real_replace = os.replace

    def die(src, dst):
        if str(dst).endswith("_contracts.json"):
            raise KeyboardInterrupt("killed")
        return real_replace(src, dst)

    import ba2_providers.options.parquet_store as ps
    ps.os.replace = die
    try:
        with pytest.raises(KeyboardInterrupt):
            store.write_contracts("AAPL", [_meta("AAPL", date(2023, 1, 20), 150.0)], *WINDOW)
    finally:
        ps.os.replace = real_replace
    assert not os.path.exists(exp_path)
    assert store.read_contracts("AAPL", *WINDOW) is None


def test_writing_a_second_time_replaces_rather_than_duplicates(store):
    exp = date(2023, 1, 20)
    store.write_partition("AAPL", exp, [_bar(), _bar(d=date(2023, 1, 4))], *WINDOW,
                          empty_contracts=[])
    store.write_partition("AAPL", exp, [_bar()], *WINDOW, empty_contracts=[])
    assert len(store.read_partition("AAPL", exp)) == 1
