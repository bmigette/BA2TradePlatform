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


# ---- stored day VALUES: one table -> enabled / disabled / refused, read identically everywhere ------------
# Real booleans are canonical. Tolerated (read by MEANING, never truthiness): the ints 1/0 and the strings
# "true"/"false"/"1"/"0" (case-insensitive, whitespace ignored). Anything else is NOT guessed: refused.
ENABLED, DISABLED, REFUSED = "enabled", "disabled", "refused"
VALUE_TABLE = [
    (True, ENABLED), (False, DISABLED),
    (1, ENABLED), (0, DISABLED),
    ("1", ENABLED), ("0", DISABLED),
    ("true", ENABLED), ("false", DISABLED),
    ("TRUE", ENABLED), ("False", DISABLED), (" true ", ENABLED), ("\t0\n", DISABLED),
    (None, REFUSED), ("yes", REFUSED), ("no", REFUSED), ("on", REFUSED), ("", REFUSED), (" ", REFUSED),
    (2, REFUSED), (-1, REFUSED), (1.0, REFUSED), (0.0, REFUSED), (0.5, REFUSED), ([], REFUSED),
    ([True], REFUSED), ({}, REFUSED), ("2", REFUSED), ("truee", REFUSED),
]


@pytest.mark.parametrize("value, outcome", VALUE_TABLE, ids=lambda x: repr(x))
def test_the_shared_function_reads_every_stored_value_by_meaning(value, outcome):
    days = {"monday": value}
    refusal = sg.schedule_refusal_message({"days": days, "times": ["09:30"]})
    if outcome == REFUSED:
        assert refusal and "monday" in refusal and "invalid" in refusal
        with pytest.raises(ValueError):
            sg.schedule_weekday_enabled(days, "monday")      # never a silent guess, even if asked directly
    else:
        assert refusal is None
        assert sg.schedule_weekday_enabled(days, "monday") is (outcome == ENABLED)


@pytest.mark.parametrize("value, outcome", [r for r in VALUE_TABLE if r[1] != REFUSED], ids=lambda x: repr(x))
def test_live_and_backtest_agree_on_every_tolerated_monday_value(value, outcome):
    from ba2_trade_platform.core.JobManager import JobManager

    days = {"monday": value}
    backtest = _schedule_allows_entry(datetime(2026, 10, 5), {"days": days}, False)      # a Monday
    trigger = JobManager.__new__(JobManager)._parse_schedule({"days": days, "times": ["09:30"]})
    live, prev, now = False, None, datetime(2026, 10, 5, 0, 0)
    for _ in range(8):
        nxt = trigger.get_next_fire_time(prev, now)
        live = live or nxt.weekday() == 0
        prev, now = nxt, nxt + timedelta(seconds=1)
    assert backtest == live == (outcome == ENABLED)


@pytest.mark.parametrize("value", [v for v, o in VALUE_TABLE if o == REFUSED], ids=lambda x: repr(x))
def test_live_refuses_every_invalid_value_once_with_one_error(value, monkeypatch):
    import ba2_trade_platform.core.JobManager as module
    from ba2_trade_platform.core.JobManager import JobManager
    errors = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    schedule = {"days": {"monday": value}, "times": ["09:30"]}
    assert JobManager._schedule_is_runnable(_Expert(4), "execution_schedule_enter_market", schedule) is False
    assert len(errors) == 1 and "expert instance 4" in errors[0] and "invalid" in errors[0]
    assert JobManager.__new__(JobManager)._parse_schedule(schedule, context="x") is None


class _Expert:
    def __init__(self, id_):
        self.id = id_


@pytest.mark.parametrize("value", [v for v, o in VALUE_TABLE if o == REFUSED], ids=lambda x: repr(x))
def test_the_backtest_refuses_an_invalid_value_at_setup_and_never_per_bar(value):
    import inspect
    bad = {"days": {**{d: True for d in WEEKDAYS}, "monday": value}, "times": ["09:30"]}
    eng = _engine({"run_schedule_override": bad}, [(_StubExpert(), 7, {}, None)])
    with pytest.raises(ValueError) as e:
        eng._validate_schedules_once()
    assert "invalid" in str(e.value) and "monday" in str(e.value)
    source = inspect.getsource(_schedule_allows_entry)
    assert "schedule_refusal_message" not in source and "invalid_schedule_day_values" not in source


def test_the_ui_loader_does_not_raise_on_an_invalid_value_and_shows_defaults():
    """The editor loads a refused schedule at its defaults (and shows the refusal banner): the display
    helper never raises."""
    days = {"monday": None, "tuesday": "false", "saturday": "yes"}
    assert sg.schedule_weekday_for_display(days, "monday") is True          # invalid -> the default (Mon on)
    assert sg.schedule_weekday_for_display(days, "tuesday") is False        # tolerated -> read by meaning
    assert sg.schedule_weekday_for_display(days, "saturday") is False       # invalid -> the default (Sat off)
    assert sg.schedule_weekday_for_display({}, "sunday") is False


def test_the_ui_saves_real_booleans():
    """``_get_schedule_config`` / the enter-market and open-positions collectors write ``checkbox.value``
    (a bool) for every day: pin the construction so a future change cannot save strings."""
    import inspect
    from ba2_trade_platform.ui.pages.settings import ExpertSettingsTab
    for name in ("_get_schedule_config", "_get_enter_market_schedule_config", "_get_open_positions_schedule_config"):
        fn = getattr(ExpertSettingsTab, name, None)
        if fn is None:
            continue
        source = inspect.getsource(fn)
        assert "= checkbox.value" in source and "str(checkbox" not in source


def test_the_old_coercion_pin_is_gone():
    """"false" and "0" used to read as ENABLED (``bool(v)``). They now mean DISABLED."""
    assert sg.schedule_weekday_enabled({"monday": "false"}, "monday") is False
    assert sg.schedule_weekday_enabled({"monday": "0"}, "monday") is False


def test_the_tolerated_spellings_agree_with_the_repo_settings_bool_parser():
    """The strict schedule parser is a subset of ``coerce_bool`` (the settings layer's reader), so a
    tolerated schedule spelling can never read differently from the same spelling elsewhere."""
    from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
    for value, outcome in VALUE_TABLE:
        if outcome != REFUSED:
            assert coerce_bool(value) is (outcome == ENABLED)


def test_every_testplatform_builder_still_writes_real_booleans():
    import ba2test_launcher as launcher
    from app.api.backtests import _run_schedule_override
    from app.services import robustness_handler as rh
    built = [launcher._daily_manage_schedule()] + [_run_schedule_override("weekly", d) for d in WEEKDAYS]
    built += [rh._day_override(d) for d in WEEKDAYS] + [rh._time_override("10:00")]
    built += [sg.schedule_override_from_genes({f"schedule:{d}": 1}, None, weekdays_only=w, option_run=o)
              for d in WEEKDAYS for w in (False, True) for o in (False, True)]
    for schedule in built:
        assert all(type(v) is bool for v in schedule["days"].values()), schedule
