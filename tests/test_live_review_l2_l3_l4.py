"""Live-side review batch (2026-10-07): L2 pass-level price outage ERROR, L3 the parsed schedule is
visible, L4 session-guard internals."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ba2_common.core import pass_price_health as PPH
from ba2_common.core import schedule_genes as SG
from ba2_trade_platform.core import JobManager as JM
from ba2_trade_platform.core.types import AnalysisUseCase

NY = ZoneInfo("America/New_York")


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


NORMAL = (2025, 12, 3)
HOLIDAY = (2025, 12, 25)


# ----------------------------------------------------------------------------------------------- L2
@pytest.fixture(autouse=True)
def _clean_health():
    PPH.reset()
    yield
    PPH.reset()


def _errors(monkeypatch):
    seen = []
    monkeypatch.setattr(PPH.logger, "error", lambda m, *a, **k: seen.append(m))
    return seen


def test_a_broker_outage_is_ONE_pass_level_error_with_the_count_the_account_and_the_first_error(monkeypatch):
    seen = _errors(monkeypatch)
    PPH.note_error(7, "AAPL", ConnectionError("broker down"))
    PPH.note_error(7, "MSFT", ConnectionError("a later error"))
    for sym in ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOG"):
        PPH.record_no_price(7, sym)
    msg = PPH.summarise_pass(7, 10, account_id=3, batch_id="7_0930_20251203")
    assert len(seen) == 1 and msg == seen[0]
    for needle in ("expert instance 7", "account 3", "6 of 10", "AAPL: ConnectionError: broker down"):
        assert needle in msg
    assert "a later error" not in msg                     # the FIRST error
    assert PPH.summarise_pass(7, 10) is None              # counters were reset by the summary


def test_a_few_unpriced_symbols_are_not_an_outage(monkeypatch):
    seen = _errors(monkeypatch)
    for sym in ("A", "B"):
        PPH.record_no_price(7, sym)
    assert PPH.summarise_pass(7, 10) is None              # 20% of 10 is not MORE than 20%
    for sym in ("A", "B", "C", "D"):                      # 4 symbols of 5: over the share, under the minimum
        PPH.record_no_price(7, sym)
    assert PPH.summarise_pass(7, 5) is None
    assert seen == []


def test_experts_are_summarised_separately(monkeypatch):
    seen = _errors(monkeypatch)
    for sym in "ABCDEF":
        PPH.record_no_price(1, sym)
    assert PPH.summarise_pass(2, 6) is None
    assert PPH.summarise_pass(1, 6) is not None and len(seen) == 1


def test_the_expert_skip_is_a_warning_per_symbol_and_feeds_the_pass_counter():
    from unittest.mock import MagicMock
    import pandas as pd
    from ba2_experts.DeterministicScorer import DeterministicScorer
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 11
    e.logger = MagicMock()
    for sym in ("A", "B", "C", "D", "E", "F"):
        e._process({"symbol": sym, "ohlcv": pd.DataFrame({"Close": [1.0] * 400}), "current_price": None},
                   {"min_history_days": 260}, None)
    e.logger.error.assert_not_called()
    assert e.logger.warning.call_count == 6
    assert PPH.summarise_pass(11, 6, account_id=1) is not None


def test_the_get_current_price_failure_is_a_warning_and_remembered_for_the_summary(monkeypatch):
    import importlib
    MEI = importlib.import_module("ba2_common.core.interfaces.MarketExpertInterface")
    from ba2_experts.DeterministicScorer import DeterministicScorer
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 5
    seen = []
    monkeypatch.setattr(MEI.logger, "warning", lambda m, *a, **k: seen.append(("W", m)))
    monkeypatch.setattr(MEI.logger, "error", lambda m, *a, **k: seen.append(("E", m)))
    monkeypatch.setattr(MEI, "get_instance", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    assert e._get_current_price("AAPL") is None
    assert [lvl for lvl, _ in seen] == ["W"]
    assert PPH._first_error[5].startswith("AAPL: ConnectionError")


# ----------------------------------------------------------------------------------------------- L3
def _cap(monkeypatch):
    seen = []
    monkeypatch.setattr(JM.logger, "info", lambda m, *a, **k: seen.append(("INFO", m)))
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: seen.append(("WARNING", m)))
    return seen


def _sched(days, times=("09:30",), basis="market"):
    return {"days": days, "times": list(times), "time_basis": basis}


FULL_MON = {"monday": True, "tuesday": False, "wednesday": False, "thursday": False,
            "friday": False, "saturday": False, "sunday": False}


def test_a_complete_schedule_logs_one_info_line(monkeypatch):
    seen = _cap(monkeypatch)
    JM.JobManager._log_parsed_schedule(SimpleNamespace(id=7), "enter_market", _sched(FULL_MON))
    assert seen == [("INFO", "schedule inst 7 enter_market: Mon @ 09:30 market")]


def test_a_partial_days_dict_logs_a_warning_naming_the_days_it_implies(monkeypatch):
    seen = _cap(monkeypatch)
    JM.JobManager._log_parsed_schedule(SimpleNamespace(id=7), "open_positions",
                                       _sched({"monday": True}, ("09:30", "15:30"), "local"))
    (level, line), = seen
    assert level == "WARNING"
    assert line == ("schedule inst 7 open_positions: Mon,Tue,Wed,Thu,Fri @ 09:30, 15:30 local "
                    "(implied by default: Tue,Wed,Thu,Fri,Sat,Sun)")


def test_describe_schedule_reads_by_the_shared_rule_and_skips_what_it_does_not_describe():
    info = SG.describe_schedule(_sched({"monday": "false", "friday": 1}))
    assert info["days"] == ["Tue", "Wed", "Thu", "Fri"] and info["implied"] == ["Tue", "Wed", "Thu", "Sat", "Sun"]
    assert SG.describe_schedule({"days": {"wensday": True}, "times": ["09:30"]}) is None     # refused elsewhere
    assert SG.describe_schedule("0 9 * * *") is None


# ----------------------------------------------------------------------------------------------- L4
def _jm():
    jm = JM.JobManager.__new__(JM.JobManager)
    return jm


def _job(expert=7, symbol="AAPL", subtype=AnalysisUseCase.ENTER_MARKET):
    return SimpleNamespace(args=[expert, symbol, subtype])


def _levels(monkeypatch):
    seen = []
    monkeypatch.setattr(JM.logger, "info", lambda m, *a, **k: seen.append(("INFO", m)))
    monkeypatch.setattr(JM.logger, "warning", lambda m, *a, **k: seen.append(("WARNING", m)))
    monkeypatch.setattr(JM.logger, "error", lambda m, *a, **k: seen.append(("ERROR", m)))
    return seen


def test_the_group_resolves_crypto_in_one_query_outside_the_per_job_loop(monkeypatch):
    queries = []

    class FakeSession:
        def __init__(self, bind): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec(self, stmt):
            queries.append(stmt)
            return SimpleNamespace(all=lambda: [SimpleNamespace(name="BTC", instrument_type=_CRYPTO)])

    from ba2_trade_platform.core.types import InstrumentType
    _CRYPTO = InstrumentType.CRYPTO
    monkeypatch.setattr(JM, "Session", FakeSession)
    monkeypatch.setattr(JM, "get_db", lambda: SimpleNamespace(bind=None))
    jm = _jm()
    jm._session_guard_enabled = lambda announce=False: True
    jm._symbol_is_crypto = lambda s: (_ for _ in ()).throw(AssertionError("per-job query"))
    due = [_job(symbol=s) for s in ("AAPL", "MSFT", "BTC", "OPEN_POSITIONS", "NVDA")]
    allowed = jm._apply_session_guard(due, _at(*NORMAL, 9, 30))
    assert len(queries) == 1
    assert [j.args[1] for j in allowed] == ["AAPL", "MSFT", "OPEN_POSITIONS", "NVDA"]   # BTC is crypto: refused


def test_a_database_error_on_the_instrument_read_is_ONE_error_with_the_number_of_jobs(monkeypatch):
    seen = _levels(monkeypatch)
    jm = _jm()
    jm._session_guard_enabled = lambda announce=False: True
    monkeypatch.setattr(JM.JobManager, "_crypto_symbols",
                        staticmethod(lambda symbols: (_ for _ in ()).throw(OSError("db locked"))))
    due = [_job(expert=i, symbol="AAPL") for i in range(1, 5)]
    assert jm._apply_session_guard(due, _at(*NORMAL, 9, 30)) == []                  # fail closed
    errors = [m for lvl, m in seen if lvl == "ERROR"]
    assert len(errors) == 1 and "all 4 job(s)" in errors[0] and "db locked" in errors[0]


def test_the_number_of_skipped_jobs_is_logged_once_per_group(monkeypatch):
    seen = _levels(monkeypatch)
    jm = _jm()
    jm._session_guard_enabled = lambda announce=False: True
    monkeypatch.setattr(JM.JobManager, "_crypto_symbols", staticmethod(lambda symbols: set()))
    due = [_job(expert=i) for i in range(1, 4)]
    assert jm._apply_session_guard(due, _at(*HOLIDAY, 9, 30)) == []
    counts = [m for lvl, m in seen if "3 of 3 job(s)" in m]
    assert len(counts) == 1


def test_a_kill_switch_read_that_raises_is_an_error_naming_the_setting_and_the_guard_stays_ON(monkeypatch):
    seen = _levels(monkeypatch)
    jm = _jm()

    def boom(announce=False):
        raise RuntimeError("defect in the switch")

    jm._session_guard_enabled = boom
    monkeypatch.setattr(JM.JobManager, "_crypto_symbols", staticmethod(lambda symbols: set()))
    assert jm._apply_session_guard([_job()], _at(*HOLIDAY, 9, 30)) == []             # evaluated: holiday -> skipped
    kill = [m for lvl, m in seen if lvl == "ERROR" and JM.SESSION_GUARD_SETTING in m]
    assert len(kill) == 1 and "evaluating the guard as ON" in kill[0]
    assert jm._apply_session_guard([_job()], _at(*NORMAL, 9, 30)) != []              # a normal session still runs


def test_the_disabled_guard_passes_everything_with_one_warning(monkeypatch):
    seen = _levels(monkeypatch)
    jm = _jm()
    jm._session_guard_enabled = lambda announce=False: False
    due = [_job(), _job(expert=8)]
    assert jm._apply_session_guard(due, _at(*HOLIDAY, 9, 30)) == due
    assert len([1 for lvl, m in seen if lvl == "WARNING" and "DISABLED" in m]) == 1


def test_the_session_calendar_is_prewarmed_and_its_duration_logged(monkeypatch):
    seen = _levels(monkeypatch)
    JM.JobManager._prewarm_session_calendar()
    assert any(lvl == "INFO" and "calendar ready in" in m for lvl, m in seen)
