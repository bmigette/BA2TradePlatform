"""``tools/import_deploy_payload.py`` and the CLOCK an entry weekday was scored on.

A backtest entry weekday means different things per clock:

* INTRADAY (5-minute, every stored classic row): decision at 09:30 of that weekday, live fires at
  09:30 of the same weekday -> copied unchanged.
* DAILY (``1d``): the bar stamped D decides on D's close and fills at D+1's open, which is a live
  09:30 run on the NEXT trading day -> backtest Monday = live Tuesday, Friday = Monday. The tool
  used to copy the gene unchanged (one session early, with data one session older).

A daily-clock schedule is now REFUSED unless ``--shift-daily-clock-weekdays`` is passed, and an
unknown clock is refused outright. Both refusals happen before anything is written.
"""
from __future__ import annotations

import json
import sys

import pytest

from tests.test_import_deploy_payload_rm_toggle_policy import tool  # noqa: F401  (fixture)

WEEK = {"monday": False, "tuesday": False, "wednesday": False, "thursday": False,
        "friday": False, "saturday": False, "sunday": False}


def _days(*on):
    return {**WEEK, **{d: True for d in on}}


def _on(days):
    return sorted(d for d, v in days.items() if v)


@pytest.mark.parametrize("interval", ["5min", "5m", "1h", "15m"])
def test_intraday_clock_copies_the_weekdays_unchanged(tool, interval):
    out, note = tool.live_entry_days(_days("monday", "thursday"), interval)
    assert _on(out) == ["monday", "thursday"]
    assert "unchanged" in note


def test_daily_clock_is_refused_without_the_flag(tool):
    with pytest.raises(ValueError, match="DAILY clock"):
        tool.live_entry_days(_days("monday"), "1d")


def test_daily_clock_shift_moves_every_day_to_the_next_trading_weekday(tool):
    out, _ = tool.live_entry_days(_days("monday", "wednesday", "friday"), "1d", shift_daily_clock=True)
    assert _on(out) == ["monday", "thursday", "tuesday"]      # Mon->Tue, Wed->Thu, Fri->Mon
    out, _ = tool.live_entry_days(_days("thursday"), "1d", shift_daily_clock=True)
    assert _on(out) == ["friday"]


def test_daily_clock_weekend_gene_is_refused_rather_than_shifted(tool):
    with pytest.raises(ValueError, match="saturday"):
        tool.live_entry_days(_days("monday", "saturday"), "1d", shift_daily_clock=True)


@pytest.mark.parametrize("interval", [None, "", "1wk", "1mo", "weird"])
def test_unknown_clock_is_refused_even_with_the_flag(tool, interval):
    with pytest.raises(ValueError):
        tool.live_entry_days(_days("monday"), interval, shift_daily_clock=True)


def _payload(tmp_path, interval):
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertInstance

    inst_id = add_instance(ExpertInstance(
        account_id=1, expert="FMPRating", alias="before", enabled=True, virtual_equity_pct=10.0))
    entry = {
        "backtest_id": 1, "target_instance_id": inst_id, "account_id": 1,
        "virtual_equity_pct": 10.0, "expert_name": "FMPRating", "label": "daily-row",
        "ruleset": {"entry_rules": [], "exit_rules": []},
        "settings": {"settings": {"expert_params": {"use_atr_stop": False,
                                                    "regime_overlay_enabled": False}},
                     "universe": None,
                     "execution": {"run_schedule_override": {"days": _days("monday"),
                                                             "times": ["09:30"]}},
                     "execution_interval": interval},
    }
    path = str(tmp_path / "payload.json")
    json.dump([entry], open(path, "w"), default=str)
    return path


def test_main_refuses_a_daily_clock_schedule_before_writing_anything(tool, capsys, monkeypatch, tmp_path):
    path = _payload(tmp_path, "1d")
    monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])
    assert tool.main() == 1
    out = capsys.readouterr().out
    assert "FATAL: daily-row:" in out and "--shift-daily-clock-weekdays" in out


def test_main_refuses_a_payload_with_no_interval(tool, capsys, monkeypatch, tmp_path):
    path = _payload(tmp_path, None)
    monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])
    assert tool.main() == 1
    assert "no execution_interval" in capsys.readouterr().out
