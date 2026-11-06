"""A recorder failure on the market-condition path degrades to NOT RECORDED, never to a failed decision.

The exit pass of an option expert now opens a capture scope, so ``begin_decision`` reaches its capture
branch on a pass that must never be stopped by recording (a stop-loss included). A failure building the
capturing reader, or recording one window while a gate is being read, is counted in CaptureHealth
(``note_failure``), logged once, and the live reader is used unchanged.
"""
from datetime import date, datetime, timezone

import ba2_common.core.market_condition_live as live
import ba2_common.core.market_condition_readers as readers
from ba2_common.core.replay.context import CaptureHealth, capture_scope

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.health = CaptureHealth()

    def submit(self, record, objects=None, observations=()):
        pass


def _meta():
    return {"analysis_id": "a1", "attempt_id": "att", "session_id": "s", "expert_class": "TradeManager",
            "expert_instance_id": 7, "symbol": "", "use_case": "open_positions",
            "scheduled_at": None, "started_at": NOW}


def _resolver(reader):
    resolver = live.LiveMarketConditionResolver.__new__(live.LiveMarketConditionResolver)
    resolver.reader = reader
    resolver.source_profile = "p"
    resolver.decisions = 0
    resolver.refresh_coverage = lambda *a, **k: []
    return resolver


def test_begin_decision_degrades_when_the_capturing_reader_cannot_be_built(monkeypatch):
    reader = object()

    def _boom(*a, **k):
        raise RuntimeError("cannot wrap")

    monkeypatch.setattr(live, "_replay_now", lambda: NOW)
    monkeypatch.setattr(readers, "capturing_reader_for", _boom)
    store = _Store()
    with capture_scope(store, _meta()) as ctx:
        state = _resolver(reader).begin_decision()
        assert state.reader is reader and state.recorder is None
        assert ctx.capture_failures  # counted, not raised


def test_recording_a_window_never_raises_into_the_gate_read():
    class _Inner:
        profile = "p"
        calc_version = "v"

        def __init__(self):
            self.n = 0
            self._retain_windows = True

        def observe_window(self, symbol, session):
            self.n += 1
            raise RuntimeError("the re-read fails")

    class _Ctx:
        failures = []

        def note_failure(self, message, exc):
            self.failures.append(message)

        def record_observation(self, **k):
            raise AssertionError("not reached")

    ctx = _Ctx()
    cap = readers.CapturingMarketConditionReader.__new__(readers.CapturingMarketConditionReader)
    cap.inner, cap._capture, cap._source_profile, cap._timing_policy = _Inner(), ctx, "p", "t"
    import threading
    cap._lock, cap._recorded = threading.Lock(), set()
    cap.record("AAA", date(2026, 11, 4))      # must not raise
    assert ctx.failures
