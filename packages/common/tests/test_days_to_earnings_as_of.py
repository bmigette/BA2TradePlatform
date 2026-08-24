"""``days_to_earnings`` measured two things wrong at once: the clock and the calendar.

**The clock.** ``evaluate()`` did ``(next_earnings - date.today()).days`` and
``_next_earnings_date`` filtered future estimates with ``date.today()`` while asking the
provider for ``as_of_date=datetime.now(timezone.utc)``. In a backtest that is the distance
from the REAL-WORLD today to the next earnings — identical on every simulated bar of every
year, and a live-vs-backtest divergence on a documented entry gate.

**The calendar.** ``get_earnings_estimates`` never passes a period parameter to FMP's
``/api/v3/analyst-estimates`` endpoint, which defaults to ANNUAL, so ``frequency="quarterly"``
was decorative: the rows are one per fiscal YEAR. Verified against the on-disk cache
(``~/Documents/ba2/common/cache/fmp_history``, 4,695 ``earnings_estimates_quarterly__*``
files) — every one of them holds 20 annual rows. What the two bugs produced together:

    symbol  simulated bar   TRUE next print   annual estimate   what the code returned
    MSFT    2024-03-15      2024-04-25 (41d)  2024-06-30 (107d) 2027-06-30 => 310d, every bar
    AAPL    2024-03-15      2024-05-02 (48d)  2024-09-27 (196d) 2026-09-27 =>  34d, every bar
    NVDA    2024-03-15      2024-05-22 (68d)  2025-01-25 (316d) 2027-01-25 => 154d, every bar

So the gate was a per-symbol CONSTANT: ``days_to_earnings <= 40`` was true on every bar for
AAPL and false on every bar for MSFT, whatever year the backtest ran. The documented
``iv_rank <= 30 and days_to_earnings <= 5 -> open_straddle`` rule
(``rules_documentation.py``) could not fire at all.

The fix reads FMP's quarterly earnings CALENDAR (``get_past_earnings`` — despite the name it
is ``historical_earning_calendar``, whose rows carry the actual announcement ``report_date``
and include already-scheduled future prints), and measures from the evaluation bar.

Every date here is frozen to 2024 while the wall clock is 2026: nothing can pass by agreeing
with today.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import DaysToEarningsCondition, create_condition
from ba2_common.core.types import ExpertEventType

SIM_BAR = date(2024, 3, 15)

#: A realistic quarterly cadence around SIM_BAR (MSFT's actual print dates).
QUARTERLY_REPORTS = ["2023-07-25", "2023-10-24", "2024-01-30", "2024-04-25", "2024-07-30"]
NEXT_QUARTERLY = date(2024, 4, 25)          # 41 days after SIM_BAR

#: What the annual analyst-estimates endpoint actually returns (fiscal year ends).
ANNUAL_ESTIMATES = ["2024-06-30", "2025-06-30", "2026-06-30", "2027-06-30"]
NEXT_ANNUAL = date(2024, 6, 30)             # 107 days after SIM_BAR


class _FakeFundamentals:
    """Stands in for ``FMPCompanyDetailsProvider``: a QUARTERLY calendar and an ANNUAL
    estimates series, recording exactly how each was asked for."""

    def __init__(self, reports=QUARTERLY_REPORTS, estimates=ANNUAL_ESTIMATES):
        self._reports = list(reports)
        self._estimates = list(estimates)
        self.past_calls = []
        self.estimate_calls = []

    def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                          format_type="markdown"):
        self.past_calls.append({"symbol": symbol, "frequency": frequency,
                                "end_date": end_date, "lookback_periods": lookback_periods})
        end = end_date.date() if isinstance(end_date, datetime) else end_date
        rows = [{"report_date": d, "fiscal_date_ending": d, "reported_eps": 1.0,
                 "estimated_eps": 1.0}
                for d in self._reports if date.fromisoformat(d) <= end]
        rows.sort(key=lambda r: r["report_date"], reverse=True)
        return {"symbol": symbol, "frequency": frequency,
                "earnings": rows[:lookback_periods]}

    def get_earnings_estimates(self, symbol, frequency, as_of_date, lookback_periods=4,
                               format_type="markdown"):
        self.estimate_calls.append({"symbol": symbol, "frequency": frequency,
                                    "as_of_date": as_of_date})
        as_of = as_of_date.date() if isinstance(as_of_date, datetime) else as_of_date
        rows = [{"fiscal_date_ending": d} for d in self._estimates
                if date.fromisoformat(d) >= as_of]
        return {"symbol": symbol, "estimates": rows[:lookback_periods]}


class _BacktestAccount:
    id = 1

    def _as_of_date(self):
        return SIM_BAR


class _LiveAccount:
    id = 1


def _rec():
    return SimpleNamespace(created_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                           instance_id=1, symbol="MSFT", data={})


@pytest.fixture
def fundamentals(monkeypatch):
    p = _FakeFundamentals()
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    return p


def _cond(account, op=">=", value=0.0):
    return create_condition(ExpertEventType.N_DAYS_TO_EARNINGS, account, "MSFT", _rec(),
                            operator_str=op, value=value)


# --------------------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------------------
def test_days_are_counted_from_the_simulated_bar(monkeypatch, fundamentals):
    """Isolate the clock: pin the earnings date and check only the subtraction."""
    fixed = SIM_BAR + timedelta(days=5)
    monkeypatch.setattr(DaysToEarningsCondition, "_next_earnings_date",
                        lambda self, symbol, as_of: fixed, raising=True)
    cond = _cond(_BacktestAccount(), "<=", 7.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 5, (
        f"days_to_earnings = {cond.calculated_value}; 5 is the distance from the SIMULATED "
        f"bar {SIM_BAR}, anything else is the distance from the real-world today"
    )


def test_the_next_earnings_lookup_is_given_the_simulated_bar(monkeypatch, fundamentals):
    """Not just the subtraction: the LOOKUP must be as-of too, or it filters "future"
    estimates against the wall clock and skips everything before it."""
    seen = {}

    def _spy(self, symbol, as_of):
        seen["as_of"] = as_of
        return SIM_BAR + timedelta(days=10)

    monkeypatch.setattr(DaysToEarningsCondition, "_next_earnings_date", _spy, raising=True)
    _cond(_BacktestAccount()).evaluate()
    assert seen["as_of"] == SIM_BAR


def test_live_still_measures_from_the_wall_clock(monkeypatch, fundamentals):
    """A live account has no simulated clock; ``date.today()`` is the right answer there."""
    fixed = date.today() + timedelta(days=3)
    monkeypatch.setattr(DaysToEarningsCondition, "_next_earnings_date",
                        lambda self, symbol, as_of: fixed, raising=True)
    cond = _cond(_LiveAccount(), "<=", 5.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 3


def test_a_broken_simulated_clock_is_unevaluable_not_wall_clock(monkeypatch, fundamentals):
    class _BrokenClock:
        id = 1

        def _as_of_date(self):
            raise RuntimeError("no simulated clock")

    monkeypatch.setattr(DaysToEarningsCondition, "_next_earnings_date",
                        lambda self, symbol, as_of: SIM_BAR, raising=True)
    cond = _cond(_BrokenClock(), "<=", 5.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


# --------------------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------------------
def test_the_next_earnings_date_is_the_next_QUARTERLY_report(fundamentals):
    """The annual analyst-estimates series would say 2024-06-30 (107 days). The real next
    print is 2024-04-25 (41 days)."""
    got = DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    assert got == NEXT_QUARTERLY, (
        f"next earnings resolved to {got}; {NEXT_QUARTERLY} is the next quarterly REPORT, "
        f"{NEXT_ANNUAL} is the annual fiscal-year-end the analyst-estimates endpoint returns"
    )


def test_the_calendar_is_asked_for_quarterly_rows_around_the_bar(fundamentals):
    DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    (call,) = fundamentals.past_calls
    assert call["frequency"] == "quarterly"
    end = call["end_date"]
    end_d = end.date() if isinstance(end, datetime) else end
    assert end_d > SIM_BAR, "the calendar must be read PAST the bar or it has no future row"
    assert end_d <= SIM_BAR + timedelta(days=400), (
        f"the forward horizon {end_d} is unboundedly far from the bar {SIM_BAR}"
    )


def test_a_report_dated_exactly_on_the_bar_counts_as_zero_days(monkeypatch):
    """Earnings day itself is 0 days away, not "already past" and not skipped."""
    p = _FakeFundamentals(reports=["2024-01-30", SIM_BAR.isoformat(), "2024-06-25"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    cond = _cond(_BacktestAccount(), "<=", 0.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 0


def test_the_documented_straddle_gate_can_actually_fire(monkeypatch):
    """``days_to_earnings <= 5`` is half of the documented
    ``iv_rank<=30 and days_to_earnings<=5 -> open_straddle`` rule. On an annual series the
    gate is a per-symbol constant that never lands inside 5 days; on the real quarterly
    calendar it must be true in the run-up to each print and false the rest of the time."""
    p = _FakeFundamentals()
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)

    class _Bar:
        id = 1

        def __init__(self, d):
            self._d = d

        def _as_of_date(self):
            return self._d

    fires = [d for d in (NEXT_QUARTERLY - timedelta(days=k) for k in range(0, 15))
             if create_condition(ExpertEventType.N_DAYS_TO_EARNINGS, _Bar(d), "MSFT", _rec(),
                                 operator_str="<=", value=5.0).evaluate()]
    assert fires, "the straddle entry gate can never fire"
    assert len(fires) == 6, f"expected the 6 bars within 5 days of the print, got {fires}"


def test_the_annual_estimates_are_the_fallback_not_the_source(monkeypatch):
    """When the calendar has no scheduled future print (a stale/short calendar), fall back to
    the analyst estimates rather than returning nothing — degraded, not absent."""
    p = _FakeFundamentals(reports=["2023-10-24", "2024-01-30"])  # nothing after the bar
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    got = DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    assert got == NEXT_ANNUAL
    assert p.estimate_calls, "the estimates fallback was never consulted"


def test_the_estimates_fallback_is_also_asked_as_of_the_bar(monkeypatch):
    """The fallback used ``as_of_date=datetime.now(timezone.utc)``, which in a backtest asks
    the provider to drop every estimate before the REAL today."""
    p = _FakeFundamentals(reports=["2023-10-24"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    (call,) = p.estimate_calls
    as_of = call["as_of_date"]
    as_of_d = as_of.date() if isinstance(as_of, datetime) else as_of
    assert as_of_d == SIM_BAR, f"estimates asked as of {as_of_d}, not the bar {SIM_BAR}"


def test_the_announcement_date_wins_over_the_fiscal_period_end(monkeypatch):
    """A calendar row carries both: ``report_date`` is when the number is ANNOUNCED (the
    volatility event the gate is about) and ``fiscal_date_ending`` is the period it covers,
    typically ~4 weeks earlier. Gating a straddle on the period end would open it a month
    before the move."""
    class _SplitDates(_FakeFundamentals):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                              format_type="markdown"):
            self.past_calls.append({"symbol": symbol, "frequency": frequency,
                                    "end_date": end_date,
                                    "lookback_periods": lookback_periods})
            return {"earnings": [{"fiscal_date_ending": "2024-03-31",
                                  "report_date": "2024-04-25"}]}

    p = _SplitDates()
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    got = DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    assert got == date(2024, 4, 25), (
        f"resolved to {got}; 2024-04-25 is the ANNOUNCEMENT, 2024-03-31 is the fiscal "
        f"period end (25 days earlier)"
    )


def test_an_unparseable_date_is_skipped_not_read_as_today(monkeypatch):
    """A garbled date must contribute NOTHING. Degrading it to "today" would read as 0 days
    to earnings — the most permissive possible answer, firing every ``days_to_earnings <= N``
    gate on sight."""
    class _Garbled(_FakeFundamentals):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                              format_type="markdown"):
            self.past_calls.append({"symbol": symbol, "frequency": frequency,
                                    "end_date": end_date,
                                    "lookback_periods": lookback_periods})
            return {"earnings": [{"report_date": "not-a-date",
                                  "fiscal_date_ending": "not-a-date"},
                                 {"report_date": "2024-04-25",
                                  "fiscal_date_ending": "2024-04-25"}]}

    p = _Garbled()
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    cond = _cond(_BacktestAccount(), "<=", 5.0)
    assert cond.evaluate() is False, "a garbled date was read as 'today' (0 days away)"
    assert cond.calculated_value == 41   # the one real row, 2024-04-25


def test_only_garbled_dates_is_unevaluable_not_a_number(monkeypatch):
    """With NOTHING parseable there is no measurement. Any number here — 0, the wall clock's
    distance, the bar — is invented."""
    class _AllGarbled(_FakeFundamentals):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                              format_type="markdown"):
            return {"earnings": [{"report_date": "2024/04/25", "fiscal_date_ending": ""},
                                 {"report_date": None, "fiscal_date_ending": "N/A"}]}

        def get_earnings_estimates(self, symbol, frequency, as_of_date, lookback_periods=4,
                                   format_type="markdown"):
            return {"estimates": [{"fiscal_date_ending": "garbage"}]}

    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: _AllGarbled(), raising=False)
    cond = _cond(_BacktestAccount(), "<=", 5.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


def test_a_row_carrying_only_a_fiscal_period_end_is_still_usable(monkeypatch):
    """``report_date`` is preferred but not required — a provider that only supplies the
    period end must still yield a date rather than being dropped."""
    class _NoReportDate(_FakeFundamentals):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                              format_type="markdown"):
            return {"earnings": [{"fiscal_date_ending": "2024-04-25"}]}

    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: _NoReportDate(), raising=False)
    got = DaysToEarningsCondition(_BacktestAccount(), "MSFT", _rec(), ">=", 0.0) \
        ._next_earnings_date("MSFT", SIM_BAR)
    assert got == NEXT_QUARTERLY


def test_a_malformed_non_dict_row_does_not_lose_the_good_rows(monkeypatch):
    """One junk row in the payload must not take the whole lookup down with it (an
    AttributeError here would be swallowed by evaluate() and read as "no earnings")."""
    class _JunkRow(_FakeFundamentals):
        def get_past_earnings(self, symbol, frequency, end_date, lookback_periods=8,
                              format_type="markdown"):
            return {"earnings": ["not-a-row", None,
                                 {"report_date": "2024-04-25",
                                  "fiscal_date_ending": "2024-04-25"}]}

    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: _JunkRow(), raising=False)
    cond = _cond(_BacktestAccount(), "<=", 45.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 41


def test_no_upcoming_earnings_anywhere_is_unevaluable(monkeypatch):
    p = _FakeFundamentals(reports=["2023-10-24"], estimates=["2023-06-30"])
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    cond = _cond(_BacktestAccount(), "<=", 7.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None
    assert cond.get_actual_value_display() is None
