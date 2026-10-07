"""Live and backtest read "is weekday X enabled in this schedule dict" through ONE function
(``ba2_common.core.schedule_genes.schedule_weekday_enabled``).

Review of 6c8cd788: live read an absent weekday key as enabled for all seven days, the settings UI
and the declared default say Monday-Friday on / Saturday-Sunday off. The change is safe for
backtests because a backtest has NO weekend bars (``trading_days`` is a market-day list), so the
only difference between the old backtest rule (``days.get(wd, True)``) and the shared one is on
Saturday/Sunday, which no bar ever has. These tests pin that.
"""
from datetime import datetime, timedelta

import pytest

from ba2_common.core import schedule_genes as sg
from app.services.backtest.daily_engine import _schedule_allows_entry

WEEKDAYS = sg.SCHEDULE_DAYS


def _old_rule(days, weekday):
    return bool(days.get(weekday, True))        # the backtest's former inline rule


def test_the_shared_default_is_weekdays_on_weekend_off():
    assert [sg.schedule_weekday_enabled({}, d) for d in WEEKDAYS] == [True] * 5 + [False] * 2


def test_an_explicit_value_always_wins():
    assert sg.schedule_weekday_enabled({"saturday": True, "monday": False}, "saturday") is True
    assert sg.schedule_weekday_enabled({"saturday": True, "monday": False}, "monday") is False


def test_unknown_keys_are_reported():
    assert sg.unknown_schedule_day_keys({"monday": True, "wensday": False, "Friday": True}) == ["wensday", "Friday"]
    assert sg.unknown_schedule_day_keys({d: True for d in WEEKDAYS}) == []


def test_the_engine_gate_agrees_with_the_old_rule_on_every_weekday_for_any_partial_dict():
    """Backtest results cannot change: on Monday-Friday the shared rule equals the old inline one
    for every subset of present keys."""
    import itertools
    for present in itertools.product((None, True, False), repeat=5):
        days = {d: v for d, v in zip(WEEKDAYS[:5], present) if v is not None}
        for offset in range(5):                                  # 2026-10-05 is a Monday
            when = datetime(2026, 10, 5) + timedelta(days=offset)
            assert _schedule_allows_entry(when, {"days": days}, False) == _old_rule(days, WEEKDAYS[offset])
            assert sg.schedule_weekday_enabled(days, WEEKDAYS[offset]) == _old_rule(days, WEEKDAYS[offset])


def test_the_only_difference_from_the_old_rule_is_an_absent_weekend_key():
    days = {"monday": True}
    for name in WEEKDAYS:
        differs = sg.schedule_weekday_enabled(days, name) != _old_rule(days, name)
        assert differs == (name in ("saturday", "sunday"))
