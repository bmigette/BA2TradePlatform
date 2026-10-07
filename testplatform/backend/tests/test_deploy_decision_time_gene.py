"""A gene-chosen decision time ends up as the LIVE schedule time of BOTH schedules.

Export side: ``schedule_override_from_genes`` (the one reconstruction ``backtest_export`` calls for
every payload) turns the stored ``schedule:time`` gene into ``execution.run_schedule_override``.
Deploy side: ``tools/import_deploy_payload.py`` copies that time into
``execution_schedule_enter_market`` AND ``execution_schedule_open_positions``.
"""
from __future__ import annotations

import json
import sys

from ba2_common.core.schedule_genes import SCHEDULE_TIME_GENE, schedule_override_from_genes

from tests.test_import_deploy_payload_rm_toggle_policy import tool  # noqa: F401  (fixture)

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _genome(time):
    sp = {f"schedule:{d}": int(d in ("tuesday", "thursday")) for d in DAYS}
    if time:
        sp[SCHEDULE_TIME_GENE] = time
    return sp


def test_the_export_reconstruction_carries_the_chosen_time():
    base = {"days": {d: d == "monday" for d in DAYS}, "times": ["10:00"]}
    out = schedule_override_from_genes(_genome("15:30"), base, weekdays_only=True)
    assert out["times"] == ["15:30"]
    assert [d for d, v in out["days"].items() if v] == ["tuesday", "thursday"]
    # a genome from a run without the gene keeps the run-level time exactly as before
    assert schedule_override_from_genes(_genome(None), base, weekdays_only=True)["times"] == ["10:00"]


def test_the_deploy_writes_the_chosen_time_into_both_live_schedules(tool, tmp_path, monkeypatch, capsys):
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import ExpertInstance

    inst_id = add_instance(ExpertInstance(
        account_id=1, expert="FMPRating", alias="before", enabled=True, virtual_equity_pct=10.0))
    run_sched = schedule_override_from_genes(
        _genome("15:30"), {"days": {d: d == "monday" for d in DAYS}, "times": ["10:00"]},
        weekdays_only=True)
    entry = {
        "backtest_id": 1, "target_instance_id": inst_id, "account_id": 1,
        "virtual_equity_pct": 10.0, "expert_name": "FMPRating", "label": "timegene-row",
        "ruleset": {"entry_rules": [], "exit_rules": []},
        "settings": {"settings": {"expert_params": {"use_atr_stop": False,
                                                    "regime_overlay_enabled": False}},
                     "universe": None,
                     "execution": {"run_schedule_override": run_sched},
                     "execution_interval": "5min"},
    }
    path = str(tmp_path / "payload.json")
    json.dump([entry], open(path, "w"), default=str)
    monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])
    rc = tool.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    inst = get_instance(ExpertInstance, inst_id)
    expert = tool._expert_class("FMPRating")(inst.id)
    enter = expert.get_setting_with_interface_default("execution_schedule_enter_market")
    manage = expert.get_setting_with_interface_default("execution_schedule_open_positions")
    assert enter["times"] == ["15:30"] and manage["times"] == ["15:30"]
    assert enter["time_basis"] == "market" and manage["time_basis"] == "market"
    assert [d for d, v in enter["days"].items() if v] == ["tuesday", "thursday"]
    assert all(manage["days"][d] for d in DAYS[:5]) and not manage["days"]["saturday"]


def test_the_deploy_refuses_a_time_whose_pass_cannot_finish_before_the_close(
        tool, tmp_path, monkeypatch, capsys):
    """15:50 + the measured p95 pass duration + margin falls after 16:00: refused before any write."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertInstance

    inst_id = add_instance(ExpertInstance(
        account_id=1, expert="FMPRating", alias="before", enabled=True, virtual_equity_pct=10.0))
    run_sched = schedule_override_from_genes(
        _genome("15:50"), {"days": {d: d == "monday" for d in DAYS}, "times": ["10:00"]},
        weekdays_only=True)
    entry = {
        "backtest_id": 1, "target_instance_id": inst_id, "account_id": 1,
        "virtual_equity_pct": 10.0, "expert_name": "FMPRating", "label": "late-row",
        "ruleset": {"entry_rules": [], "exit_rules": []},
        "settings": {"settings": {"expert_params": {"use_atr_stop": False,
                                                    "regime_overlay_enabled": False}},
                     "universe": None, "execution": {"run_schedule_override": run_sched},
                     "execution_interval": "5min"},
    }
    path = str(tmp_path / "late.json")
    json.dump([entry], open(path, "w"), default=str)
    monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])
    assert tool.main() == 1
    out = capsys.readouterr().out
    assert "FATAL: late-row" in out and "after the 16:00 close" in out
