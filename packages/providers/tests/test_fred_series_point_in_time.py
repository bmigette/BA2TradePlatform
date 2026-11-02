"""Point-in-time correctness for the FRED series cache.

The bug these guard against: a backtest standing on 2024-01-31 seeing January's
unemployment rate, which was not published until 2024-02-02. Filtering on the
observation date leaks a month of hindsight into every macro regime call.

No network: each test writes a synthetic cache file and reads it back.
"""
import json
import os

import pandas as pd
import pytest

from ba2_providers.macro import fred_series as fs


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "CACHE_FOLDER", str(tmp_path))
    fs.reset_cache()
    yield
    fs.reset_cache()


def _write(series_id: str, vintage: bool, observations: list, *, stamp: bool = True,
           first_vintage: str = "2000-01-03", fetched_at: str = None) -> None:
    """A cache file in the format ``refresh_series`` writes.

    A first-release series gets the ``first_release_v1`` header; a row with no
    ``realtime_start`` is stamped with its own observation date (published the same day,
    like T10Y3M) unless ``stamp=False``. DGS3MO (same-day) keeps the legacy header.
    ``vintage`` is kept only so the call sites read as they always did.
    """
    os.makedirs(os.path.join(fs.CACHE_FOLDER, "fred"), exist_ok=True)
    if fs.SERIES_SPEC[series_id]["availability"] == fs.AVAIL_SAME_DAY_CLOSE:
        doc = {"series_id": series_id, "vintage": False, "observations": observations}
    else:
        rows = []
        for o in observations:
            o = dict(o)
            if stamp and "realtime_start" not in o:
                o["realtime_start"] = o.get("date")
            rows.append(o)
        doc = {"series_id": series_id,
               "fetched_at": fetched_at or pd.Timestamp.now(tz="UTC").isoformat(),
               "availability": fs.AVAIL_FIRST_RELEASE, "format": fs.CACHE_FORMAT_FIRST_RELEASE,
               "first_vintage": first_vintage, "observations": rows}
    with open(fs.cache_path(series_id), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_vintage_series_hides_observations_not_yet_published():
    """UNRATE for January is dated 2024-01-01 but first published 2024-02-02.

    Bar 2024-02-01 is the live decision of 2024-02-02 (it fills at that open); the release
    came out at 08:30 ET THAT morning, and a vintage dated L is not visible on L -- so it is
    still hidden. Bar 2024-02-02 is the decision of 2024-02-05, which sees it.
    """
    _write("UNRATE", True, [
        {"date": "2023-12-01", "value": "3.7", "realtime_start": "2024-01-05"},
        {"date": "2024-01-01", "value": "3.9", "realtime_start": "2024-02-02"},
    ])

    before = fs.get_series_as_of("UNRATE", "2024-02-01")
    assert len(before) == 1
    assert str(before.index[-1].date()) == "2023-12-01", (
        "January's reading leaked before its publication date")

    on_release = fs.get_series_as_of("UNRATE", "2024-02-02")
    assert len(on_release) == 2
    assert str(on_release.index[-1].date()) == "2024-01-01"
    assert on_release.iloc[-1] == pytest.approx(3.9)


def test_the_option_rate_series_still_cuts_on_observation_date():
    """DGS3MO is the option BS rate: same-day by design (it inverts bar d's own close)."""
    _write("DGS3MO", False, [
        {"date": "2024-03-14", "value": "5.40"},
        {"date": "2024-03-15", "value": "5.41"},
        {"date": "2024-03-18", "value": "5.33"},
    ])

    s = fs.get_series_as_of("DGS3MO", "2024-03-15")
    assert len(s) == 2
    assert s.iloc[-1] == pytest.approx(5.41)


def test_as_of_none_is_the_live_decision_made_now():
    """Live sees every row first published before today's decision label."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
    ])
    assert len(fs.get_series_as_of("VIXCLS", None)) == 2


def test_missing_values_are_skipped_not_zeroed():
    """FRED marks gaps with '.'; coercing those to 0.0 would poison a z-score."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "."},
    ])
    s = fs.get_series_as_of("VIXCLS", None)
    assert len(s) == 1
    assert s.iloc[-1] == pytest.approx(14.40)


def test_unknown_series_raises_rather_than_guessing_vintage_mode():
    with pytest.raises(ValueError, match="Add it to SERIES_SPEC"):
        fs.get_series_as_of("NOTASERIES", None)


def test_missing_cache_file_raises_pointing_at_prewarm():
    """A backtest must fail loudly, not silently fetch or return an empty series -- and not
    with an OSError, which the expert's broad handler absorbs (review 2026-09-26, I2)."""
    from ba2_providers.fmp_common import frozen_ttl_cache

    with frozen_ttl_cache():
        with pytest.raises(fs.MacroAvailabilityUnknown, match="prewarm"):
            fs.get_series_as_of("UNRATE", "2024-03-15")


def test_napm_is_not_in_the_spec():
    """ISM pulled FRED licensing; NAPM 404s. It must not reappear by copy-paste."""
    assert "NAPM" not in fs.SERIES_SPEC


def test_ice_bofa_hy_oas_is_not_in_the_spec():
    """FRED serves ICE indices on a rolling ~3y licence -- unusable pre-2023."""
    assert "BAMLH0A0HYM2" not in fs.SERIES_SPEC
    assert "BAA10Y" in fs.SERIES_SPEC


# --------------------------------------------------------------------------- #
# The parse-once memo (_ParsedSeries). ``get_series_as_of`` used to re-parse every
# raw row on every call -- 633k strptime calls and 62% of a real DeterministicScorer
# backtest. The parse is now memoized per payload; these pin that the memo is a
# SPEED structure and never a source of stale data, which matters because this
# function is on the LIVE path too.
# --------------------------------------------------------------------------- #

def test_the_parse_is_reused_across_different_as_of_cuts():
    """Second and later cuts must not re-walk the raw rows."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
        {"date": "2024-03-18", "value": "14.33"},
    ])
    assert len(fs.get_series_as_of("VIXCLS", "2024-03-14")) == 1
    parsed_after_first = fs._PARSED["VIXCLS"][1]
    assert len(fs.get_series_as_of("VIXCLS", "2024-03-18")) == 3
    assert fs._PARSED["VIXCLS"][1] is parsed_after_first, (
        "a second cut rebuilt the parse instead of filtering the memoized one")


def test_a_reloaded_payload_is_reparsed_even_if_only_the_row_memo_was_dropped():
    """The LIVE staleness rail: the parse is keyed on the ROWS OBJECT, not the id.

    ``_fill_cache_on_the_live_path`` can rewrite a series mid-process, after which
    ``_load`` returns a NEW list. A memo keyed on the series id alone would keep
    serving the old macro data for the life of the process -- which, with
    /api/reload not calling the expert's own reset, is the whole process lifetime.
    """
    _write("VIXCLS", False, [{"date": "2024-03-14", "value": "14.40"}])
    assert fs.get_series_as_of("VIXCLS", None).iloc[-1] == pytest.approx(14.40)

    # New file, and ONLY the raw-row memo dropped -- exactly what a refill does.
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "99.99"},
    ])
    fs._MEM.pop("VIXCLS", None)

    s = fs.get_series_as_of("VIXCLS", None)
    assert len(s) == 2 and s.iloc[-1] == pytest.approx(99.99), (
        "the parse memo served rows that no longer exist on disk")


def test_reset_cache_drops_the_parse_as_well_as_the_rows():
    _write("VIXCLS", False, [{"date": "2024-03-14", "value": "14.40"}])
    fs.get_series_as_of("VIXCLS", None)
    assert fs._PARSED and fs._MEM
    fs.reset_cache()
    assert not fs._PARSED and not fs._MEM


def test_rows_with_an_unparseable_date_are_dropped_exactly_as_before():
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "not-a-date", "value": "1.0"},
        {"value": "2.0"},                         # no "date" key at all
        {"date": "2024-03-15", "value": "14.41"},
    ])
    s = fs.get_series_as_of("VIXCLS", None)
    assert len(s) == 2
    assert list(s.values) == pytest.approx([14.40, 14.41])


def test_a_null_date_is_kept_as_nat_rather_than_cut_away():
    """``pd.Timestamp(None)`` is NaT, and ``NaT > cut`` is False, so the original
    loop's ``if known_on > cut: continue`` KEPT such a row. Spelling the vectorized
    filter as ``known <= cut`` would silently start dropping it."""
    _write("VIXCLS", False, [
        {"date": None, "value": "1.0", "realtime_start": "2024-03-14"},
        {"date": "2024-03-14", "value": "14.40"},
    ])
    assert len(fs.get_series_as_of("VIXCLS", "2024-03-14")) == 2
    assert len(fs.get_series_as_of("VIXCLS", None)) == 2


def test_a_row_with_no_value_key_raises_only_once_the_cut_reaches_it():
    """The one drop the original did NOT make unconditionally: ``row["value"]``
    raises KeyError, but it was reached only for rows inside the cut."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15"},                   # no "value" key
    ])
    assert len(fs.get_series_as_of("VIXCLS", "2024-03-14")) == 1
    with pytest.raises(KeyError):
        fs.get_series_as_of("VIXCLS", "2024-03-15")
    with pytest.raises(KeyError):
        fs.get_series_as_of("VIXCLS", None)


def test_duplicate_observation_dates_keep_their_original_order():
    """``.sort_index()`` is not a stable sort, so the PRE-sort order is part of the
    answer: the filter must hand pandas the rows in raw-file order."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "1.0"},
        {"date": "2024-03-14", "value": "2.0"},
        {"date": "2024-03-14", "value": "3.0"},
    ])
    assert list(fs.get_series_as_of("VIXCLS", None).values) == pytest.approx([1.0, 2.0, 3.0])


def test_vintage_cut_still_reads_realtime_start_after_memoization():
    _write("UNRATE", True, [
        {"date": "2023-12-01", "value": "3.7", "realtime_start": "2024-01-05"},
        {"date": "2024-01-01", "value": "3.9", "realtime_start": "2024-02-02"},
    ])
    assert len(fs.get_series_as_of("UNRATE", "2024-02-01")) == 1
    assert len(fs.get_series_as_of("UNRATE", "2024-02-02")) == 2
    # A row with no first-release date is REFUSED (it used to be dropped): when it became
    # public is unknown, and dropping it would quietly hide a row live has.
    fs.reset_cache()
    _write("UNRATE", True, [
        {"date": "2023-12-01", "value": "3.7", "realtime_start": "2024-01-05"},
        {"date": "2024-01-01", "value": "3.9"},          # no realtime_start
    ], stamp=False)
    with pytest.raises(fs.MacroAvailabilityUnknown, match="first-release date"):
        fs.get_series_as_of("UNRATE", None)


def test_an_intraday_instant_is_labelled_by_its_new_york_date():
    """A non-midnight instant (intraday bar, recorded live decision) is its NY date's decision:
    14:00 UTC on 03-15 is 10:00 ET on 03-15, which sees the 03-14 print, not 03-15's."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
    ])
    aware = pd.Timestamp("2024-03-15 14:00", tz="UTC")
    assert len(fs.get_series_as_of("VIXCLS", aware)) == 1


def test_the_returned_series_never_aliases_the_memo():
    """A caller that writes into the result must not corrupt the next reader."""
    _write("VIXCLS", False, [
        {"date": "2024-03-14", "value": "14.40"},
        {"date": "2024-03-15", "value": "14.41"},
    ])
    first = fs.get_series_as_of("VIXCLS", None)
    first.iloc[:] = -1.0
    assert list(fs.get_series_as_of("VIXCLS", None).values) == pytest.approx([14.40, 14.41])
