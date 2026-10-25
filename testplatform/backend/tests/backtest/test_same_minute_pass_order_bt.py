"""goal2027 2.4 parity, BACKTEST side: when the entry and the manage schedule fire on the same bar, the
MANAGE pass runs before the ENTRY pass (ba2_common SAME_MINUTE_PASS_ORDER). Live twin:
tests/test_same_minute_pass_order_live.py."""
import pytest

from ba2_common.core.schedule_genes import SAME_MINUTE_PASS_ORDER
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


def test_shared_minute_runs_manage_then_entry(events):
    _engine_run(["10:00"], ["10:00"], run_id=910)
    assert events, "no pass ran"
    by_bar = {}
    for name, as_of in events:
        by_bar.setdefault(as_of, []).append(name)
    # Day 1 only enters (the skip-flat-bars shortcut never calls manage on a flat book); later days have an
    # open position, so both passes fire on the one bar.
    both = [order for order in by_bar.values() if len(order) == 2]
    assert both, f"no bar ran both passes: {by_bar}"
    assert all(order == list(SAME_MINUTE_PASS_ORDER) for order in both), by_bar
