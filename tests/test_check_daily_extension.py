"""tools/check_daily_extension.py: the read-only completion check, on synthetic caches."""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import date, datetime, timezone

import pandas as pd
import pytest

TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
chk = importlib.import_module("check_daily_extension")


def put(cache, sym, last, tz=False):
    days = pd.bdate_range("2026-09-01", last)
    d = pd.DataFrame({"Date": days, "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10})
    if tz:
        d["Date"] = d["Date"].dt.tz_localize("UTC")
    d["effective_date"] = d["Date"]
    d.to_parquet(cache / f"{sym}_1d.parquet", index=False)


def ny(text):
    return pd.Timestamp(text, tz="America/New_York").to_pydatetime().astimezone(timezone.utc)


def test_last_final_session_follows_the_settlement_delay_and_the_calendar():
    assert chk.last_final_session(ny("2026-10-07 09:31")) == date(2026, 10, 6)
    assert chk.last_final_session(ny("2026-10-06 19:59")) == date(2026, 10, 5)
    assert chk.last_final_session(ny("2026-10-06 20:00")) == date(2026, 10, 6)
    assert chk.last_final_session(ny("2026-10-10 12:00")) == date(2026, 10, 9)          # weekend
    assert chk.last_final_session(ny("2026-11-26 12:00")) == date(2026, 11, 25)         # Thanksgiving
    assert chk.last_final_session(ny("2026-11-27 17:00")) == date(2026, 11, 27)         # half day


def run(tmp_path, cache, symbols, *extra):
    sf = tmp_path / "u.txt"
    sf.write_text("\n".join(symbols), encoding="utf-8")
    rj = tmp_path / "r.json"
    code = chk.main(["--cache-dir", str(cache), "--symbols-file", str(sf), "--last-session", "2026-10-06",
                     "--report-json", str(rj), *extra])
    return code, json.loads(rj.read_text())


def test_pass_when_every_symbol_reached_or_explained(tmp_path):
    cache = tmp_path / "c"; cache.mkdir()
    put(cache, "OK1", "2026-10-06"); put(cache, "OK2", "2026-10-06", tz=True)
    put(cache, "CTBI", "2025-12-31")
    code, res = run(tmp_path, cache, ["OK1", "OK2", "CTBI"])
    assert code == 0 and res["passed"] and res["explained"] == ["CTBI"] and res["unreached"] == []


def test_fail_on_a_stale_unexplained_symbol_and_on_a_missing_file(tmp_path):
    cache = tmp_path / "c"; cache.mkdir()
    put(cache, "OK1", "2026-10-06"); put(cache, "STALE", "2026-10-02")
    code, res = run(tmp_path, cache, ["OK1", "STALE", "GONE"])
    assert code == 1 and res["unreached"] == ["GONE", "STALE"]
    code, res = run(tmp_path, cache, ["OK1", "STALE", "GONE"], "--exceptions", "STALE:halted since 10-01 per FMP profile", "GONE:no file, vendor has none")
    assert code == 0 and sorted(res["explained"]) == ["GONE", "STALE"]


def test_an_exception_needs_a_reason(tmp_path):
    cache = tmp_path / "c"; cache.mkdir()
    put(cache, "STALE", "2026-10-02")
    sf = tmp_path / "u.txt"; sf.write_text("STALE", encoding="utf-8")
    assert chk.main(["--cache-dir", str(cache), "--symbols-file", str(sf), "--last-session", "2026-10-06",
                     "--exceptions", "STALE:"]) == 2


def test_a_bar_after_the_last_final_session_fails_unless_tolerated(tmp_path):
    cache = tmp_path / "c"; cache.mkdir()
    put(cache, "TODAY", "2026-10-07")                         # a forming bar a pre-fix app left
    code, res = run(tmp_path, cache, ["TODAY"])
    assert code == 1 and res["bars_after_last_final_session"] == ["TODAY"]
    code, res = run(tmp_path, cache, ["TODAY"], "--tolerate-unfinished-today")
    assert code == 0 and res["tolerated_unfinished_today"] is True


def test_the_repair_report_requires_truncated_symbols_to_be_reextended(tmp_path):
    cache = tmp_path / "c"; cache.mkdir()
    put(cache, "A", "2026-09-11"); put(cache, "B", "2026-10-06")
    rep = tmp_path / "rep.json"
    rep.write_text(json.dumps({"truncated_symbols": ["A", "B"], "truncated_symbols_planned": ["A", "B"]}), encoding="utf-8")
    code, res = run(tmp_path, cache, ["A", "B"], "--repair-report", str(rep))
    assert code == 1 and res["truncated_not_reextended"] == ["A"]
