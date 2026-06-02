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
