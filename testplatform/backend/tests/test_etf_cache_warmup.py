"""Hermetic tests for cache writes, restart recovery and missing-session failures."""
from datetime import date, datetime, timezone
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research import warm_etf_cache as W


@pytest.fixture
def cache(tmp_path, monkeypatch):
    from ba2_common import config
    from ba2_common.core import native_cache
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path))
    path = tmp_path / "FMPOHLCVProvider"
    path.mkdir()
    return path


def bars(days, *, interval="1d"):
    dates = []
    for day in days:
        if interval == "1d":
            dates.append(pd.Timestamp(day, tz="UTC"))
        else:
            dates.extend(pd.date_range(f"{day} 14:30", periods=78, freq="5min", tz="UTC"))
    return pd.DataFrame({"Date": pd.to_datetime(dates, utc=True), "Open": 100., "High": 102.,
                         "Low": 99., "Close": 101., "Volume": 1000.})


def test_plan_matches_driver_and_warms_both_engine_intervals():
    plan = W.build_plan("2020-01-01", "2025-12-31", ["SPY", "IEF", "TLT", "GLD"])
    assert plan["warmup_days"] == 600
    assert plan["fetch_start"] == "2018-05-11"
    assert plan["intervals"] == ["1d", "5min"]


def test_preview_uses_no_provider_database_or_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "configure_runtime", lambda *a: pytest.fail("preview imported providers"))
    monkeypatch.setattr(W, "read_key", lambda *a: pytest.fail("preview read credentials"))
    cache_dir = tmp_path / "FMPOHLCVProvider"
    assert W.main(["--dry-run", "--cache-dir", str(cache_dir), "--output-dir", str(tmp_path / "out")]) == 0
    assert not cache_dir.exists()
    assert (tmp_path / "out/plan.json").is_file()


def test_key_read_is_readonly_and_environment_has_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    dbpath = tmp_path / "settings.sqlite"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE appsetting (key TEXT, value_str TEXT)")
        db.execute("INSERT INTO appsetting VALUES (?, ?)", ("FMP_API_KEY", "fake-db-key"))
    before = dbpath.read_bytes()
    assert W.read_key(dbpath) == "fake-db-key"
    assert dbpath.read_bytes() == before
    monkeypatch.setenv("FMP_API_KEY", "fake-env-key")
    assert W.read_key(tmp_path / "absent.sqlite") == "fake-env-key"


def test_explicit_provider_key_does_not_open_settings_db(cache, monkeypatch):
    module = importlib.import_module("ba2_providers.ohlcv.FMPOHLCVProvider")
    monkeypatch.setattr(module, "get_app_setting", lambda *a: pytest.fail("DB opened"))
    assert module.FMPOHLCVProvider(api_key="fake-key").api_key == "fake-key"
    with pytest.raises(ValueError, match="not configured"):
        module.FMPOHLCVProvider(api_key="")


def test_weekends_holidays_and_half_days_are_not_false_gaps():
    plan = {"fetch_start": "2024-11-28", "end": "2024-12-02"}
    assert W.session_requirements(plan, "5min") == {date(2024, 11, 29): 42, date(2024, 12, 2): 78}


def test_internal_one_day_gap_is_fetched_and_resume_skips_everything(cache):
    plan = {"fetch_start": "2024-01-02", "end": "2024-01-04"}
    # Preserve unrelated earlier history, and detect a single-session interior gap.
    W.save_frame(cache, "SPY", "1d", bars(["2023-12-29", "2024-01-02", "2024-01-04"]))
    calls = []
    def fetch(symbol, start, end, interval):
        calls.append((start, end))
        assert start.date() == end.date() == date(2024, 1, 3)
        assert end.hour == 23 and end.minute == 59  # inclusive last session
        return bars(["2024-01-03"])
    provider = SimpleNamespace(_get_ohlcv_data_impl=fetch)
    assert W.warm_pair(plan, cache, "SPY", "1d", provider)["status"] == "passed"
    assert W.warm_pair(plan, cache, "SPY", "1d", provider)["fetched_chunks"] == 0
    assert len(calls) == 1
    assert len(W.read_cache(cache, "SPY", "1d")) == 4


def test_partial_response_is_saved_but_never_reported_success(cache):
    plan = {"fetch_start": "2024-01-02", "end": "2024-01-04"}
    provider = SimpleNamespace(_get_ohlcv_data_impl=lambda *a: bars(["2024-01-02"]))
    with pytest.raises(ValueError, match="Incomplete FMP response"):
        W.warm_pair(plan, cache, "SPY", "1d", provider)
    assert len(W.read_cache(cache, "SPY", "1d")) == 1
    calls = []
    def remaining(symbol, start, end, interval):
        calls.append(start.date())
        return bars(["2024-01-03", "2024-01-04"])
    provider._get_ohlcv_data_impl = remaining
    assert W.warm_pair(plan, cache, "SPY", "1d", provider)["status"] == "passed"
    assert calls == [date(2024, 1, 3)]


def test_empty_response_is_failure_without_cache_write(cache):
    provider = SimpleNamespace(_get_ohlcv_data_impl=lambda *a: bars([]))
    with pytest.raises(ValueError, match="no bars"):
        W.warm_pair({"fetch_start": "2024-01-02", "end": "2024-01-02"}, cache, "SPY", "1d", provider)
    assert not (cache / "SPY_1d.parquet").exists()


def test_short_intraday_session_fails_despite_valid_bounds():
    required = {date(2024, 1, 2): 78}
    frame = bars(["2024-01-02"], interval="5min")
    frame = frame.drop(frame.index[20])
    assert W.coverage(frame, required)["missing_or_short_sessions"] == ["2024-01-02"]


@pytest.mark.parametrize("defect", ["duplicate", "nan", "zero", "negative_volume", "bounds"])
def test_bad_bars_cannot_be_persisted(cache, defect):
    frame = bars(["2024-01-02"])
    if defect == "duplicate":
        frame = pd.concat([frame, frame])
    elif defect == "nan":
        frame.loc[0, "Close"] = float("nan")
    elif defect == "zero":
        frame.loc[0, "Close"] = 0
    elif defect == "negative_volume":
        frame.loc[0, "Volume"] = -1
    else:
        frame.loc[0, "High"] = 50
    with pytest.raises(ValueError):
        W.save_frame(cache, "SPY", "1d", frame)
    assert not (cache / "SPY_1d.parquet").exists()


def test_conflicting_aliases_fail_without_rewriting(cache):
    bars(["2024-01-02"], interval="5min").to_parquet(cache / "SPY_5min.parquet")
    bars(["2024-01-02"], interval="5min").to_parquet(cache / "SPY_5m.parquet")
    with pytest.raises(ValueError, match="Conflicting"):
        W.read_cache(cache, "SPY", "5min")


def test_check_reports_each_failure_and_exits_nonzero(cache, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "configure_runtime", lambda *a: None)
    monkeypatch.setattr(W, "read_key", lambda *a: pytest.fail("check read credentials"))
    monkeypatch.setattr(W, "session_requirements", lambda *a: {date(2024, 1, 2): 1})
    out = tmp_path / "report"
    assert W.main(["--check", "--cache-dir", str(cache), "--output-dir", str(out)]) == 1
    report = json.loads((out / "coverage.json").read_text())
    assert len(report["results"]) == 8
    assert all(r["status"] == "failed" for r in report["results"])


def test_error_messages_redact_credentials():
    assert "secret" not in W.clean_error(ValueError("key secret url?apikey=encoded_secret&x=1"), "secret")


def test_intraday_chunks_are_small_and_cover_last_day():
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    assert list(W.windows(days, "5min")) == [
        (date(2024, 1, 2), date(2024, 1, 4)), (date(2024, 1, 5), date(2024, 1, 5))]
