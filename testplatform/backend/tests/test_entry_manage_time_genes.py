"""goal2027 item 2.4: separate ENTRY and MANAGE decision-time genes (``--decision-times split``).

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.4; plan:
docs/plans/2026-10-25-goal2027-entry-manage-times-plan.md.

Pins: the two opt-in genes (``schedule:entry_time`` / ``schedule:manage_time``) and their mutual exclusion with
``schedule:time``; decode -> trial config (entry time on the entry schedule, manage time on the manage schedule);
the launcher flag, the ``timesplit`` name token and the driver passthrough; export refusal of a split row missing a
gene; and the deploy writing BOTH times to the live schedules, with the close-time refusal applying to both.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

import pytest

from ba2_common.core.knowability import DEFAULT_DECISION_TIME_CHOICES
from ba2_common.core.schedule_genes import (
    SCHEDULE_ENTRY_TIME_GENE, SCHEDULE_MANAGE_TIME_GENE, SCHEDULE_TIME_GENE,
    entry_time_from_genes, manage_time_from_genes, schedule_override_from_genes, split_times_from_genes)
from app.services.strategy_optimization_handler import _build_daily_trial_config
from app.services.strategy_param_space import collect_param_space, decode_params

from tests.test_import_deploy_payload_rm_toggle_policy import tool  # noqa: F401  (fixture)

TIMES = list(DEFAULT_DECISION_TIME_CHOICES)
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAYS_ON = {d: d in DAYS[:5] for d in DAYS}
_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")
_TOOLS = os.path.join(os.path.dirname(_TESTPLATFORM), "tools")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_entry_manage", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_entry_manage"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def _strategy():
    return types.SimpleNamespace(entry_rules=None, exit_rules=None)


def _split_cfg(times=TIMES, day_genes=True):
    cfg = {d: {"optimize": True} for d in DAYS} if day_genes else {}
    cfg["entry_time"] = {"optimize": True, "choices": list(times)}
    cfg["manage_time"] = {"optimize": True, "choices": list(times)}
    return cfg


def _flat(entry, manage):
    flat = {f"schedule:{d}": int(d in ("monday", "tuesday", "wednesday", "thursday", "friday")) for d in DAYS}
    flat[SCHEDULE_ENTRY_TIME_GENE] = entry
    flat[SCHEDULE_MANAGE_TIME_GENE] = manage
    return flat


def _bt_cfg():
    return {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0, "seed": 42,
        "account_settings": {}, "execution_interval": "5min",
        "experts": [{"class": "FMPRating", "settings": {}}],
        "run_schedule_override": {"days": dict(WEEKDAYS_ON), "times": ["10:00"]},
        "manage_schedule_override": {"days": dict(WEEKDAYS_ON), "times": ["10:00"]},
    }


# ----------------------------------------------------------------------------- gene space
class TestGeneSpace:
    def test_two_choice_genes_after_the_days_entry_then_manage(self):
        keys = list(collect_param_space(_strategy(), schedule_cfg=_split_cfg()))
        assert keys[-2:] == [SCHEDULE_ENTRY_TIME_GENE, SCHEDULE_MANAGE_TIME_GENE]
        assert keys.index("schedule:sunday") < keys.index(SCHEDULE_ENTRY_TIME_GENE)
        assert SCHEDULE_TIME_GENE not in keys

    def test_each_gene_is_the_seven_value_stratified_choice(self):
        space = collect_param_space(_strategy(), schedule_cfg=_split_cfg())
        for gene in (SCHEDULE_ENTRY_TIME_GENE, SCHEDULE_MANAGE_TIME_GENE):
            spec = space[gene]
            assert spec["type"] == "choice" and spec["choices"] == TIMES and spec["stratify"] is True

    def test_the_single_gene_and_the_split_genes_are_mutually_exclusive(self):
        cfg = _split_cfg()
        cfg["time"] = {"optimize": True, "choices": list(TIMES)}
        with pytest.raises(ValueError, match="mutually exclusive"):
            collect_param_space(_strategy(), schedule_cfg=cfg)

    def test_one_of_the_pair_alone_is_refused(self):
        cfg = _split_cfg()
        del cfg["manage_time"]
        with pytest.raises(ValueError, match="both"):
            collect_param_space(_strategy(), schedule_cfg=cfg)

    @pytest.mark.parametrize("bad", [["10:00", "09:40"], ["09:40"], ["9:40", "10:00"]])
    def test_a_malformed_list_is_refused(self, bad):
        with pytest.raises(ValueError):
            collect_param_space(_strategy(), schedule_cfg=_split_cfg(bad))

    def test_refused_on_a_bypass_expert(self):
        with pytest.raises(ValueError, match="bypass"):
            collect_param_space(_strategy(), schedule_cfg=_split_cfg(), bypass=True)

    def test_no_split_config_leaves_the_space_untouched(self):
        cfg = {d: {"optimize": True} for d in DAYS}
        keys = list(collect_param_space(_strategy(), schedule_cfg=cfg))
        assert keys == [f"schedule:{d}" for d in DAYS]


# ----------------------------------------------------------------------------- decode -> trial config
class TestDecodeAndTrialConfig:
    def test_decode_returns_both_times_and_no_single_time(self):
        d = decode_params(_strategy(), _flat("09:45", "15:30"))
        assert (d["schedule_entry_time"], d["schedule_manage_time"], d["schedule_time"]) == ("09:45", "15:30", None)
        assert all(d["schedule_days"][x] is False for x in ("saturday", "sunday"))   # times never read as days

    def test_trial_config_puts_each_time_on_its_own_schedule(self):
        trial = _build_daily_trial_config(_bt_cfg(), decode_params(_strategy(), _flat("09:45", "15:30")),
                                          option_trade_records=False)
        assert trial["run_schedule_override"]["times"] == ["09:45"]
        assert trial["manage_schedule_override"]["times"] == ["15:30"]

    def test_single_gene_still_drives_both(self):
        flat = {k: v for k, v in _flat("10:00", "10:00").items() if "_time" not in k}
        flat[SCHEDULE_TIME_GENE] = "12:00"
        trial = _build_daily_trial_config(_bt_cfg(), decode_params(_strategy(), flat), option_trade_records=False)
        assert trial["run_schedule_override"]["times"] == ["12:00"]
        assert trial["manage_schedule_override"]["times"] == ["12:00"]

    def test_a_genome_with_both_kinds_is_refused(self):
        flat = _flat("10:00", "10:00")
        flat[SCHEDULE_TIME_GENE] = "12:00"
        with pytest.raises(ValueError, match="mutually exclusive"):
            decode_params(_strategy(), flat)

    def test_a_genome_with_only_one_split_gene_is_refused(self):
        flat = _flat("10:00", "10:00")
        del flat[SCHEDULE_MANAGE_TIME_GENE]
        with pytest.raises(ValueError, match="both"):
            decode_params(_strategy(), flat)


# ----------------------------------------------------------------------------- stored-genome helpers
class TestStoredGenomeHelpers:
    def test_split_helpers(self):
        sp = _flat("09:40", "15:45")
        assert split_times_from_genes(sp) == ("09:40", "15:45")
        assert (entry_time_from_genes(sp), manage_time_from_genes(sp)) == ("09:40", "15:45")
        single = {SCHEDULE_TIME_GENE: "12:00"}
        assert split_times_from_genes(single) is None
        assert (entry_time_from_genes(single), manage_time_from_genes(single)) == ("12:00", "12:00")
        assert split_times_from_genes({"schedule:monday": 1}) is None
        assert manage_time_from_genes({"schedule:monday": 1}) is None

    def test_reconstruction_uses_the_entry_time_and_never_reads_times_as_days(self):
        out = schedule_override_from_genes(_flat("09:40", "15:45"), {"days": {}, "times": ["10:00"]},
                                           weekdays_only=True)
        assert out["times"] == ["09:40"]
        assert sorted(out["days"]) == sorted(DAYS)

    def test_a_stored_time_that_is_not_a_time_is_refused(self):
        with pytest.raises(ValueError):
            split_times_from_genes({SCHEDULE_ENTRY_TIME_GENE: 3, SCHEDULE_MANAGE_TIME_GENE: "10:00"})


# ----------------------------------------------------------------------------- launcher
class TestLauncher:
    def _resolve(self, raw, name, **kw):
        return _M._resolve_decision_times(raw, "optimize", interval="5min", name=name,
                                          bypass_jobs=kw.get("bypass", []), option_jobs=kw.get("options", []))

    def test_split_means_the_default_seven_values(self):
        assert self._resolve("split", "x-timesplit") == TIMES
        assert _M._decision_times_is_split("split") is True
        assert _M._decision_times_is_split("default") is False
        assert _M._decision_times_is_split(None) is False

    def test_split_requires_the_timesplit_token_and_timegene_does_not_do(self):
        with pytest.raises(SystemExit) as e:
            self._resolve("split", "x-timegene")
        assert "timesplit" in str(e.value)
        with pytest.raises(SystemExit) as e:
            self._resolve("default", "x-timesplit")      # a single-gene run keeps requiring timegene
        assert "timegene" in str(e.value)

    def test_split_refused_for_bypass_and_option_jobs(self):
        with pytest.raises(SystemExit):
            self._resolve("split", "x-timesplit", bypass=["FactorRanker"])
        with pytest.raises(SystemExit):
            self._resolve("split", "x-timesplit", options=["FMPRating/O_LC"])

    def test_gene_declaration(self):
        assert _M._decision_time_gene(TIMES, split=True) == {
            "schedule:entry_time": {"optimize": True, "choices": TIMES},
            "schedule:manage_time": {"optimize": True, "choices": TIMES}}
        assert _M._decision_time_gene(TIMES) == {"schedule:time": {"optimize": True, "choices": TIMES}}
        assert _M._decision_time_gene(None, split=True) == {}

    def test_help_documents_split(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            _M.main(["optimize", "--help"])
        assert "split" in buf.getvalue()


# ----------------------------------------------------------------------------- driver
_DRIVER = os.path.join(_TOOLS, "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def mf():
    sys.path.insert(0, _TOOLS)
    try:
        import matrix_flags
        return matrix_flags
    finally:
        sys.path.remove(_TOOLS)


class TestDriver:
    def test_flags_helpers(self, mf):
        assert mf.decision_times_split("split") is True and mf.decision_times_split(None) is False
        times, header = mf.decision_times_plan("split", "5min")
        assert times == TIMES and "entry_time" in header and "manage_time" in header
        assert mf.decision_times_tokens(TIMES, split=True) == ["--decision-times", "split"]
        assert mf.decision_times_tokens(TIMES) == ["--decision-times", ",".join(TIMES)]
        assert mf.with_decision_times_name("n", TIMES, split=True) == "n-timesplit"
        assert mf.with_decision_times_name("n", TIMES) == "n-timegene"
        assert mf.with_decision_times_name("n", None, split=True) == "n"

    def test_split_on_a_daily_clock_is_refused(self, mf):
        with pytest.raises(ValueError):
            mf.decision_times_plan("split", "1d")

    @staticmethod
    def _run(script, *argv):
        import subprocess
        return subprocess.run([sys.executable, script, *argv, "--dry-run"], capture_output=True, text=True)

    def test_capband_dry_run_names_carry_timesplit_and_the_bypass_job_stays_plain(self):
        r = self._run(_DRIVER, "--decision-times", "split", "--name-suffix", "g27", "--bands", "mid",
                      "--strategies", "S1", "--interval", "5min")
        assert r.returncode == 0, r.stderr
        lines = [l for l in r.stdout.splitlines() if "TODO" in l or "DONE" in l]
        assert lines, r.stdout
        s1 = [l for l in lines if "(bypass)" not in l]
        assert s1 and all("-timesplit" in l and "-timegene" not in l for l in s1), lines
        assert all("-timesplit" not in l for l in lines if "(bypass)" in l)

    def test_senate_driver_refuses_split(self):
        r = self._run(os.path.join(_TOOLS, "run_senate_matrix.py"), "--decision-times", "split",
                      "--name-suffix", "g27")
        assert r.returncode != 0 and "split" in (r.stderr + r.stdout)


# ----------------------------------------------------------------------------- export
class TestExport:
    @pytest.fixture(autouse=True)
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _opt_and_bt(strategy_params, ranges):
        from datetime import datetime
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.models.strategy import Strategy
        from app.models.strategy_optimization import StrategyOptimization
        db = SessionLocal()
        strat = Strategy(name="em-strat", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="em-opt", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {
                "backtest_id": 1, "enabled_instruments": ["AAPL"], "execution_interval": "5min",
                "experts": [{"class": "FMPRating", "settings": {}}], "account_settings": {},
                "warmup_days": 30, "seed": 42,
                "run_schedule_override": {"days": dict(WEEKDAYS_ON), "times": ["10:00"]}}},
            parameter_ranges=ranges, all_results=[], best_params={}, best_fitness=1.0, status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        bt = Backtest(name="TOP1-em", expert_name="FMPRating", engine_type="daily_expert",
                      strategy_params=strategy_params, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=opt.id)
        return db, bt

    _RANGES = {SCHEDULE_ENTRY_TIME_GENE: {"type": "choice"}, SCHEDULE_MANAGE_TIME_GENE: {"type": "choice"}}

    def test_both_times_reach_the_payload(self):
        from app.api.backtests import _derive_export_payload
        db, bt = self._opt_and_bt(_flat("09:45", "15:30"), self._RANGES)
        try:
            ex = _derive_export_payload(bt, "expert_settings", db)["execution"]
            assert ex["run_schedule_override"]["times"] == ["09:45"]
            assert ex["manage_times"] == ["15:30"]
        finally:
            db.close()

    @pytest.mark.parametrize("lost", [SCHEDULE_ENTRY_TIME_GENE, SCHEDULE_MANAGE_TIME_GENE])
    def test_a_split_run_row_missing_either_gene_is_refused(self, lost):
        from fastapi import HTTPException
        from app.api.backtests import _derive_export_payload
        sp = _flat("09:45", "15:30")
        del sp[lost]
        db, bt = self._opt_and_bt(sp, self._RANGES)
        try:
            with pytest.raises(HTTPException) as e:
                _derive_export_payload(bt, "expert_settings", db)
            assert "entry_time" in str(e.value.detail) and "manage_time" in str(e.value.detail)
        finally:
            db.close()

    def test_a_single_gene_row_has_no_manage_times_key(self):
        from app.api.backtests import _derive_export_payload
        sp = {f"schedule:{d}": int(d == "monday") for d in DAYS}
        sp[SCHEDULE_TIME_GENE] = "12:00"
        db, bt = self._opt_and_bt(sp, {SCHEDULE_TIME_GENE: {"type": "choice"}})
        try:
            ex = _derive_export_payload(bt, "expert_settings", db)["execution"]
            assert ex["run_schedule_override"]["times"] == ["12:00"] and "manage_times" not in ex
        finally:
            db.close()


# ----------------------------------------------------------------------------- deploy
def _entry(inst_id, label, entry_t, manage_t):
    sp = {f"schedule:{d}": int(d in ("tuesday", "thursday")) for d in DAYS}
    sp[SCHEDULE_ENTRY_TIME_GENE] = entry_t
    sp[SCHEDULE_MANAGE_TIME_GENE] = manage_t
    run_sched = schedule_override_from_genes(
        sp, {"days": {d: d == "monday" for d in DAYS}, "times": ["10:00"]}, weekdays_only=True)
    execution = {"run_schedule_override": run_sched, "manage_times": [manage_t]}
    return {
        "backtest_id": 1, "target_instance_id": inst_id, "account_id": 1,
        "virtual_equity_pct": 10.0, "expert_name": "FMPRating", "label": label,
        "ruleset": {"entry_rules": [], "exit_rules": []},
        "settings": {"settings": {"expert_params": {"use_atr_stop": False, "regime_overlay_enabled": False}},
                     "universe": None, "execution": execution, "execution_interval": "5min"},
    }


def _deploy(tool, tmp_path, monkeypatch, entry):
    path = str(tmp_path / "payload.json")
    json.dump([entry], open(path, "w"), default=str)
    monkeypatch.setattr(sys, "argv", ["import_deploy_payload.py", path])
    return tool.main()


def _instance():
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import ExpertInstance
    return add_instance(ExpertInstance(account_id=1, expert="FMPRating", alias="before", enabled=True,
                                       virtual_equity_pct=10.0))


class TestDeploy:
    def test_entry_time_goes_to_enter_market_and_manage_time_to_open_positions(
            self, tool, tmp_path, monkeypatch, capsys):
        from ba2_common.core.db import get_instance
        from ba2_common.core.models import ExpertInstance
        inst_id = _instance()
        rc = _deploy(tool, tmp_path, monkeypatch, _entry(inst_id, "split-row", "09:45", "15:30"))
        assert rc == 0, capsys.readouterr().out
        expert = tool._expert_class("FMPRating")(get_instance(ExpertInstance, inst_id).id)
        enter = expert.get_setting_with_interface_default("execution_schedule_enter_market")
        manage = expert.get_setting_with_interface_default("execution_schedule_open_positions")
        assert enter["times"] == ["09:45"] and manage["times"] == ["15:30"]
        assert manage["time_basis"] == "market"
        assert all(manage["days"][d] for d in DAYS[:5]) and not manage["days"]["saturday"]

    @pytest.mark.parametrize("entry_t,manage_t", [("15:50", "10:00"), ("10:00", "15:50")])
    def test_a_time_that_cannot_finish_before_the_close_is_refused_on_either_schedule(
            self, tool, tmp_path, monkeypatch, capsys, entry_t, manage_t):
        inst_id = _instance()
        assert _deploy(tool, tmp_path, monkeypatch, _entry(inst_id, "late-row", entry_t, manage_t)) == 1
        out = capsys.readouterr().out
        assert "FATAL: late-row" in out and "after the 16:00 close" in out

    def test_a_payload_without_manage_times_deploys_as_before(self, tool, tmp_path, monkeypatch, capsys):
        from ba2_common.core.db import get_instance
        from ba2_common.core.models import ExpertInstance
        inst_id = _instance()
        entry = _entry(inst_id, "old-row", "10:00", "10:00")
        del entry["settings"]["execution"]["manage_times"]
        assert _deploy(tool, tmp_path, monkeypatch, entry) == 0, capsys.readouterr().out
        expert = tool._expert_class("FMPRating")(get_instance(ExpertInstance, inst_id).id)
        assert expert.get_setting_with_interface_default("execution_schedule_open_positions")["times"] == ["10:00"]
