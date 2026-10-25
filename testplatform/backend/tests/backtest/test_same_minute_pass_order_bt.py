"""goal2027 2.4 (owner decision: ENTRY FIRST is KEPT), BACKTEST side: when the entry and the manage schedule
fire on the same bar, the ENTRY pass runs before the MANAGE pass. Live twin:
tests/test_same_minute_pass_order_live.py. The GA can still put exits before entries by choosing
manage_time earlier than entry_time."""
import pytest

from tests.backtest.test_decision_time_gene import _engine_run

_NAME = {"_run_expert_bar": "enter_market", "_manage_open_positions": "open_positions"}


@pytest.fixture
def events(monkeypatch):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    log = []
    for attr, name in _NAME.items():
        orig = getattr(DailyBacktestEngine, attr)

        def spy(self, *a, _orig=orig, _name=name, **k):
            log.append((_name, a[-1]))
            return _orig(self, *a, **k)
        monkeypatch.setattr(DailyBacktestEngine, attr, spy)
    return log


def test_shared_minute_runs_entry_then_manage(events):
    _engine_run(["10:00"], ["10:00"], run_id=910)
    by_bar = {}
    for name, as_of in events:
        by_bar.setdefault(as_of, []).append(name)
    both = [order for order in by_bar.values() if len(order) == 2]
    assert both, f"no bar ran both passes: {by_bar}"
    assert all(order == ["enter_market", "open_positions"] for order in both), by_bar
