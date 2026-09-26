"""TradingAgents gates automated trade opening on the DECLARED key only (review 2026-09-26).

``_notify_trade_manager`` also read an undeclared legacy ``automatic_trading`` row as permission
to auto-open -- and ``self.settings.get('automatic_trading', False)`` is a raw string for such a
row, so even ``"false"`` was truthy and switched automated opening ON. No such rows exist in the
prod or dev DB; the read is removed so only ``allow_automated_trade_opening`` gates it.
"""
import logging

import pytest

import ba2_trade_platform.core.db as core_db
from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents


def _agent(settings, allow_open):
    agent = TradingAgents.__new__(TradingAgents)
    agent.id = 4242
    agent._settings_cache = dict(settings)
    agent.__dict__["logger"] = logging.getLogger("test_tradingagents_auto_trade_gate")
    agent.get_setting_with_interface_default = (
        lambda key, log_warning=True: allow_open if key == "allow_automated_trade_opening"
        else pytest.fail(f"unexpected setting read: {key}"))
    return agent


@pytest.fixture
def recommendation_reads(monkeypatch):
    reads = []

    def _get_instance(model, rec_id):
        reads.append(rec_id)
        return None     # "not found": _notify_trade_manager stops right after the gate

    monkeypatch.setattr(core_db, "get_instance", _get_instance)
    return reads


@pytest.mark.parametrize("legacy", ["true", "false", True, "1"])
def test_a_legacy_automatic_trading_row_does_not_open_trades(recommendation_reads, legacy):
    _agent({"automatic_trading": legacy}, allow_open=False)._notify_trade_manager(1, "AAPL")
    assert recommendation_reads == [], "gate passed on the legacy automatic_trading row"


def test_the_declared_permission_still_opens(recommendation_reads):
    _agent({}, allow_open=True)._notify_trade_manager(7, "AAPL")
    assert recommendation_reads == [7]
