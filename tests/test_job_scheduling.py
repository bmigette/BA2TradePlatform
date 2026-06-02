from apscheduler.triggers.cron import CronTrigger
from ba2_trade_platform.core.JobManager import build_monthly_cron


def test_build_monthly_cron_first_monday():
    trig = build_monthly_cron(ordinal=1, weekday="monday", hour=9, minute=30)
    assert isinstance(trig, CronTrigger)
    # str(CronTrigger) exposes the fields; day must be the "1st mon" expression
    s = str(trig)
    assert "day='1st mon'" in s
    assert "hour='9'" in s and "minute='30'" in s


def test_build_monthly_cron_third_tuesday():
    trig = build_monthly_cron(ordinal=3, weekday="tuesday", hour=16, minute=0)
    assert "day='3rd tue'" in str(trig)


def test_build_monthly_cron_rejects_bad_ordinal():
    import pytest
    with pytest.raises(ValueError):
        build_monthly_cron(ordinal=5, weekday="monday", hour=9, minute=30)


def test_parse_schedule_monthly():
    from ba2_trade_platform.core.JobManager import JobManager
    jm = JobManager.__new__(JobManager)
    trig = jm._parse_schedule({"frequency": "monthly", "ordinal": 2, "weekday": "friday", "times": ["10:00"]})
    assert "day='2nd fri'" in str(trig)


def test_parse_schedule_weekly_still_works():
    from ba2_trade_platform.core.JobManager import JobManager
    jm = JobManager.__new__(JobManager)
    trig = jm._parse_schedule({"days": {"monday": True, "wednesday": True}, "times": ["09:30"]})
    assert trig is not None  # weekly path unchanged


def test_open_positions_suppressed_when_flag_false():
    from ba2_trade_platform.core.JobManager import should_schedule_open_positions
    assert should_schedule_open_positions({"schedules_open_positions": False}) is False
    assert should_schedule_open_positions({}) is True  # default on
    assert should_schedule_open_positions({"schedules_open_positions": True}) is True


def test_assemble_monthly_schedule_round_trips_through_parse():
    # The UI builds its monthly config via assemble_monthly_schedule; that exact
    # shape must parse back into the matching CronTrigger.
    from ba2_trade_platform.core.JobManager import assemble_monthly_schedule, JobManager
    cfg = assemble_monthly_schedule(ordinal=1, weekday="monday", times=["09:30"])
    assert cfg == {"frequency": "monthly", "ordinal": 1, "weekday": "monday", "times": ["09:30"]}
    jm = JobManager.__new__(JobManager)
    trig = jm._parse_schedule(cfg)
    assert "day='1st mon'" in str(trig)
