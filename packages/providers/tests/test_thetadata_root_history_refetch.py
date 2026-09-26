"""A ROOT_HISTORY entry reaches the partitions already on disk (stage-2 repair, 2026-09-26).

The ThetaData provider honours ``ROOT_HISTORY`` for every NEW request, but the warm tool skips a
partition whose manifest says complete -- so adding an alias (XYZ <- SQ) or a None root (ECHO
before its listing) used to leave every partition fetched under the old assumption in place,
and the stage-2 repair had to select, stage and swap them by hand (repair/theta_select.py,
theta_stage.py). Now:

* every partition the ThetaData warm writes records the symbol's ``root_history`` signature;
* a partition whose recorded signature differs from today's is STALE (re-fetched) when its
  expiry is within reach of a cutover (a contract lists at most ``LISTING_HORIZON_DAYS``
  before it expires);
* a LEGACY manifest (no signature) of a symbol with a root history is STALE when
  - an old ROOT applies (alias) and it was written before the entry was applied
    (``ROOT_HISTORY_LEGACY_FETCHED``; a symbol missing there never was), or
  - a ``None`` root applies and the partition holds a bar before that cutover (content: a
    pre-listing chain belongs to another company);
* a symbol without a root history is never touched.

No network.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.interfaces import OptionEodBar
from ba2_providers.options import thetadata as T
from ba2_providers.options.parquet_store import OptionHistoryParquetStore, PartitionState
from ba2_providers.options.tastytrade import occ_symbol

UTC = timezone.utc


def _m(written_at, **kw):
    m = {"schema_version": 1, "status": "complete", "start": "2020-01-01", "end": "2026-09-25",
         "written_at": written_at.isoformat()}
    m.update(kw)
    return m


def _never():
    raise AssertionError("the first bar must not be read for this case")


def test_the_signature_names_every_cutover_and_root():
    assert T.root_history_signature("META") == "2022-06-09:FB"
    assert T.root_history_signature("ecHo") == "2026-05-26:-"
    assert T.root_history_signature("AAPL") == ""


def test_the_legacy_table_is_frozen_and_utc():
    """Only the aliases whose partitions were re-fetched BEFORE signatures existed: an alias added
    later is NOT listed, so every legacy partition of it in reach is re-fetched."""
    assert set(T.ROOT_HISTORY_LEGACY_FETCHED) == {"META", "IR", "CZR", "COHR", "BN", "B", "DOC",
                                                  "LUMN", "VTRS", "BALL", "XYZ"}
    aliases = {s for s, h in T.ROOT_HISTORY.items() if any(r is not None for _c, r in h)}
    assert set(T.ROOT_HISTORY_LEGACY_FETCHED) <= aliases
    assert all(t.tzinfo is not None for t in T.ROOT_HISTORY_LEGACY_FETCHED.values())


def test_a_symbol_without_history_is_never_stale():
    old = datetime(2020, 1, 1, tzinfo=UTC)
    assert T.root_history_refetch_reason("AAPL", date(2021, 6, 18), _m(old), _never) is None
    assert T.root_history_refetch_reason("AAPL", date(2021, 6, 18), _m(old, root_history=""), _never) is None


def test_a_recorded_signature_that_still_matches_is_kept_and_a_changed_one_is_refetched():
    t = datetime(2026, 9, 1, tzinfo=UTC)
    sig = T.root_history_signature("XYZ")
    assert T.root_history_refetch_reason("XYZ", date(2023, 6, 16), _m(t, root_history=sig), _never) is None
    r = T.root_history_refetch_reason("XYZ", date(2023, 6, 16), _m(t, root_history=""), _never)
    assert r and "root history" in r and sig in r
    # Out of reach of every cutover: nothing a root change could have altered.
    far = date(2025, 1, 21) + timedelta(days=T.LISTING_HORIZON_DAYS + 7)
    assert T.root_history_refetch_reason("XYZ", far, _m(t, root_history=""), _never) is None


def test_a_legacy_alias_partition_is_refetched_unless_written_after_the_entry_was_applied():
    applied = T.ROOT_HISTORY_LEGACY_FETCHED["XYZ"]
    before, after = applied - timedelta(hours=3), applied + timedelta(minutes=5)
    r = T.root_history_refetch_reason("XYZ", date(2022, 6, 17), _m(before), _never)
    assert r and "SQ" in r
    assert T.root_history_refetch_reason("XYZ", date(2022, 6, 17), _m(after), _never) is None
    far = date(2025, 1, 21) + timedelta(days=T.LISTING_HORIZON_DAYS + 7)
    assert T.root_history_refetch_reason("XYZ", far, _m(before), _never) is None


def test_a_new_alias_entry_refetches_every_legacy_partition_in_reach(monkeypatch):
    monkeypatch.setitem(T.ROOT_HISTORY, "NEWCO", ((date(2023, 3, 1), "OLDCO"),))
    late = datetime(2030, 1, 1, tzinfo=UTC)          # however recent: nothing proves the alias
    r = T.root_history_refetch_reason("NEWCO", date(2023, 6, 16), _m(late), _never)
    assert r and "OLDCO" in r


@pytest.mark.parametrize("first_bar,stale", [(date(2026, 5, 1), True), (date(2026, 5, 26), False),
                                             (None, False)])
def test_a_legacy_none_root_partition_is_judged_by_its_bars(first_bar, stale):
    """ECHO: no chain before 2026-05-26. A legacy partition holding an earlier bar holds another
    company's chain; one that starts at the listing (or is empty) is exactly right, whenever it
    was written."""
    old = datetime(2026, 9, 6, tzinfo=UTC)
    r = T.root_history_refetch_reason("ECHO", date(2026, 7, 17), _m(old), lambda: first_bar)
    assert bool(r) is stale


# ------------------------------------------------------------------- through the parquet store
def _bars(underlying, expiry, days):
    occ = occ_symbol(underlying, expiry, "C", 100.0)
    return [OptionEodBar(occ_symbol=occ, bar_date=d, open=1.0, high=1.1, low=0.9, close=1.0,
                         volume=10, open_interest=5, iv=0.3) for d in days]


def test_the_store_stamps_the_signature_and_reports_a_legacy_alias_partition_stale(tmp_path):
    root = str(tmp_path / "theta")
    legacy_time = T.ROOT_HISTORY_LEGACY_FETCHED["XYZ"] - timedelta(days=30)
    plain = OptionHistoryParquetStore(root=root, clock=lambda: legacy_time)
    exp = date(2022, 6, 17)
    plain.write_partition("XYZ", exp, _bars("XYZ", exp, [date(2022, 6, 1)]), date(2020, 1, 1), exp)
    assert "root_history" not in plain.read_manifest("XYZ", exp)

    hooked = OptionHistoryParquetStore(root=root, **T.parquet_store_hooks())
    assert hooked.partition_state("XYZ", exp, date(2020, 1, 1), exp) is PartitionState.STALE
    assert "SQ" in hooked.stale_reasons[("XYZ", exp)]
    # A plain store (every reader) is unaffected.
    assert plain.partition_state("XYZ", exp, date(2020, 1, 1), exp) is PartitionState.COMPLETE

    hooked.write_partition("XYZ", exp, _bars("XYZ", exp, [date(2021, 6, 1), date(2022, 6, 1)]),
                           date(2020, 1, 1), exp)
    assert hooked.read_manifest("XYZ", exp)["root_history"] == T.root_history_signature("XYZ")
    assert hooked.partition_state("XYZ", exp, date(2020, 1, 1), exp) is PartitionState.COMPLETE


def test_the_store_reads_the_first_bar_for_a_none_root_partition(tmp_path):
    root = str(tmp_path / "theta")
    old = datetime(2026, 9, 6, tzinfo=UTC)
    plain = OptionHistoryParquetStore(root=root, clock=lambda: old)
    exp = date(2026, 7, 17)
    plain.write_partition("ECHO", exp, _bars("ECHO", exp, [date(2026, 4, 1), date(2026, 6, 1)]),
                          date(2020, 1, 1), exp)
    hooked = OptionHistoryParquetStore(root=root, **T.parquet_store_hooks())
    assert hooked.partition_state("ECHO", exp, date(2020, 1, 1), exp) is PartitionState.STALE
    plain.write_partition("ECHO", exp, _bars("ECHO", exp, [date(2026, 6, 1)]), date(2020, 1, 1), exp)
    assert hooked.partition_state("ECHO", exp, date(2020, 1, 1), exp) is PartitionState.COMPLETE


# ------------------------------------------------ review: an empty re-fetch never erases rows
def test_a_stale_partition_whose_refetch_comes_back_empty_keeps_its_rows(tmp_path):
    """A partition that held rows is not overwritten by an EMPTY re-fetch (a vendor hiccup, a
    wrong alias) and is not signed: the old file stays, and the store reports it."""
    root = str(tmp_path / "theta")
    legacy = OptionHistoryParquetStore(root=root, clock=lambda: datetime(2026, 9, 1, tzinfo=UTC))
    exp = date(2023, 6, 16)
    legacy.write_partition("XYZ", exp, _bars("XYZ", exp, [date(2023, 6, 1)]), date(2020, 1, 1), exp)
    before = open(legacy.bars_path("XYZ", exp), "rb").read()
    m_before = legacy.read_manifest("XYZ", exp)
    hooked = OptionHistoryParquetStore(root=root, **T.parquet_store_hooks())
    got = hooked.write_partition("XYZ", exp, [], date(2020, 1, 1), exp, empty_contracts=["X"])
    assert open(hooked.bars_path("XYZ", exp), "rb").read() == before
    assert hooked.read_manifest("XYZ", exp) == m_before and "root_history" not in m_before
    assert ("XYZ", exp) in hooked.kept_nonempty and got.get("kept_old")


def test_a_none_root_cutover_may_empty_a_partition_of_the_old_company(tmp_path):
    """ECHO: every row of the partition lies before 2026-05-26 (another company's chain), so the
    empty re-fetch is the correct answer and is written."""
    root = str(tmp_path / "theta")
    legacy = OptionHistoryParquetStore(root=root, clock=lambda: datetime(2026, 9, 1, tzinfo=UTC))
    exp = date(2021, 6, 18)
    legacy.write_partition("ECHO", exp, _bars("ECHO", exp, [date(2021, 6, 1)]), date(2020, 1, 1), exp)
    hooked = OptionHistoryParquetStore(root=root, **T.parquet_store_hooks())
    m = hooked.write_partition("ECHO", exp, [], date(2020, 1, 1), exp, empty_contracts=["X"])
    assert m["status"] == "empty" and not os.path.exists(hooked.bars_path("ECHO", exp))
    assert m["root_history"] == T.root_history_signature("ECHO") and not hooked.kept_nonempty


def test_a_plain_store_still_writes_empty(tmp_path):
    root = str(tmp_path / "theta")
    plain = OptionHistoryParquetStore(root=root)
    exp = date(2023, 6, 16)
    plain.write_partition("AAPL", exp, _bars("AAPL", exp, [date(2023, 6, 1)]), date(2020, 1, 1), exp)
    assert plain.write_partition("AAPL", exp, [], date(2020, 1, 1), exp)["status"] == "empty"
