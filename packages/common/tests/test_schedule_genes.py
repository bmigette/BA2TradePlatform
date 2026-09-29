from ba2_common.core.schedule_genes import (
    SCHEDULE_DAYS, WEEKDAYS, repair_no_weekday, schedule_override_from_genes,
)


def test_constants():
    assert SCHEDULE_DAYS == ("monday", "tuesday", "wednesday", "thursday", "friday",
                             "saturday", "sunday")
    assert WEEKDAYS == SCHEDULE_DAYS[:5]


def test_no_schedule_genes_returns_none():
    assert schedule_override_from_genes({"model:x": 1}) is None
    assert schedule_override_from_genes(None) is None


def test_genes_replace_days_and_keep_base_times():
    sp = {"schedule:thursday": 1, "schedule:monday": 0}
    out = schedule_override_from_genes(sp, {"times": ["10:00"]})
    assert out["times"] == ["10:00"]
    assert out["days"]["thursday"] is True and out["days"]["monday"] is False


def test_default_times_when_base_has_none():
    assert schedule_override_from_genes({"schedule:friday": 1})["times"] == ["09:30"]


def test_weekend_only_genome_deploys_as_monday_under_weekdays_only():
    out = schedule_override_from_genes({"schedule:saturday": 1}, weekdays_only=True)
    assert out["days"]["monday"] is True
    assert out["days"]["saturday"] is False


def test_equity_repair_only_when_all_seven_off():
    days = {d: False for d in SCHEDULE_DAYS}
    days["sunday"] = True
    assert repair_no_weekday(dict(days), option_run=False)["monday"] is False
    assert repair_no_weekday(dict(days), option_run=True)["monday"] is True
