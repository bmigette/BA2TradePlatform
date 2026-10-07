"""The decision-time gene (``schedule:time``): validation, reconstruction from a stored genome."""
from __future__ import annotations

import pytest

from ba2_common.core.schedule_genes import (
    SCHEDULE_TIME_GENE, retime_schedules, schedule_override_from_genes,
    schedule_time_from_genes, validate_decision_times)

OWNER_LIST = ["09:35", "09:40", "09:45", "10:00", "15:30"]


def test_the_owners_list_is_valid_and_comes_back_sorted():
    assert validate_decision_times(OWNER_LIST, "5min") == OWNER_LIST
    assert validate_decision_times(list(reversed(OWNER_LIST)), "5min") == OWNER_LIST


@pytest.mark.parametrize("extra", ["12:00", "15:50"])
def test_the_value_list_is_a_parameter_not_five_morning_times(extra):
    out = validate_decision_times([*OWNER_LIST, extra], "5min")
    assert extra in out and len(out) == 6 and out == sorted(out)


@pytest.mark.parametrize("bad, why", [
    (["09:35", "09:37"], "grid"),          # off the 5-minute grid
    (["09:35", "9:40"], "HH:MM"),          # not zero padded
    (["09:35", "25:00"], "clock"),
    (["09:35", "09:25"], "grid"),          # before the open
    (["09:35", "16:00"], "grid"),          # after the last bar
    (["09:35", "09:30"], "first bar"),     # the decision price would be the prior session's
    (["09:35", "15:55"], "last bar"),      # would fill at the NEXT session's open
    (["09:35", "09:35"], "duplicate"),
    (["09:35"], "at least"),               # one value is not a gene
    ([], "at least"),
    ("09:35,09:40", "list"),
])
def test_a_bad_list_is_refused_loudly(bad, why):
    with pytest.raises(ValueError, match=why):
        validate_decision_times(bad, "5min")


def test_a_daily_clock_has_no_decision_time():
    with pytest.raises(ValueError, match="intraday"):
        validate_decision_times(OWNER_LIST, "1d")


def test_the_grid_follows_the_execution_interval():
    assert validate_decision_times(["09:45", "10:15"], "15min") == ["09:45", "10:15"]
    with pytest.raises(ValueError, match="grid"):
        validate_decision_times(["09:40", "10:15"], "15min")


def test_a_chosen_time_wins_over_the_run_level_time_for_the_stored_genome():
    sp = {"schedule:monday": 1, "schedule:tuesday": 0, SCHEDULE_TIME_GENE: "15:30"}
    out = schedule_override_from_genes(sp, {"days": {}, "times": ["10:00"]})
    assert out["times"] == ["15:30"] and out["days"]["monday"] is True


def test_the_time_gene_is_not_mistaken_for_a_weekday():
    sp = {"schedule:monday": 1, SCHEDULE_TIME_GENE: "09:40"}
    out = schedule_override_from_genes(sp, None, weekdays_only=True)
    assert set(out["days"]) == {"monday", "tuesday", "wednesday", "thursday", "friday",
                                "saturday", "sunday"}
    assert out["times"] == ["09:40"]


def test_a_genome_without_the_time_gene_reconstructs_exactly_as_before():
    sp = {"schedule:monday": 1}
    assert schedule_override_from_genes(sp, {"times": ["10:00"]})["times"] == ["10:00"]
    assert schedule_override_from_genes(sp, None)["times"] == ["09:30"]      # the legacy time
    assert schedule_time_from_genes(sp) is None


def test_a_time_gene_alone_takes_the_run_days_or_refuses():
    days = {"monday": True, "tuesday": False}
    out = schedule_override_from_genes({SCHEDULE_TIME_GENE: "10:00"}, {"days": days, "times": ["09:30"]})
    assert out == {"days": days, "times": ["10:00"]}
    with pytest.raises(ValueError, match="refusing to guess"):
        schedule_override_from_genes({SCHEDULE_TIME_GENE: "10:00"}, None)


def test_a_corrupt_stored_time_is_refused_not_reinterpreted():
    with pytest.raises(ValueError, match="HH:MM"):
        schedule_time_from_genes({SCHEDULE_TIME_GENE: 3})


def test_retime_moves_both_schedules_and_nothing_else():
    cfg = {"execution_interval": "5min",
           "run_schedule_override": {"days": {"monday": True}, "times": ["09:30"]},
           "manage_schedule_override": {"days": {"monday": True, "tuesday": True}, "times": ["09:30"]},
           "seed": 7}
    out = retime_schedules(cfg, "15:30")
    assert out["run_schedule_override"] == {"days": {"monday": True}, "times": ["15:30"]}
    assert out["manage_schedule_override"]["times"] == ["15:30"]
    assert out["manage_schedule_override"]["days"] == {"monday": True, "tuesday": True}
    assert out["seed"] == 7 and cfg["run_schedule_override"]["times"] == ["09:30"]   # input intact
    with pytest.raises(ValueError):
        retime_schedules({**cfg, "manage_schedule_override": None}, "15:30")
    with pytest.raises(ValueError, match="last bar"):
        retime_schedules(cfg, "15:55")


def test_retime_refuses_the_first_bar_unless_the_measurement_opt_in_is_given():
    from ba2_common.core.schedule_genes import validate_decision_times
    cfg = {"execution_interval": "5min",
           "run_schedule_override": {"days": {"monday": True}, "times": ["10:00"]},
           "manage_schedule_override": {"days": {"monday": True}, "times": ["10:00"]}}
    with pytest.raises(ValueError, match="first bar"):
        retime_schedules(cfg, "09:30")
    out = retime_schedules(cfg, "09:30", allow_first_bar=True)
    assert out["run_schedule_override"]["times"] == ["09:30"]
    assert out["manage_schedule_override"]["times"] == ["09:30"]
    with pytest.raises(ValueError, match="first bar"):       # the GA gene validation still refuses it
        validate_decision_times(["09:30", "10:00"], "5min")
    with pytest.raises(ValueError):                           # the opt-in never makes a daily clock valid
        retime_schedules({**cfg, "execution_interval": "1d"}, "09:30", allow_first_bar=True)
