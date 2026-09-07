"""The entry pass must release the parked OPEN_POSITIONS pass AFTER dropping the trigger lock.

WHAT HAPPENED (PROD, 2026-09-07 15:32:51). ``_check_and_process_expert_recommendations`` holds
``_risk_manager_lock`` -- a plain, non-reentrant ``threading.Lock`` -- for its whole processing
block. e5e37c6a (09-06 21:11) added a call to ``release_deferred_open_positions`` INSIDE that
block, and the first line of that function is ``with self._risk_manager_lock``. The thread that
had just created expert 12's two orders then waited on itself, forever. Expert 7's trigger
queued behind it; so did all eight of expert 10's, whose four actionable recommendations were
never evaluated and never traded. Nothing moved until the 21:54 restart, and the next
enter-market pass would have done it again.

Why the existing tests missed it: ``tests/test_open_positions_waits_for_entry.py`` calls the
release from an unlocked thread. The bug lives at the CALL SITE, inside the lock -- so these
tests drive the real trigger method all the way to that site.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.WorkerQueue import WorkerQueue
from ba2_trade_platform.core.types import AnalysisUseCase


EXPERT = 42
BATCH = "42_0930_20260907"


def _drive_to_the_release(monkeypatch, q: WorkerQueue):
    """Everything the trigger needs to reach its post-processing hook, and nothing more.

    Every gate on the way is satisfied with the SMALLEST fake that passes it: an expert that
    uses the risk manager, in classic mode, with an enter_market ruleset, whose trade manager
    creates no orders. The fakes are patched onto the modules the trigger imports from at
    call time (``from .utils import ...`` inside the function), which is why the dotted
    targets are the module attributes rather than the names in this file.
    """
    expert = SimpleNamespace(settings={"risk_manager_mode": "classic"})
    record = SimpleNamespace(id=EXPERT, account_id=1, enter_market_ruleset_id=7,
                             open_positions_ruleset_id=8)
    trade_manager = SimpleNamespace(
        process_expert_recommendations_after_analysis=lambda expert_id, **kw: [],
        process_open_positions_recommendations=lambda expert_id, **kw: [],
    )
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_expert_instance_from_id",
                        lambda expert_id: expert)
    monkeypatch.setattr("ba2_trade_platform.core.utils.expert_uses_risk_manager",
                        lambda cls: True)
    monkeypatch.setattr("ba2_trade_platform.core.utils.get_risk_manager_mode",
                        lambda settings: "classic")
    monkeypatch.setattr("ba2_trade_platform.core.db.get_instance",
                        lambda model, ident: record)
    monkeypatch.setattr("ba2_trade_platform.core.TradeManager.get_trade_manager",
                        lambda: trade_manager)

    # Something parked, so the release has work to do -- and a recorder instead of a real
    # submission, exactly as the deferral tests do.
    q._deferred_open_positions[EXPERT] = {"batch_id": BATCH, "registered_at": time.time()}
    submitted = []

    def fake_submit(expert_instance_id, expansion_type, **kw):
        submitted.append((expert_instance_id, expansion_type, kw.get("subtype"), kw.get("batch_id")))
        return f"task-{len(submitted)}"
    monkeypatch.setattr(q, "submit_instrument_expansion_task", fake_submit)
    return submitted


def test_the_entry_pass_RETURNS_and_releases_the_parked_exit_pass(monkeypatch):
    """The deadlock itself, with a timeout. Before the fix this thread never comes back."""
    q = WorkerQueue()                       # inert: threads only start on .start()
    submitted = _drive_to_the_release(monkeypatch, q)

    worker = threading.Thread(
        target=q._check_and_process_expert_recommendations,
        args=(EXPERT, AnalysisUseCase.ENTER_MARKET), daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "the entry pass never returned: it is waiting on its own lock"
    assert submitted == [(EXPERT, "OPEN_POSITIONS", AnalysisUseCase.OPEN_POSITIONS, BATCH)]
    assert not q._risk_manager_lock.locked()
    assert EXPERT not in q._deferred_open_positions


def test_the_release_is_called_with_the_lock_ALREADY_DROPPED(monkeypatch):
    """The invariant, stated directly: at the moment of the release, nobody holds the lock.

    Sharper than the timeout test -- it names the mistake rather than its symptom -- and
    it fails in milliseconds instead of five seconds.
    """
    q = WorkerQueue()
    _drive_to_the_release(monkeypatch, q)
    held_at_release = []

    def probe(expert_instance_id, reason):
        held_at_release.append(q._risk_manager_lock.locked())
        return None
    monkeypatch.setattr(q, "release_deferred_open_positions", probe)

    q._check_and_process_expert_recommendations(EXPERT, AnalysisUseCase.ENTER_MARKET)

    assert held_at_release == [False]


def test_the_release_also_happens_after_the_expert_left_the_processing_set(monkeypatch):
    """Order matters for the parking predicate: it reads ``_processing_experts`` to decide
    whether an entry pass is in flight. A release that ran before the ``finally`` cleared
    the key would let a freshly submitted exit pass park itself behind a pass that had
    already finished."""
    q = WorkerQueue()
    _drive_to_the_release(monkeypatch, q)
    seen = []

    def probe(expert_instance_id, reason):
        seen.append(f"expert_{EXPERT}_{AnalysisUseCase.ENTER_MARKET.value}" in q._processing_experts)
        return None
    monkeypatch.setattr(q, "release_deferred_open_positions", probe)

    q._check_and_process_expert_recommendations(EXPERT, AnalysisUseCase.ENTER_MARKET)

    assert seen == [False]


def test_an_OPEN_POSITIONS_pass_never_triggers_a_release(monkeypatch):
    """Only the entry pass un-parks the exit pass. The exit pass releasing itself would
    resubmit its own expansion."""
    q = WorkerQueue()
    submitted = _drive_to_the_release(monkeypatch, q)
    called = []
    monkeypatch.setattr(q, "release_deferred_open_positions",
                        lambda *a, **k: called.append(a))

    q._check_and_process_expert_recommendations(EXPERT, AnalysisUseCase.OPEN_POSITIONS)

    assert called == []
    assert submitted == []
