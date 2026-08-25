"""preload()'s bulk/cold-start timing (see price_source.py's TIMING (2026-08-25) comment,
_PRELOAD_BULK_LOG_MIN, and _worker_log).

preload() runs once per individual -- thousands of times over a GA run -- and almost every one
of those calls hits a warm _WORKER_BAR_CACHE. Logging every call would be exactly the
per-file-read noise the user asked NOT to have; logging none would hide the one case that
actually matters, a bulk/mostly-cold load (a worker's first individual after spawn/recycle).

These go through _worker_log, not logger/print alone: preload() runs inside a spawned pool
child, where _worker_init has already installed a process-global logging.disable(ERROR) (a
logger call there is silently dropped), and a bare print() is not durably retrievable on a
remote worker unless its whole process tree happens to have shell-level stdout redirection.
_worker_log writes to stdout (capsys catches that half) AND appends to this pid's own
LOGS_DIR/worker_child_<pid>.log (the durable half, retrievable via worker_server.py's existing
/logs endpoint with no changes there).
"""
from datetime import datetime

import pytest

from app.services.backtest import price_source as ps


class _FakeOHLCV:
    """Minimal provider: every symbol gets 2 bars. Counts reads so a test can prove a hit
    skipped the disk entirely."""

    def __init__(self):
        self.reads = []

    def read_window(self, symbol, start, end, interval):
        import pandas as pd
        self.reads.append(symbol)
        return pd.DataFrame({
            "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "Open": [1.0, 2.0], "High": [1.0, 2.0],
            "Low": [1.0, 2.0], "Close": [1.0, 2.0], "Volume": [10, 20],
        })


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    # LOGS_DIR redirected to a throwaway tmp_path for every test in this file -- _worker_log
    # writes a real file per pid, and the real LOGS_DIR must never be touched by a test run.
    import ba2_common.logger as _bl
    monkeypatch.setattr(_bl, "LOGS_DIR", str(tmp_path))
    ps.clear_worker_bar_cache()
    ps._TRIAL_SEQ = 0
    yield
    ps.clear_worker_bar_cache()
    ps._TRIAL_SEQ = 0


def _preload(provider, symbols):
    src = ps.AsOfPriceSource(ohlcv_provider=provider, interval="1d")
    src.preload(symbols, datetime(2024, 1, 2), datetime(2024, 1, 3), warmup_days=0)
    return src


def test_a_small_preload_prints_nothing(capsys, monkeypatch):
    """Below the bulk threshold -> completely silent, even though every symbol is a cold load."""
    monkeypatch.setattr(ps, "_PRELOAD_BULK_LOG_MIN", 20)
    _preload(_FakeOHLCV(), [f"S{i}" for i in range(5)])  # 5 < 20
    assert capsys.readouterr().out == ""


def test_a_bulk_cold_preload_prints_start_and_stop(capsys, monkeypatch):
    """At/above the threshold -> both lines fire, with real counts."""
    monkeypatch.setattr(ps, "_PRELOAD_BULK_LOG_MIN", 3)
    symbols = [f"S{i}" for i in range(5)]  # 5 >= 3
    _preload(_FakeOHLCV(), symbols)

    out = capsys.readouterr().out
    assert ">> preload starting: 5 symbol(s), ~5 to load from disk" in out
    assert ">> preload done: 0 cached, 5 loaded from disk, 0 missing" in out


def test_a_bulk_cold_preload_is_durably_retrievable_afterwards(monkeypatch, tmp_path):
    """The actual ask: these lines must survive after the trial/worker is gone, retrievable via
    worker_server.py's existing /logs endpoint -- i.e. a real file under LOGS_DIR, not just
    whatever stdout happened to catch live."""
    import os
    monkeypatch.setattr(ps, "_PRELOAD_BULK_LOG_MIN", 3)
    _preload(_FakeOHLCV(), [f"S{i}" for i in range(5)])

    path = tmp_path / f"worker_child_{os.getpid()}.log"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert ">> preload starting: 5 symbol(s), ~5 to load from disk" in content
    assert ">> preload done: 0 cached, 5 loaded from disk, 0 missing" in content


def test_worker_log_is_best_effort_never_raises(monkeypatch, capsys):
    """A disk-full / permissions / vanished-LOGS_DIR failure must never break the trial
    _worker_log is reporting on -- it's diagnostics, not part of the trial's own contract."""
    import ba2_common.logger as _bl
    monkeypatch.setattr(_bl, "LOGS_DIR", "/definitely/does/not/exist/at/all")
    ps._worker_log(">> this must not raise even though the directory is bogus")
    assert ">> this must not raise even though the directory is bogus" in capsys.readouterr().out


def test_a_warm_repeat_of_a_bulk_load_falls_silent(capsys, monkeypatch):
    """The scenario this exists for: the FIRST preload after a worker starts is bulk/cold and
    logs; every later individual reusing the same window is warm and goes back to silent --
    proving this is genuinely gated on cache state, not just a raw symbol-count threshold.

    BT_BAR_CACHE_TRIALS must be > 0 for this: the default (0) is "flush per individual" --
    every preload starts cold by design, so a repeat would stay cold too and this test would
    not actually exercise the warm-cache path it's named for."""
    monkeypatch.setattr(ps, "_PRELOAD_BULK_LOG_MIN", 3)
    monkeypatch.setattr(ps, "_WORKER_BAR_CACHE_TRIALS", 5)
    provider = _FakeOHLCV()
    symbols = [f"S{i}" for i in range(5)]

    _preload(provider, symbols)
    first_call_out = capsys.readouterr().out
    assert "preload starting" in first_call_out  # sanity: the setup above did log

    _preload(provider, symbols)  # same window -> every symbol now cached
    second_call_out = capsys.readouterr().out
    assert second_call_out == ""
    assert len(provider.reads) == 5  # confirms the second call truly skipped the disk
