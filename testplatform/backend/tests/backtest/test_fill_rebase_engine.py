"""The entry's stop/target are re-based to the REAL fill in the backtest, as live does.

Live (TradeManager, when the parent entry reaches FILLED) re-scales the stop to
``fill * stop / reference`` and floors the take-profit against the fill; both are the ONE pure
function ``ba2_common.core.tpsl_fill_rebase.rebase_levels_at_fill``.  The backtest calls the same
function from ``BacktestAccount._apply_fill`` for an entry order, so the stop that is TESTED on the
following bars is the re-based one.

Fixture: the decision bar closes at 100; the next bar OPENS at 103 (long) / 97 (short) -- a 3% gap
between the price the levels were built from (the decision price) and the fill.  The bracket is the
usual stop -8% / target +10% (mirrored for a short).

    python -m pytest tests/backtest/test_fill_rebase_engine.py -q
"""
from __future__ import annotations

from datetime import date

import pytest

from tests.backtest.test_max_loss_stop_engine import _store_mode
from tests.backtest.test_short_selling_engine import BUY, DAY1, SELL, _run

# Long: fills 3% ABOVE the decision close.  Stop 92 -> 103 * 0.92 = 94.76.  Bar 3 trades down to 94:
# below 94.76, above the un-rebased 92 -- only the re-based stop is hit.
LONG_GAP_UP = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 103, 104, 102, 103),
    (date(2024, 1, 4), 103, 103.5, 94, 95),
    (date(2024, 1, 5), 95, 96, 94.5, 95),
]
# Long: fills 3% BELOW the decision close.  Stop 92 -> 97 * 0.92 = 89.24.  Bar 3 trades down to 91:
# below the un-rebased 92 (it would stop out), above the re-based 89.24 (it survives).
LONG_GAP_DOWN = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 97, 98, 96, 97),
    (date(2024, 1, 4), 97, 97.5, 91, 92),
    (date(2024, 1, 5), 92, 93, 91.5, 92),
]
# Short: fills 3% BELOW the decision close.  Stop 108 -> 97 * 1.08 = 104.76.  Bar 3 trades up to 105.
SHORT_GAP_DOWN = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 97, 98, 96, 97),
    (date(2024, 1, 4), 97, 105, 96.5, 104),
    (date(2024, 1, 5), 104, 104.5, 103, 104),
]
# Short: fills 3% ABOVE.  Stop 108 -> 103 * 1.08 = 111.24.  Bar 3 trades up to 109: above the
# un-rebased 108 (it would stop out), below the re-based 111.24.
SHORT_GAP_UP = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 103, 104, 102, 103),
    (date(2024, 1, 4), 103, 109, 102.5, 108),
    (date(2024, 1, 5), 108, 108.5, 107, 108),
]


def _go(monkeypatch, bars, signal, inmem="1", run_id=1200):
    _store_mode(monkeypatch, inmem)
    return _run(bars, {DAY1: signal}, run_id=run_id + (100 if inmem == "0" else 0), inmem=inmem,
                enable_short=(signal == SELL))


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
@pytest.mark.parametrize("bars,signal,stop,stopped", [
    (LONG_GAP_UP, BUY, 94.76, True),
    (LONG_GAP_DOWN, BUY, 89.24, False),
    (SHORT_GAP_DOWN, SELL, 104.76, True),
    (SHORT_GAP_UP, SELL, 111.24, False),
], ids=["long-fill-above", "long-fill-below", "short-fill-below", "short-fill-above"])
def test_the_stop_is_rebased_to_the_fill_and_that_is_the_stop_tested(
        monkeypatch, inmem, bars, signal, stop, stopped):
    outcome, [txn] = _go(monkeypatch, bars, signal, inmem, run_id=1200 + 10 * len(bars) + int(stop) % 7)
    assert txn["stop_loss"] == pytest.approx(stop)
    trades = [t for t in outcome["trades"] if t["exit_reason"] == "stop_loss"]
    if stopped:
        [t] = trades
        assert t["exit_price"] == pytest.approx(stop)
    else:
        assert trades == [], "the un-rebased stop would have fired; the re-based one must not"


def test_the_take_profit_stays_when_it_clears_the_floor(monkeypatch):
    _, [txn] = _go(monkeypatch, LONG_GAP_UP, BUY, run_id=1301)
    assert txn["take_profit"] == pytest.approx(110.0)
    _, [txn] = _go(monkeypatch, SHORT_GAP_DOWN, SELL, run_id=1302)
    assert txn["take_profit"] == pytest.approx(90.0)


def test_the_take_profit_floor_is_measured_from_the_fill(monkeypatch):
    """TP +4% of the decision close, fill 3% above it: only +0.97% of the fill, under the 2%
    floor -> raised to fill * 1.02.  (Live: TradeManager's post-fill floor re-check.)"""
    from tests.backtest.test_short_selling_engine import _adjust
    _store_mode(monkeypatch, "1")
    outcome, [txn] = _run(LONG_GAP_UP, {DAY1: BUY}, run_id=1303, inmem="1", enable_short=False,
                          entry_actions=[_adjust("adjust_stop_loss", -8.0),
                                         _adjust("adjust_take_profit", 4.0)])
    assert txn["take_profit"] == pytest.approx(103.0 * 1.02)
    assert txn["stop_loss"] == pytest.approx(94.76)


def test_a_fill_at_the_decision_price_changes_nothing(monkeypatch):
    flat = [(date(2024, 1, 2), 100, 101, 99, 100),
            (date(2024, 1, 3), 100, 101, 99, 100),
            (date(2024, 1, 4), 100, 101, 99, 100)]
    _, [txn] = _go(monkeypatch, flat, BUY, run_id=1304)
    assert txn["stop_loss"] == pytest.approx(92.0) and txn["take_profit"] == pytest.approx(110.0)


def test_the_record_keeps_the_pre_fill_levels(monkeypatch):
    """The pre-fill and enforced levels are both on the transaction's meta_data."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from tests.backtest import test_short_selling_engine as ssm
    seen = {}
    real = ssm.get_instance if hasattr(ssm, "get_instance") else None  # noqa: F841
    _store_mode(monkeypatch, "1")
    from app.services.backtest.backtest_account import BacktestAccount
    orig = BacktestAccount._rebase_levels_at_entry_fill

    def spy(self, order, fill_px):
        orig(self, order, fill_px)
        seen["meta"] = (get_instance(Transaction, order.transaction_id).meta_data or {}).get("fill_rebase")

    monkeypatch.setattr(BacktestAccount, "_rebase_levels_at_entry_fill", spy)
    _go(monkeypatch, LONG_GAP_UP, BUY, run_id=1305)
    rec = seen["meta"]
    assert rec["reference_price"] == pytest.approx(100.0) and rec["fill_price"] == pytest.approx(103.0)
    assert rec["stop_loss_pre_fill"] == pytest.approx(92.0) and rec["stop_loss"] == pytest.approx(94.76)
    assert rec["take_profit_pre_fill"] == pytest.approx(110.0)
    assert rec["stop_rebased"] is True and rec["tp_floored"] is False
