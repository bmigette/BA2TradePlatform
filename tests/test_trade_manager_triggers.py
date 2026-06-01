"""
Tests for the consolidated WAITING_TRIGGER handling in TradeManager.

Covers:
- classify_waiting_trigger(): the pure trigger-decision used by the single surviving
  WAITING_TRIGGER method (M1 consolidation), including the explicit PARTIALLY_FILLED
  no-op (H2).
- The duplicate method was removed (M1).
- refresh_accounts is guarded by a non-blocking reentrancy lock (H1).
"""
import pytest

from ba2_trade_platform.core import TradeManager as tm_mod
from ba2_trade_platform.core.TradeManager import TradeManager, classify_waiting_trigger
from ba2_trade_platform.core.types import OrderStatus


# ---------------------------------------------------------------------------
# classify_waiting_trigger (M1 + H2)
# ---------------------------------------------------------------------------

class TestClassifyWaitingTrigger:
    def test_parent_at_trigger_submits(self):
        assert classify_waiting_trigger(OrderStatus.FILLED, OrderStatus.FILLED) == "submit"

    def test_partial_fill_waits(self):
        """H2: a partially-filled parent keeps the dependent waiting (no cancel)."""
        assert classify_waiting_trigger(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED) == "wait"

    def test_non_trigger_terminal_cancels(self):
        assert classify_waiting_trigger(OrderStatus.EXPIRED, OrderStatus.FILLED) == "cancel"
        assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.FILLED) == "cancel"

    def test_active_non_terminal_waits(self):
        assert classify_waiting_trigger(OrderStatus.ACCEPTED, OrderStatus.FILLED) == "wait"
        assert classify_waiting_trigger(OrderStatus.NEW, OrderStatus.FILLED) == "wait"

    def test_no_trigger_status_waits(self):
        assert classify_waiting_trigger(OrderStatus.FILLED, None) == "wait"

    def test_cancel_trigger_matches_before_terminal(self):
        """A dependent that waits for CANCELED must SUBMIT (not cancel) when parent is CANCELED."""
        assert classify_waiting_trigger(OrderStatus.CANCELED, OrderStatus.CANCELED) == "submit"


# ---------------------------------------------------------------------------
# M1: duplicate method removed
# ---------------------------------------------------------------------------

class TestDuplicateMethodRemoved:
    def test_old_duplicate_method_gone(self):
        assert not hasattr(TradeManager, "_check_order_status_changes_and_trigger_dependents")

    def test_surviving_method_present(self):
        assert hasattr(TradeManager, "_check_all_waiting_trigger_orders")


# ---------------------------------------------------------------------------
# H1: refresh_accounts reentrancy lock
# ---------------------------------------------------------------------------

class TestRefreshReentrancyLock:
    def test_refresh_skips_when_lock_held(self, monkeypatch):
        """If a refresh is already running (lock held), a second call returns immediately
        without doing any work."""
        calls = {"n": 0}

        def _counting_get_all_instances(*args, **kwargs):
            calls["n"] += 1
            return []

        monkeypatch.setattr(tm_mod, "get_all_instances", _counting_get_all_instances)

        tm = TradeManager()
        acquired = tm_mod._REFRESH_LOCK.acquire(blocking=False)
        assert acquired
        try:
            tm.refresh_accounts()  # should bail out at the lock
        finally:
            tm_mod._REFRESH_LOCK.release()

        assert calls["n"] == 0, "refresh_accounts did work despite the lock being held"

    def test_refresh_runs_when_lock_free(self, monkeypatch):
        """Sanity: when the lock is free, refresh_accounts proceeds past the guard."""
        calls = {"n": 0}

        def _counting_get_all_instances(*args, **kwargs):
            calls["n"] += 1
            return []

        monkeypatch.setattr(tm_mod, "get_all_instances", _counting_get_all_instances)

        tm = TradeManager()
        tm.refresh_accounts()
        assert calls["n"] >= 1
