"""TP/SL direction from the POSITION, in the real backtest engine.

A long is entered on a BUY bar; on a later SELL bar the open-positions rule (has_position AND
bearish -> ``adjust_stop_loss -5`` off ``current_price``) must tighten the stop to 5% UNDER that
bar's close.
Before the fix the SELL recommendation made the action price the long as a short (5% ABOVE the
market); the SL min-distance floor then pulled that to 3% under the market (106.70), a tighter
stop no rule asked for. Both trade-store modes.

Run from the backend dir:
    python -m pytest tests/backtest/test_tp_sl_direction_engine.py -v
"""
from __future__ import annotations

from datetime import date

import pytest

from tests.backtest.test_max_loss_stop_engine import _store_mode
from tests.backtest.test_short_selling_engine import BUY, SELL, _run, _run_id

# Entry signal 2024-01-02 (close 100), fill at the next open. The SELL bar is 2024-01-04 (close
# 110): the stop moves to 110 * 0.95 = 104.5, and no later low reaches it.
BARS = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 106, 100, 105),
    (date(2024, 1, 4), 105, 111, 104.9, 110),
    (date(2024, 1, 5), 110, 112, 108, 111),
    (date(2024, 1, 8), 111, 113, 109, 112),
]

TIGHTEN_ON_HELD = [{
    "id": "trail", "name": "trail",
    "conditions": {"type": "AND", "conditions": [
        {"id": "held", "field": "has_position", "op": "is_true"},
        # only on the SELL bar, so the stop it sets is the one the run ends with
        {"id": "bear", "field": "bearish", "op": "is_true"}]},
    "actions": [{"id": "sl", "action_type": "adjust_stop_loss",
                 "reference_value": "current_price", "action_value": -5.0}],
    "continue_processing": False,
}]


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_sell_bar_on_a_held_long_keeps_the_stop_below_the_market(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    outcome, txns = _run(BARS, {date(2024, 1, 2): BUY, date(2024, 1, 4): SELL},
                         run_id=_run_id(870, inmem), inmem=inmem, enable_short=False,
                         entry_actions=[], exit_rules=TIGHTEN_ON_HELD)

    assert len(txns) == 1 and txns[0]["side"] == "BUY"
    txn = txns[0]
    assert txn["stop_loss"] == pytest.approx(110.0 * 0.95), "5% under the SELL bar's close"
    assert txn["stop_loss"] < 110.0
    assert txn["status"] == "OPENED", "the long must survive: its stop was never above the market"
    [trade] = outcome["trades"]
    assert trade["exit_reason"] == "open_at_end"
