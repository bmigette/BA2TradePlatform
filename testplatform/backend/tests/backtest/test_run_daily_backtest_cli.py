"""CLI: --run-schedule maps to the engine's run_schedule_override (weekly entry cadence).

Weekly cadence is the cheap ~5x perf win: the engine already SKIPS analyze_as_of() on
off-cadence bars (daily_engine._schedule_allows_entry); this just exposes it on the CLI.
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_daily_backtest.py"
_OTHER_DAYS = ("tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _load():
    spec = importlib.util.spec_from_file_location("run_daily_backtest_cli", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rdb = _load()


def _args(*extra):
    return rdb._parse_args(
        ["--expert", "FMPEarningsDrift", "--start", "2024-01-02", "--end", "2024-03-01", *extra]
    )


def test_default_is_daily_no_override():
    cfg = rdb._build_real_config(_args())
    assert "run_schedule_override" not in cfg  # daily = analyse every bar (legacy)


def test_weekly_sets_monday_only_override():
    cfg = rdb._build_real_config(_args("--run-schedule", "weekly"))
    days = cfg["run_schedule_override"]["days"]
    assert days["monday"] is True
    assert all(days[d] is False for d in _OTHER_DAYS)


def test_weekly_custom_day():
    cfg = rdb._build_real_config(_args("--run-schedule", "weekly", "--run-schedule-day", "friday"))
    days = cfg["run_schedule_override"]["days"]
    assert days["friday"] is True
    assert days["monday"] is False


def test_helper_daily_and_none_are_no_override():
    assert rdb._run_schedule_override("daily") is None
    assert rdb._run_schedule_override(None) is None


def test_invalid_schedule_rejected():
    with pytest.raises(SystemExit):
        rdb._parse_args(["--run-schedule", "fortnightly"])  # argparse choices reject it
