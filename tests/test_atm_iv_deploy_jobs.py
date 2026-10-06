"""F1/F2/F10: DGS3MO upkeep, the 09:05 warm job, JobManager wiring, the backfill tool's guards."""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options import atm_iv_task_hook as W
from tests.atm_iv_fakes import make_provider, reset_module_state

END = __import__("datetime").date(2026, 3, 6)


@pytest.fixture(autouse=True)
def _clean():
    reset_module_state()
    yield
    reset_module_state()


# ---- F1: DGS3MO -----------------------------------------------------------------------------
def _write(path, fetched_at):
    path.write_text(json.dumps({"series_id": "DGS3MO", "fetched_at": fetched_at.isoformat(),
                                "observations": []}))


def test_dgs3mo_is_fetched_when_missing_and_skipped_at_startup_when_fresh(tmp_path):
    p = tmp_path / "DGS3MO.json"
    calls = []
    now = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
    ref = lambda sid, key: calls.append((sid, key)) or 900
    assert W.ensure_dgs3mo(only_if_stale=True, path=str(p), key_resolver=lambda: "k", refresher=ref, now=now) == "refreshed"
    _write(p, now - timedelta(hours=2))
    assert W.ensure_dgs3mo(only_if_stale=True, path=str(p), key_resolver=lambda: "k", refresher=ref, now=now) == "current"
    assert len(calls) == 1
    # the 09:00 job refreshes unconditionally
    assert W.ensure_dgs3mo(only_if_stale=False, path=str(p), key_resolver=lambda: "k", refresher=ref, now=now) == "refreshed"
    assert len(calls) == 2
    # a file older than the startup threshold is refreshed at startup
    _write(p, now - timedelta(hours=30))
    assert W.ensure_dgs3mo(only_if_stale=True, path=str(p), key_resolver=lambda: "k", refresher=ref, now=now) == "refreshed"


def test_dgs3mo_failures_are_logged_not_raised(tmp_path):
    def boom(sid, key):
        raise RuntimeError("fred down")
    assert W.ensure_dgs3mo(only_if_stale=False, path=str(tmp_path / "x.json"),
                           key_resolver=lambda: "k", refresher=boom) == "failed"
    assert W.ensure_dgs3mo(only_if_stale=False, path=str(tmp_path / "x.json"),
                           key_resolver=lambda: None, refresher=boom) == "no_key"


def test_the_0900_job_refreshes_dgs3mo_even_without_a_deterministicscorer(monkeypatch):
    from ba2_trade_platform.core.JobManager import JobManager
    calls = []
    monkeypatch.setattr(W, "has_iv_rank_gates", lambda: True)
    monkeypatch.setattr(W, "ensure_dgs3mo", lambda **k: calls.append(k) or "refreshed")
    stub = SimpleNamespace(_any_enabled_expert=lambda n: False,
                           _ensure_dgs3mo=lambda only_if_stale: JobManager._ensure_dgs3mo(stub, only_if_stale))
    JobManager._execute_fred_preopen_refresh(stub)
    assert calls == [{"only_if_stale": False}]
    # no iv_rank gates -> no FRED request at all
    calls.clear()
    monkeypatch.setattr(W, "has_iv_rank_gates", lambda: False)
    JobManager._execute_fred_preopen_refresh(stub)
    assert calls == []


# ---- F10: the backfill tool -------------------------------------------------------------------
def test_backfill_tool_refuses_every_apply_without_the_flag_unless_scratch(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_iv_history", os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "backfill_iv_history.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    # live default, and an arbitrary dir without --scratch, both refuse
    assert tool.main(["--symbols", "AAPL", "--apply"]) == 2
    assert tool.main(["--symbols", "AAPL", "--apply", "--cache-dir", str(tmp_path)]) == 2
    # --scratch on something that looks like an app cache still refuses
    from ba2_trade_platform import config
    assert tool.main(["--symbols", "AAPL", "--apply", "--scratch", "--cache-dir",
                      os.path.join(config.CACHE_FOLDER, "AtmIvHistory")]) == 2
    assert not tool._is_scratch(os.path.join(config.CACHE_FOLDER, "x"))
    assert not tool._is_scratch(r"C:\Users\x\Documents\ba2_trade_platform-opt\cache\AtmIvHistory")
    assert tool.main(["--symbols", "AAPL", "--cache-dir", str(tmp_path / "dry")]) == 0   # dry run needs nothing
