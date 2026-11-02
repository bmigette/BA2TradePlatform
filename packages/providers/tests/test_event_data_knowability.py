"""Event data is visible at an INTRADAY decision only once it was public (ba2_common.core.knowability).

At a 09:30 decision on day D the legacy readers kept everything dated ``<= D`` (dates parsed at
00:00). Live at 09:30 has: an ``amc`` earnings row dated D WITHOUT its EPS yet, a ``bmo`` row dated D
with EPS, Form 4s filed up to 09:30 and no later, statements accepted before 09:30. Each test below
fails on the legacy reading, and each pins that LIVE / the DAILY clock (flag off) did not move.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ba2_common.core.knowability import (
    filing_visible_from, intraday_decisions, intraday_decision_clock, published_known,
    earnings_visible_from)
from ba2_common.core.provider_utils import insider_effective_date, statement_effective_date

DECISION = datetime(2024, 3, 5, 9, 30, tzinfo=timezone.utc)   # 09:30 New York wall time, as the engine labels it


# ------------------------------------------------------------------ the flag
def test_flag_is_scoped_and_thread_local():
    assert not intraday_decision_clock()
    with intraday_decisions(True):
        assert intraday_decision_clock()
        with intraday_decisions(False):
            assert not intraday_decision_clock()
        assert intraday_decision_clock()
    assert not intraday_decision_clock()


# ------------------------------------------------------------------ the rules
@pytest.mark.parametrize("raw, known", [
    ("2024-03-05 09:00:48", True),      # Form 4 filed before the open
    ("2024-03-05 09:30:00", True),      # at the instant
    ("2024-03-05 16:52:29", False),     # accepted after the close
    ("2024-03-05", False),              # date only: from the NEXT session
    ("2024-03-04", True),
    ("2024-03-05T10:36:01.000Z", True),   # UTC 10:36 = 05:36 New York
    ("2024-03-05T19:00:00.000Z", False),  # UTC 19:00 = 14:00 New York, after a 09:30 decision
    ("not-a-date", False),              # unparseable is never silently admitted
    (None, False),
])
def test_published_known(raw, known):
    assert published_known(raw, DECISION) is known


def test_earnings_slot_rule():
    d = datetime(2024, 3, 5)
    assert earnings_visible_from(d, "bmo") <= DECISION.replace(tzinfo=None)
    for slot in ("amc", "--", None, ""):
        assert earnings_visible_from(d, slot) > DECISION.replace(tzinfo=None), slot
    assert earnings_visible_from(datetime(2024, 3, 4), "amc") <= DECISION.replace(tzinfo=None)


def test_filing_visible_from_keeps_the_time():
    assert filing_visible_from("2024-03-05 16:52:29") == datetime(2024, 3, 5, 16, 52, 29)
    assert filing_visible_from("2024-03-05") == datetime(2024, 3, 6)


# ------------------------------------------------------------------ effective dates
STATEMENT = {"fillingDate": "2024-03-05", "acceptedDate": "2024-03-05 16:52:29", "date": "2023-12-31"}
FORM4 = {"filingDate": "2024-03-05 16:30:00", "transactionDate": "2024-03-01"}


def test_legacy_effective_dates_unchanged_without_the_flag():
    # statements: the date-only fillingDate wins, so the 16:52 acceptance is ignored (THE LEAK)
    assert statement_effective_date(STATEMENT) == datetime(2024, 3, 5, tzinfo=timezone.utc)
    # Form 4: a space-separated filingDate already kept its time; a date-only one is the same day
    assert insider_effective_date(FORM4) == datetime(2024, 3, 5, 16, 30, tzinfo=timezone.utc)
    assert insider_effective_date({"filingDate": "2024-03-05"}) == datetime(2024, 3, 5, tzinfo=timezone.utc)


def test_statement_accepted_after_the_open_is_not_visible_at_0930():
    with intraday_decisions(True):
        eff = statement_effective_date(STATEMENT)
    assert eff == datetime(2024, 3, 5, 16, 52, 29, tzinfo=timezone.utc)
    assert eff > DECISION


def test_statement_without_an_accept_time_is_visible_from_the_next_session():
    with intraday_decisions(True):
        eff = statement_effective_date({"fillingDate": "2024-03-05", "date": "2023-12-31"})
    assert eff == datetime(2024, 3, 6, tzinfo=timezone.utc)


def test_date_only_form4_is_visible_from_the_next_session():
    with intraday_decisions(True):
        assert insider_effective_date({"filingDate": "2024-03-05"}) == datetime(2024, 3, 6, tzinfo=timezone.utc)


def test_form4_filed_after_0930_is_not_visible_but_one_before_is():
    with intraday_decisions(True):
        late = insider_effective_date(FORM4)
        early = insider_effective_date({"filingDate": "2024-03-05 09:00:48"})
    assert late > DECISION
    assert early <= DECISION


# ------------------------------------------------------------------ the earnings reader
def _earnings_provider():
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
    p = FMPCompanyDetailsProvider.__new__(FMPCompanyDetailsProvider)
    p.api_key = "fake-key"
    return p


ROWS = [
    {"date": "2024-03-05", "symbol": "AAA", "eps": 1.2, "epsEstimated": 1.0, "time": "amc"},
    {"date": "2023-12-05", "symbol": "AAA", "eps": 0.9, "epsEstimated": 0.9, "time": "amc"},
    {"date": "2024-03-04", "symbol": "BBB", "eps": 2.0, "epsEstimated": 1.0, "time": "bmo"},
]


def _latest(symbol, end_date, rows):
    import sys
    import ba2_providers.fundamentals.details.FMPCompanyDetailsProvider  # noqa: F401
    m = sys.modules["ba2_providers.fundamentals.details.FMPCompanyDetailsProvider"]
    clear = getattr(m._parse_past_earnings_rows, "cache_clear", None)
    if clear:
        clear()
    with patch.object(m, "fmp_history_disk_cached", return_value=[r for r in rows if r["symbol"] == symbol]):
        out = _earnings_provider().get_past_earnings(
            symbol, frequency="quarterly", end_date=end_date, lookback_periods=1, format_type="dict")
    return out["earnings"][0]["report_date"] if out["earnings"] else None


def test_amc_report_dated_today_is_visible_at_0930_on_the_legacy_reading_only():
    assert _latest("AAA", DECISION, ROWS) == "2024-03-05"          # legacy: the leak
    with intraday_decisions(True):
        assert _latest("AAA", DECISION, ROWS) == "2023-12-05"      # the amc print is after the close
        # ... and visible at the NEXT day's decision
        assert _latest("AAA", datetime(2024, 3, 6, 9, 30, tzinfo=timezone.utc), ROWS) == "2024-03-05"


def test_bmo_report_dated_today_stays_visible_at_0930():
    rows = [{"date": "2024-03-05", "symbol": "BBB", "eps": 2.0, "epsEstimated": 1.0, "time": "bmo"}]
    with intraday_decisions(True):
        assert _latest("BBB", DECISION, rows) == "2024-03-05"
