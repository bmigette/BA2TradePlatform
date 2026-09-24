"""``get_past_earnings`` parses each per-symbol history ONCE per payload.

WHY THIS EXISTS. The function is called once per (symbol, decision date), and its
per-call work was re-``strptime``-ing and re-building a dict for every row of an
immutable per-symbol history (AAPL = 164 rows back to 1985) before discarding all
but ``lookback_periods`` of them. Measured on a real 10-symbol / 501-bar
DeterministicScorer backtest: 5,010 calls, 10.7 s. Only the ``end_date`` cut and
the lookback slice depend on the caller, so the parse moved behind a memo.

The memo is on the LIVE path too, so the tests below pin BOTH halves: that the
parse is reused for the same payload, and that it can never be served for a
different one. ``fmp_history_disk_cached`` is a straight passthrough to the API
when the TTL freeze flag is not set, so a live call gets a fresh list object every
time and misses here by construction -- that is the staleness rail, and
``test_a_new_payload_object_is_reparsed`` is what holds it.
"""
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
    FMPCompanyDetailsProvider,
)

# The PACKAGE re-exports the class under the module's own name, so
# ``from ...details import FMPCompanyDetailsProvider`` hands back the class. The
# memo and its reset live on the MODULE.
mod = sys.modules["ba2_providers.fundamentals.details.FMPCompanyDetailsProvider"]

MODPATH = "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider.fmp_history_disk_cached"


@pytest.fixture(autouse=True)
def _clean_memo():
    mod.reset_past_earnings_parse_cache()
    yield
    mod.reset_past_earnings_parse_cache()


def _provider():
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "fake-key"
    return p


def _rows():
    """Newest-last, the order FMP serves; deliberately unsorted at the tail."""
    return [
        {"date": "2023-02-02", "eps": 1.88, "epsEstimated": 1.94, "time": "amc"},
        {"date": "2023-05-04", "eps": 1.52, "epsEstimated": 1.43, "time": "amc"},
        {"date": "2023-08-03", "eps": 1.26, "epsEstimated": 1.19, "time": "amc"},
        {"date": "2023-11-02", "eps": 1.46, "epsEstimated": 1.39, "time": "amc"},
        {"date": "2024-02-01", "eps": 2.18, "epsEstimated": 2.10, "time": "amc"},
    ]


def _earnings(provider, rows, end_date, lookback=8):
    with patch(MODPATH, return_value=rows):
        out = provider.get_past_earnings(
            "AAPL", frequency="quarterly", end_date=end_date,
            lookback_periods=lookback, format_type="dict")
    return out["earnings"]


def test_the_cut_and_the_lookback_still_decide_the_window():
    p, rows = _provider(), _rows()
    got = _earnings(p, rows, datetime(2023, 11, 2), lookback=8)
    assert [e["report_date"] for e in got] == [
        "2023-11-02", "2023-08-03", "2023-05-04", "2023-02-02"], "newest first, cut inclusive"

    got = _earnings(p, rows, datetime(2023, 11, 2), lookback=2)
    assert [e["report_date"] for e in got] == ["2023-11-02", "2023-08-03"]

    assert _earnings(p, rows, datetime(2022, 1, 1)) == [], "nothing published yet"


def test_a_zero_or_negative_lookback_slices_exactly_as_the_old_list_slice_did():
    """The old code did ``filtered[:lookback_periods]``; 0 means none and a negative
    drops from the tail. A ``len(...) >= lookback`` loop guard would get both wrong."""
    p, rows = _provider(), _rows()
    assert _earnings(p, rows, datetime(2024, 3, 1), lookback=0) == []
    got = _earnings(p, rows, datetime(2024, 3, 1), lookback=-2)
    assert [e["report_date"] for e in got] == ["2024-02-01", "2023-11-02", "2023-08-03"]


def test_the_same_payload_object_is_parsed_once():
    p, rows = _provider(), _rows()
    _earnings(p, rows, datetime(2023, 5, 4))
    parsed = mod._PAST_EARNINGS_PARSED["AAPL__quarterly"][1]
    _earnings(p, rows, datetime(2024, 3, 1))
    assert mod._PAST_EARNINGS_PARSED["AAPL__quarterly"][1] is parsed, (
        "a second cut re-parsed the history instead of filtering the memoized parse")


def test_a_new_payload_object_is_reparsed():
    """The LIVE staleness rail: live gets a fresh list from the API on every call."""
    p = _provider()
    assert len(_earnings(p, _rows(), datetime(2024, 3, 1))) == 5

    fresh = _rows() + [{"date": "2024-05-02", "eps": 1.53, "epsEstimated": 1.50,
                        "time": "amc"}]
    got = _earnings(p, fresh, datetime(2024, 6, 1))
    assert got[0]["report_date"] == "2024-05-02", (
        "the memo served a parse built from a payload the caller no longer has")


def test_annual_and_quarterly_histories_do_not_share_a_memo_entry():
    p = _provider()
    quarterly = _rows()
    annual = [{"date": "2023-09-30", "eps": 6.13, "epsEstimated": 6.05, "time": "amc"}]
    with patch(MODPATH, return_value=quarterly):
        q = p.get_past_earnings("AAPL", frequency="quarterly",
                                end_date=datetime(2024, 3, 1), lookback_periods=8,
                                format_type="dict")["earnings"]
    with patch(MODPATH, return_value=annual):
        a = p.get_past_earnings("AAPL", frequency="annual",
                                end_date=datetime(2024, 3, 1), lookback_periods=8,
                                format_type="dict")["earnings"]
    assert len(q) == 5 and len(a) == 1


def test_a_caller_mutating_the_result_cannot_poison_the_memo():
    p, rows = _provider(), _rows()
    first = _earnings(p, rows, datetime(2024, 3, 1))
    first[0]["reported_eps"] = -999
    assert _earnings(p, rows, datetime(2024, 3, 1))[0]["reported_eps"] == pytest.approx(2.18)


def test_unparseable_and_dateless_rows_are_still_dropped():
    p = _provider()
    rows = [
        {"date": "2023-02-02", "eps": 1.88, "epsEstimated": 1.94},
        {"date": "", "eps": 1.0, "epsEstimated": 1.0},
        {"eps": 1.0, "epsEstimated": 1.0},
        {"date": "the-third-of-never", "eps": 1.0, "epsEstimated": 1.0},
    ]
    got = _earnings(p, rows, datetime(2024, 3, 1))
    assert [e["report_date"] for e in got] == ["2023-02-02"]


def test_surprise_fields_and_key_order_are_unchanged():
    p = _provider()
    rows = [
        {"date": "2023-02-02", "eps": 1.88, "epsEstimated": 1.94, "time": "amc"},
        {"date": "2023-05-04", "eps": 1.52, "epsEstimated": 0, "time": "bmo"},
    ]
    got = _earnings(p, rows, datetime(2024, 3, 1))
    newest, older = got[0], got[1]
    assert list(newest.keys()) == [
        "fiscal_date_ending", "report_date", "reported_eps", "estimated_eps",
        "time", "surprise", "surprise_percent"]
    assert newest["surprise"] is None, "no estimate -> no surprise, never a zero"
    assert older["surprise"] == pytest.approx(1.88 - 1.94)
    assert older["surprise_percent"] == pytest.approx((1.88 - 1.94) / 1.94 * 100)


def test_a_tz_aware_end_date_still_compares_against_naive_fmp_dates():
    p, rows = _provider(), _rows()
    got = _earnings(p, rows, datetime(2023, 11, 2, tzinfo=timezone.utc))
    assert [e["report_date"] for e in got][0] == "2023-11-02"
