"""tools/check_window_coverage.py --end (goal2027 design 2.6): the cache must reach the END of the window
too, not only its start. A symbol whose DAILY file also stops early is delisted (excluded), not missing."""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import check_window_coverage as CWC  # noqa: E402


def _bars(path, first, last):
    pd.DataFrame({"Date": pd.to_datetime([first, last], utc=True), "Close": [1.0, 1.0]}).to_parquet(path)


@pytest.fixture
def cache(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    _bars(d / "FULL_5min.parquet", "2020-01-02", "2025-12-31")
    _bars(d / "FULL_1d.parquet", "2015-01-02", "2025-12-31")
    _bars(d / "SHORT_5min.parquet", "2020-01-02", "2025-06-30")     # intraday stops early: a defect
    _bars(d / "SHORT_1d.parquet", "2015-01-02", "2025-12-31")
    _bars(d / "GONE_5min.parquet", "2020-01-02", "2023-03-01")      # delisted: daily stops too
    _bars(d / "GONE_1d.parquet", "2015-01-02", "2023-03-01")
    return str(d)


def _run(monkeypatch, cache, *argv):
    monkeypatch.setattr(sys, "argv", ["check_window_coverage.py", "--cache-dir", cache, "--start", "2020-01-01",
                                      "--sample", "10", *argv])
    return CWC.main()


def test_without_end_only_the_start_is_checked(monkeypatch, cache):
    assert _run(monkeypatch, cache, "--min-covered-pct", "100") == 0


def test_end_flags_a_symbol_whose_intraday_stops_early(monkeypatch, cache, capsys):
    assert _run(monkeypatch, cache, "--end", "2025-12-31", "--min-covered-pct", "100") == 1
    out = capsys.readouterr().out
    assert "SHORT" in out and "last bar 2025-06-30" in out


def test_a_delisted_symbol_is_excluded_from_the_end_check(monkeypatch, cache, capsys):
    assert _run(monkeypatch, cache, "--end", "2025-12-31", "--min-covered-pct", "50") == 0
    out = capsys.readouterr().out
    assert "1 delisted before the end (excluded)" in out
    assert "GONE " not in out                                  # never listed as a defect


def test_end_before_start_is_refused(monkeypatch, cache):
    assert _run(monkeypatch, cache, "--end", "2019-06-30") == 2
