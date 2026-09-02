"""The perf harness must actually TRADE, or its wall-clock measures the wrong program.

``tools/perf_sample_bt.py`` exists to time the enter path plus the fill engine ("exercises the
enter path (temp-list order flow) + the fill engine (bracket / TP-SL fills) so the wall-clock
reflects the order-simulator cost" -- its own docstring). It has been printing ``orders/run=0``:
every BUY action was created and evaluated, and then the candidate risk manager raised
``'object' object has no attribute 'get_indicator'`` on EVERY expert on EVERY bar, because the
harness installed a bare ``object()`` as the engine's indicator provider. ATR sizing calls
``get_indicator``; the engine caught the AttributeError, logged a WARNING, and sized nothing. So
the numbers were the cost of a backtest that evaluates rules and never places an order -- the
half of the program the harness was written to measure was never running.

This test is the guard: the harness must place orders. It runs a deliberately TINY sample
(3 symbols x 40 bars, one run) so it costs a fraction of a second in the suite while still
proving the whole chain -- expert -> enter ruleset -> risk manager -> order.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_REPO, "tools", "perf_sample_bt.py")


@pytest.fixture()
def harness(monkeypatch):
    """The REAL script, loaded with a tiny sample size.

    The size constants are read at IMPORT time from the environment, so they are set before the
    module is executed -- and the module is loaded fresh under a private name so this does not
    disturb (or get disturbed by) any other import of it.

    THE GLOBAL LOGGING DISABLE IS RESTORED, and that is not housekeeping. The script calls
    ``logging.disable(logging.CRITICAL)`` at module scope -- correct for a perf harness (log
    formatting would dominate the measurement) but PROCESS-WIDE and permanent. Left set, it
    silently suppressed a later test in the same session that asserts a WARNING reaches a log
    file (test_worker_server.py::test_install_orchestration_file_logging_persists_root_warnings
    failed in a full-suite run and passed alone -- measured, not guessed). The level lives on
    ``logging.root.manager.disable``; snapshot and put it back."""
    import logging

    monkeypatch.setenv("PERF_SYMS", "3")
    monkeypatch.setenv("PERF_BARS", "40")
    monkeypatch.setenv("PERF_RUNS", "1")
    disable_before = logging.root.manager.disable
    spec = importlib.util.spec_from_file_location("perf_sample_bt_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["perf_sample_bt_under_test"] = mod
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        sys.modules.pop("perf_sample_bt_under_test", None)
        logging.disable(disable_before)


def test_the_sample_backtest_places_orders(harness):
    """THE REGRESSION. ``orders/run`` is what the harness prints beside its wall-clock, and a
    zero there means the timing describes a run with no order flow at all."""
    seconds, orders = harness._run_once()
    assert orders > 0, (
        "the perf sample placed NO orders -- its wall-clock is measuring rule evaluation only, "
        "not the enter path + fill engine it claims to measure")
    assert seconds > 0


def test_the_indicator_provider_stub_answers_get_indicator(harness):
    """The specific defect, pinned at its own seam: ATR sizing calls ``get_indicator`` on the
    injected provider, and a stub without it makes the risk manager fail on every bar."""
    provider = harness._indicator_stub()
    out = provider.get_indicator("S0", "atr", end_date=None, lookback_days=60,
                                 interval="1day", format_type="dict", period=14)
    assert out["values"], "the stub must return at least one ATR value"
    assert all(isinstance(v, float) for v in out["values"])


def test_the_stub_is_deterministic_and_offline(harness):
    """Hermetic is the harness's whole premise ('no network/data'): the same call must give the
    same answer, and it must not depend on anything the sample does not construct itself."""
    a = harness._indicator_stub().get_indicator("S0", "atr", period=14)
    b = harness._indicator_stub().get_indicator("S0", "atr", period=14)
    assert a == b
