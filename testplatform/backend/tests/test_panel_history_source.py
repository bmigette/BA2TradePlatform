"""The gate refuses a panel built from the other OHLCV history; the build/prewarm CLI names its history (Task 8)."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_screener_gate_daily import FakePS, _panel, _runtime, ls, sg  # noqa: E402

STAMP = "ohlcv_history_source"


def _save(panel, d, **extra):
    man = {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [],
           "last_bar_date": panel.sessions[-2], "source_fingerprint": "fp", **extra}
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, {k: np.asarray(v) for k, v in panel.arrays.items()}, man)
    sg.clear_panel_memo()


@pytest.mark.parametrize("stamp,job,ok", [
    (None, "fmp", True), (None, "alpaca", False),
    ("fmp", "fmp", True), ("fmp", "alpaca", False),
    ("alpaca", "alpaca", True), ("alpaca", "fmp", False)])
def test_get_panel_checks_the_history_source(tmp_path, stamp, job, ok):
    _world, panel = _panel(2)
    _save(panel, tmp_path / "p", **({STAMP: stamp} if stamp else {}))
    if ok:
        sg.get_panel(str(tmp_path / "p"), history_source=job)
    else:
        with pytest.raises(sg.ScreenerGateRefusal, match="OHLCV history"):
            sg.get_panel(str(tmp_path / "p"), history_source=job)


def test_the_memoised_panel_is_rechecked_for_a_later_job_of_the_other_history(tmp_path):
    _world, panel = _panel(2)
    _save(panel, tmp_path / "p", **{STAMP: "alpaca"})
    sg.get_panel(str(tmp_path / "p"), history_source="alpaca")
    with pytest.raises(sg.ScreenerGateRefusal, match="OHLCV history"):
        sg.get_panel(str(tmp_path / "p"), history_source="fmp")


def test_panel_gate_refuses_a_panel_of_the_other_history(tmp_path):
    _world, panel = _panel(2)
    d = tmp_path / "p"
    _save(panel, d, **{STAMP: "alpaca"})
    rt = _runtime(str(d), {"market_cap_min": 1e9, "max_stocks": 5})
    with pytest.raises(sg.ScreenerGateRefusal, match="the job runs on 'fmp'"):
        sg.PanelGate(rt, FakePS({}), intraday=True, history_source="fmp")


def test_cli_build_and_prewarm_name_their_history(monkeypatch):
    import ba2test_launcher as L
    src = open(L.__file__, encoding="utf-8").read()
    assert 'bm.add_argument("--ohlcv-history-source", required=True, choices=["fmp", "alpaca"]' in src
    assert 'pw.add_argument("--ohlcv-history-source", choices=["fmp", "alpaca"], default=None' in src
    # prewarm refuses a panel build without it, before any network call
    args = type("A", (), {"symbols": None, "experts": "", "screener_panel": True, "screener_store": "s",
                          "ohlcv_history_source": None})()
    monkeypatch.setattr("app.services.prewarm_fetchers.resolve_keys", lambda: {"fmp": "k", "finnhub": None})
    with pytest.raises(SystemExit, match="--ohlcv-history-source"):
        L._cmd_prewarm(args)
