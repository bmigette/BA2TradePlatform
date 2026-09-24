"""B3: the backtest engine records each equity entry's max-loss stop, and nothing else changes.

The max-loss stop is the stop the position was SIZED on: the RM safeguard. When the ruleset's own
entry stop is tighter, THAT is what protects the position (``reconcile_protective_stop``), but
the size was still keyed off the safeguard, so the recorded value is the safeguard. B4 lets a
rule loosen the stop back to it and no further.

  * ``_size_and_submit`` (the DB pass the open-positions tail uses): reuses the SL-precedence
    fixture of test_entry_bracket_engine.py.
  * ``_size_and_submit_candidates`` (every enter-market entry): a full ``engine.run()``, in both
    the in-memory trade store and the SQLite path, so the value is proven to survive the fill
    engine's later writes to the same transaction.
  * No-impact: the same run with the write patched out yields identical orders, trades, fills
    and equity.

Run from the backend dir:
    python -m pytest tests/backtest/test_max_loss_stop_engine.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.position_sizing import max_loss_stop_of
from ba2_common.core.types import OrderRecommendation, Recommendation

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from tests.backtest.test_entry_bracket_engine import _precedence_setup


# --------------------------------------------------------------------------- #
# _size_and_submit: safeguard $92 (8% of $100) vs a ruleset stop
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("account_id, ruleset_sl, submitted_sl", [
    (401, 97.0, 97.0),    # ruleset stop tighter: it is submitted, the safeguard was sized on
    (402, 85.0, 92.0),    # safeguard tighter: submitted AND sized on
    (403, None, 92.0),    # no ruleset stop: the safeguard alone
])
def test_size_and_submit_records_the_safeguard_as_max_loss_stop(account_id, ruleset_sl, submitted_sl):
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction

    engine, account, txn_id, ctx = _precedence_setup(
        account_id=account_id, expert_id=account_id, ruleset_sl_price=ruleset_sl)
    try:
        engine._size_and_submit(account_id, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert txn.stop_loss == pytest.approx(submitted_sl), "the submitted stop moved"
        assert max_loss_stop_of(txn) == pytest.approx(92.0)
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# engine.run(): the enter-market candidate path
# --------------------------------------------------------------------------- #

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

# Rising: the entry on 2024-01-02 (close 100) fills at the next open and is never stopped out.
RISING = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 112, 100, 110),
    (date(2024, 1, 4), 110, 122, 109, 120),
]

# Two stop-outs and re-entries, for the no-impact comparison.
CHOPPY = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 103, 99, 102),
    (date(2024, 1, 4), 101, 102, 88, 90),
    (date(2024, 1, 5), 90, 92, 89, 91),
    (date(2024, 1, 8), 92, 96, 91, 95),
    (date(2024, 1, 9), 95, 96, 80, 82),
    (date(2024, 1, 10), 82, 85, 81, 84),
    (date(2024, 1, 11), 84, 88, 83, 87),
]


class _MaxLossStubExpert(MarketExpertInterface):
    """BUY on every bar in ``buy_on`` (all bars when None), HOLD otherwise. No providers."""

    def __init__(self, id: int, price_source, buy_on=None):
        super().__init__(id)
        self._ps = price_source
        self._buy_on = buy_on

    @classmethod
    def description(cls) -> str:
        return "Stub expert for the max-loss stop engine tests."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        close = float(self._ps.close_at("AAPL", as_of))
        day = as_of.date() if hasattr(as_of, "date") else as_of
        buy = self._buy_on is None or day in self._buy_on
        return Recommendation(
            signal=OrderRecommendation.BUY if buy else OrderRecommendation.HOLD,
            confidence=80.0 if buy else 50.0,
            current_price=close,
            details="stub",
            expected_profit_percent=10.0 if buy else 0.0,
        )


def _run(bars, *, run_id, entry_sl_pct=None, buy_on=None):
    """Full engine.run() over ``bars`` with deterministic risk_atr sizing (ATR off, 8% risk and
    8% floor -> the safeguard is exactly 8% under the signal close). ``entry_sl_pct`` adds a
    ruleset entry stop (``adjust_stop_loss`` off ``order_open_price``). Returns the account's
    outcome and every entry transaction's (stop_loss, max_loss_stop), read BEFORE teardown."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset, seed_ruleset_from_tree
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"max-loss-stop-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        if entry_sl_pct is None:
            ruleset_id = seed_enter_long_ruleset()
        else:
            ruleset_id = seed_ruleset_from_tree(None, entry_actions=[
                {"id": "e_sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": entry_sl_pct}])
        seed_expert_instance(account_id=account_id, expert_class_name="_MaxLossStubExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in bars])
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = _MaxLossStubExpert(expert_id, ps, buy_on=buy_on)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        })
        resolver.register_expert(expert_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, ruleset_id)], price_source=ps,
            config={"start_date": datetime.combine(bars[0][0], datetime.min.time()),
                    "end_date": datetime.combine(bars[-1][0], datetime.min.time()),
                    "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()

        entries = [o for o in account.get_orders()
                   if o.symbol == "AAPL" and o.side == OrderDirection.BUY and o.depends_on_order is None]
        stops = []
        for o in sorted(entries, key=lambda o: o.id):
            txn = get_instance(Transaction, o.transaction_id)
            stops.append((txn.stop_loss, max_loss_stop_of(txn)))
        orders = sorted(
            (o.side.value, o.order_type.value, o.status.value, o.quantity, o.filled_qty,
             o.open_price, o.stop_price, o.limit_price, o.depends_on_order is None)
            for o in account.get_orders())
        outcome = {
            "orders": orders,
            "trades": account.get_round_trip_trades(),
            "equity": list(account.get_balance_history()),
        }
        return outcome, stops
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
@pytest.mark.parametrize("entry_sl_pct, submitted_sl", [
    (None, 92.0),    # no ruleset stop: the safeguard is submitted and recorded
    (-3.0, 97.0),    # tighter ruleset stop is submitted; the safeguard is still what was sized on
    (-15.0, 92.0),   # looser ruleset stop: the safeguard wins the reconcile too
])
def test_engine_entry_records_the_sized_on_stop(monkeypatch, inmem, entry_sl_pct, submitted_sl):
    monkeypatch.setenv("BT_INMEM_TRADES", inmem)
    run_id = 410 + (0 if entry_sl_pct is None else int(-entry_sl_pct)) + (100 if inmem == "0" else 0)
    _, stops = _run(RISING, run_id=run_id, entry_sl_pct=entry_sl_pct, buy_on={date(2024, 1, 2)})
    assert len(stops) == 1, "exactly one entry expected"
    stop_loss, max_loss = stops[0]
    assert stop_loss == pytest.approx(submitted_sl), "the protective stop the engine attached moved"
    assert max_loss == pytest.approx(92.0), "the recorded max-loss stop is not the sized-on safeguard"


@pytest.mark.parametrize("entry_sl_pct", [None, -3.0], ids=["safeguard-only", "ruleset-tighter"])
def test_recording_the_max_loss_stop_changes_no_trade(monkeypatch, entry_sl_pct):
    """The same fixed run with and without the write: identical orders, fills, trades, equity."""
    from app.services.backtest import daily_engine

    run_id = 440 if entry_sl_pct is None else 450
    with_write, stops = _run(CHOPPY, run_id=run_id, entry_sl_pct=entry_sl_pct)
    assert len(stops) >= 2, "the fixture must re-enter after a stop-out to be a real comparison"
    assert all(m is not None for _, m in stops), "every entry should carry a max-loss stop"
    assert len(with_write["trades"]) >= 2

    monkeypatch.setattr(daily_engine, "record_max_loss_stop", lambda *a, **k: None)
    without_write, stops_off = _run(CHOPPY, run_id=run_id + 1, entry_sl_pct=entry_sl_pct)
    assert all(m is None for _, m in stops_off), "the patch did not disable the write"

    assert with_write["orders"] == without_write["orders"]
    assert with_write["trades"] == without_write["trades"]
    assert with_write["equity"] == without_write["equity"]
