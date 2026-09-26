"""``tools/warm_dividend_calendars.py`` -- caches the FMP dividend calendars the option basis
guard's carry separation reads (2026-09-26). Exit 0 when every symbol was cached, 1 when any
failed (named), 2 for a configuration error. The source is a seam; no network here.

Run from ``testplatform/backend``:
    ...python.exe -m pytest tests/test_warm_dividend_calendars_cli.py -q -p no:cacheprovider
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "tools" / "warm_dividend_calendars.py"


def _tool():
    spec = importlib.util.spec_from_file_location("warm_dividend_calendars", str(_SCRIPT))
    m = importlib.util.module_from_spec(spec)
    sys.modules["warm_dividend_calendars"] = m
    spec.loader.exec_module(m)
    return m


class _Source:
    calls = 0
    bytes = 0

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.asked = []

    def dividend_calendar(self, sym):
        self.asked.append(sym)
        if sym in self.fail:
            raise RuntimeError("boom")
        return [type("E", (), {"ex_date": date(2024, 6, 3), "dividend": 1.0, "adj_dividend": 1.0})()]


def test_every_symbol_is_cached_once_and_counted(tmp_path, monkeypatch, capsys):
    m = _tool()
    src = _Source()
    monkeypatch.setattr(m, "make_source", lambda root: src)
    uni = tmp_path / "u.txt"
    uni.write_text("aaa\nBBB\n# comment\nAAA\n")
    assert m.main(["--universe-file", str(uni), "--cache-root", str(tmp_path)]) == 0
    assert src.asked == ["AAA", "BBB"]
    out = json.loads(capsys.readouterr().out)
    assert out["symbols"] == 2 and out["failed"] == {} and out["rows"] == 2


def test_a_failed_symbol_is_named_and_exits_1(tmp_path, monkeypatch, capsys):
    m = _tool()
    monkeypatch.setattr(m, "make_source", lambda root: _Source(fail={"BBB"}))
    uni = tmp_path / "u.txt"
    uni.write_text("AAA\nBBB\n")
    assert m.main(["--universe-file", str(uni), "--cache-root", str(tmp_path)]) == 1
    assert "BBB" in json.loads(capsys.readouterr().out)["failed"]


def test_a_missing_universe_file_is_a_configuration_error(tmp_path):
    m = _tool()
    assert m.main(["--universe-file", str(tmp_path / "nope.txt")]) == 2
