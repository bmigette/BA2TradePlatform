"""Write support: broker error mapping for writes and the per-expert processing lock."""
import threading

import pytest

from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.services import _locks
from ba2_trade_platform.services.accounts import broker_write
from ba2_trade_platform.services.errors import BrokerRefused, BrokerUnreachable, Conflict

pytestmark = pytest.mark.api_db


class APIError(Exception):          # matched by class name, like the real SDK errors
    pass


def test_broker_write_maps_failures_to_class_names_only():
    def boom(exc):
        raise exc
    with pytest.raises(BrokerUnreachable) as e:
        broker_write(1, "the order", boom, TimeoutError("secret-host:443"))
    assert "TimeoutError" in e.value.message and "secret-host" not in e.value.message
    with pytest.raises(BrokerRefused) as e:
        broker_write(1, "the order", boom, APIError('{"code": 40310000, "message": "wash trade"}'))
    assert "APIError" in e.value.message and "wash trade" not in e.value.message
    with pytest.raises(BrokerRefused):
        broker_write(1, "the order", boom, ValueError("qty too small"))
    with pytest.raises(KeyError):                      # a defect is not disguised
        broker_write(1, "the order", boom, KeyError("x"))
    assert broker_write(1, "the order", lambda a: a + 1, 1) == 2


def test_hold_expert_locks_takes_both_and_releases():
    tm = TradeManager()
    with tm.hold_expert_locks(7, 1.0) as held:
        assert held is True
        for use_case in ("enter_market", "open_positions"):
            assert tm._processing_lock(7, use_case).locked()
    for use_case in ("enter_market", "open_positions"):
        assert not tm._processing_lock(7, use_case).locked()


def test_hold_expert_locks_times_out_without_holding_anything():
    tm = TradeManager()
    pass_lock = tm._processing_lock(7, "open_positions")        # a running exit pass
    pass_lock.acquire()
    try:
        with tm.hold_expert_locks(7, 0.2) as held:
            assert held is False
        assert not tm._processing_lock(7, "enter_market").locked()   # the first lock was handed back
    finally:
        pass_lock.release()


def test_expert_write_lock_conflicts_while_a_pass_runs(monkeypatch):
    tm = TradeManager()
    monkeypatch.setattr(_locks, "get_trade_manager", lambda: tm)
    with _locks.expert_write_lock(None):          # no expert: no lock
        pass
    lock = tm._processing_lock(3, "enter_market")
    lock.acquire()
    try:
        with pytest.raises(Conflict):
            with _locks.expert_write_lock(3, timeout=0.2):
                pytest.fail("must not run while a pass holds the lock")
    finally:
        lock.release()
    with _locks.expert_write_lock(3, timeout=0.2):
        pass
