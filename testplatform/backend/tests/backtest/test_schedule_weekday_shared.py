"""Live and backtest read "is weekday X enabled in this schedule dict" through ONE function
(``ba2_common.core.schedule_genes.schedule_weekday_enabled``).

Review of 6c8cd788: live read an absent weekday key as enabled for all seven days, the settings UI
and the declared default say Monday-Friday on / Saturday-Sunday off. The change is safe for
backtests because a backtest has NO weekend bars (``trading_days`` is a market-day list), so the
only difference between the old backtest rule (``days.get(wd, True)``) and the shared one is on
Saturday/Sunday, which no bar ever has. These tests pin that.

Re-review: an unknown day key is refused by the live scheduler, so the backtest refuses it too, ONCE at
run setup (never per bar), with the same sentence.
"""
import itertools
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


def test_the_refusal_sentence_names_every_unknown_key():
    msg = sg.schedule_refusal_message({"days": {"monday": True, "wensday": False}, "times": ["09:30"]})
    assert msg.startswith("schedule refused: unknown day key 'wensday'")
    assert sg.schedule_refusal_message({"days": {d: True for d in WEEKDAYS}}) is None
    assert sg.schedule_refusal_message({"frequency": "monthly", "weekday": "monday"}) is None
    assert sg.schedule_refusal_message(None) is None


def test_the_engine_gate_agrees_with_the_old_rule_on_every_weekday_for_any_partial_dict():
    """Backtest results cannot change: on Monday-Friday the shared rule equals the old inline one
    for every subset of present keys."""
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


# ---- the backtest refuses an unknown day key ONCE, at setup -----------------------------------------
class _StubExpert:
    def __init__(self, schedule=None):
        self._schedule = schedule

    def get_setting_with_interface_default(self, key):
        return self._schedule


def _engine(config, experts):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.config = config
    eng.experts = experts
    return eng


def test_an_unknown_day_key_fails_the_run_up_front_with_the_shared_message():
    typo = {"days": {**{d: True for d in WEEKDAYS}, "wensday": False}, "times": ["09:30"]}
    eng = _engine({"run_schedule_override": typo}, [(_StubExpert(), 7, {}, None)])
    with pytest.raises(ValueError) as e:
        eng._validate_schedules_once()
    assert "wensday" in str(e.value) and "expert 7" in str(e.value) and "entry" in str(e.value)
    assert sg.schedule_refusal_message(typo) in str(e.value)


def test_a_manage_schedule_typo_is_refused_too():
    typo = {"days": {"mondey": True}, "times": ["09:30"]}
    eng = _engine({"manage_schedule_override": typo}, [(_StubExpert(), 3, {}, None)])
    with pytest.raises(ValueError, match="manage"):
        eng._validate_schedules_once()


def test_a_valid_run_passes_the_setup_check():
    ok = {"days": {d: True for d in WEEKDAYS}, "times": ["09:30"]}
    _engine({"run_schedule_override": ok}, [(_StubExpert(), 1, {}, None)])._validate_schedules_once()
    _engine({}, [(_StubExpert(None), 1, {}, None)])._validate_schedules_once()


def test_the_bar_gate_is_not_the_validator():
    """Never per bar: ``_schedule_allows_entry`` itself does not validate (it must stay cheap)."""
    import inspect
    source = inspect.getsource(_schedule_allows_entry)
    assert "unknown_schedule_day_keys" not in source and "schedule_refusal_message" not in source


def test_every_testplatform_schedule_builder_passes_the_validation():
    """All known writers emit exactly the seven lower-case keys, so no stored run can trip the setup check.
    (The launcher's inline weekly dict in ``_cmd_optimize`` has the same shape as the API's
    ``_run_schedule_override``, which mirrors it.)"""
    import ba2test_launcher as launcher
    from app.api.backtests import _run_schedule_override
    from app.services import robustness_handler as rh

    built = [launcher._daily_manage_schedule()]
    built += [_run_schedule_override("weekly", d) for d in WEEKDAYS]
    built += [rh._day_override(d) for d in WEEKDAYS] + [rh._time_override("10:00")]
    built += [sg.schedule_override_from_genes({f"schedule:{d}": 1}, {"times": ["09:30"]}, weekdays_only=w, option_run=o)
              for d in WEEKDAYS for w in (False, True) for o in (False, True)]
    assert built
    for schedule in built:
        assert sg.schedule_refusal_message(schedule) is None, schedule
        assert set(schedule["days"]) == set(sg.SCHEDULE_DAYS)


def test_the_optimizer_decode_builds_only_valid_day_keys():
    from app.services.strategy_param_space import decode_params
    import inspect
    # the decoded schedule is {day: bool} over SCHEDULE_DAYS (see decode_params); pin the construction
    source = inspect.getsource(decode_params)
    assert "for day in SCHEDULE_DAYS" in source


# ---- non-boolean stored values: live and backtest read them through the SAME coercion ----------------
NON_BOOLEAN = ["1", 1, "true", "false", "0", 0, None]
EXPECTED_ENABLED = {"1": True, 1: True, "true": True, "false": True, "0": True, 0: False, None: False}


@pytest.mark.parametrize("value", NON_BOOLEAN, ids=repr)
def test_live_and_backtest_agree_on_a_non_boolean_monday_value(value):
    """PINNED, not endorsed: the coercion is ``bool(value)``. So the STRINGS "false" and "0" count as
    ENABLED (any non-empty string is truthy) while 0 and None disable the day. Changing that would change
    behaviour; the owner decides separately."""
    from ba2_trade_platform.core.JobManager import JobManager

    days = {"monday": value}
    backtest = _schedule_allows_entry(datetime(2026, 10, 5), {"days": days}, False)      # a Monday
    trigger = JobManager.__new__(JobManager)._parse_schedule({"days": days, "times": ["09:30"]})
    live, prev, now = False, None, datetime(2026, 10, 5, 0, 0)
    for _ in range(8):
        nxt = trigger.get_next_fire_time(prev, now)
        live = live or nxt.weekday() == 0
        prev, now = nxt, nxt + timedelta(seconds=1)
    assert backtest == live == EXPECTED_ENABLED[value]
