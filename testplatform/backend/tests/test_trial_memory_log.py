"""The per-individual telemetry line (memory + trial wall time).

This log is the ONLY trail a healthy run leaves behind while it climbs toward a worker OOM --
before it existed, the snapshot was collected on every trial but printed only on the FAILURE
path, so the first reading ever seen came from a worker that had already broken. These tests
pin the two properties that make it useful: it must never raise (it runs inside the GA's hot
loop) and it must actually carry the numbers.
"""
import logging

from app.services.strategy_optimization_handler import _log_trial_memory

MEM = {"bar_cache": {"entries": 498, "symbols": 498, "bars": 871500, "mb": 128.3},
       "series_memo": {"entries": 498, "symbols": 498, "rows": 871500, "mb": 96.1},
       "rss_mb": 2410}


def test_line_carries_time_rss_and_both_cache_layers(caplog):
    with caplog.at_level(logging.INFO):
        _log_trial_memory(2, 8, 12, 20, MEM, secs=104.7)
    msg = caplog.text
    assert "gen 3/8" in msg and "ind 12/20" in msg   # 0-indexed gen displayed 1-indexed
    assert "104.7s" in msg
    assert "2410MB" in msg
    assert "498 sym" in msg and "871500 bars" in msg
    assert "128.3MB" in msg and "96.1MB" in msg


def test_timing_survives_a_failed_memory_probe(caplog):
    """A psutil hiccup must not also cost us the timing -- a rising trial time is a signal on
    its own, and it is the earliest one (a working set outgrowing RAM slows trials down before
    it OOMs)."""
    with caplog.at_level(logging.INFO):
        _log_trial_memory(0, 8, 1, 20, {"error": "psutil boom"}, secs=99.5)
    assert "99.5s" in caplog.text
    assert "no mem probe" in caplog.text


def test_never_raises_on_absent_or_malformed_telemetry(caplog):
    """Runs in the GA's per-individual path: a missing/odd snapshot must degrade to silence,
    never take down a trial that actually succeeded."""
    with caplog.at_level(logging.INFO):
        _log_trial_memory(0, 8, 1, 20, None)
        _log_trial_memory(0, 8, 1, 20, {})           # no bar_cache / series_memo keys
        _log_trial_memory(0, 8, 1, 20, "not-a-dict", secs=1.0)
