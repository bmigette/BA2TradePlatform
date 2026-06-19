"""Phase 2 Task 4: daily engine loop mechanics (isolated from real experts/providers).

A deterministic STUB expert (no providers, no network) returns a single BUY ``Recommendation``
for "AAPL" on day 1 and HOLD afterwards. The engine drives the FULL packaged order path
against that stub:

    analyze_as_of -> ExpertRecommendation row -> TradeActionEvaluator (seeded enter ruleset)
    -> TradeRiskManagement classic RM (notional sizing) -> BacktestAccount.submit_order
    -> next-bar fill -> snapshot_equity.

Asserts: (a) exactly one position opens, (b) get_balance_history has one snapshot per bar,
(c) the final equity reflects the AAPL move, (d) the virtual clock advanced each bar (price
cache busted — the time machine returns the bar's own close, never a stale prior value).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_engine_unit.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation
from ba2_common.core.types import Recommendation


# 5 daily bars; AAPL rises 100 -> 140 close over the window.
BARS = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 112, 100, 110),
    (date(2024, 1, 4), 110, 122, 109, 120),
    (date(2024, 1, 5), 120, 132, 119, 130),
    (date(2024, 1, 8), 130, 142, 129, 140),
]
START = datetime(2024, 1, 2)
END = datetime(2024, 1, 8)


def _bar_rows(rows):
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


class _StubExpert(MarketExpertInterface):
    """Deterministic expert: BUY AAPL on the FIRST bar, HOLD afterwards.

    Overrides ``analyze_as_of`` directly (the engine's only decision call), so no providers
    are touched. The base ``_gather``/``_process`` are never invoked. Records every as_of it
    was asked about so the test can assert the clock advanced once per bar.
    """

    def __init__(self, id: int, buy_on: date, price_source):
        super().__init__(id)
        self._buy_on = buy_on
        self._ps = price_source
        self.seen_as_of: list = []

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub backtest expert (deterministic BUY on a fixed day)."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.seen_as_of.append(as_of.date() if hasattr(as_of, "date") else as_of)
        # current_price is the as_of close (Decision 5: one price source).
        close = self._ps.close_at("AAPL", as_of)
        if (as_of.date() if hasattr(as_of, "date") else as_of) == self._buy_on:
            return Recommendation(
                signal=OrderRecommendation.BUY,
                confidence=80.0,
                current_price=float(close),
                details="stub buy",
                expected_profit_percent=10.0,
            )
        return Recommendation(
            signal=OrderRecommendation.HOLD,
            confidence=50.0,
            current_price=float(close),
            details="stub hold",
            expected_profit_percent=0.0,
        )


def _build_run(account_id=1, expert_id=1, cfg=None):
    """Wire a full backtest fixture: seams, backtest DB, account, ruleset, stub expert.

    Returns (engine, account, expert, db_ctx, price_source). Caller MUST close db_ctx.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.daily_engine import DailyBacktestEngine

    cfg = cfg or {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"engine-unit-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    ruleset_id = seed_enter_long_ruleset()
    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_StubExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    expert = _StubExpert(expert_id, buy_on=date(2024, 1, 2), price_source=ps)
    # Enable automated opening + buy (interface defaults are False/True; RM gates on these).
    expert.save_settings(
        {
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
        }
    )
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=None,  # notional sizing -> ATR not needed
    )
    return engine, account, expert, ctx, ps


def test_engine_opens_one_position_and_records_equity():
    from ba2_common.core.types import OrderStatus

    engine, account, expert, ctx, ps = _build_run()
    try:
        # Avoid a real ATR/indicator provider build (notional sizing never uses it).
        engine._indicator_provider = object()
        results = engine.run()

        # (b) one equity snapshot per simulated bar.
        hist = account.get_balance_history()
        assert len(hist) == len(BARS)
        assert len(results["equity_history"]) == len(BARS)

        # (a) exactly one position opened (AAPL long), filled at day-2 open (100).
        positions = account.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["qty"] > 0

        # One filled BUY order exists.
        filled = [o for o in account.get_orders() if o.status == OrderStatus.FILLED]
        assert len(filled) == 1
        assert filled[0].symbol == "AAPL"

        # (c) final equity reflects the AAPL rise (bought ~day-2 open 100, marked at 140 close).
        qty = positions[0]["qty"]
        commission = engine.config.get("commission", 0.0)
        # final equity = cash + qty*last_close; cash = 100k - qty*entry_fill - commissions.
        entry_fill = filled[0].open_price
        expected_final = (100_000.0 - qty * entry_fill) + qty * 140.0
        assert account.equity() == pytest.approx(expected_final)
        assert account.equity() > 100_000.0  # the position made money

        # (d) the clock advanced once per bar (decision called per bar, not stale).
        assert expert.seen_as_of == [d for (d, *_rest) in BARS]
    finally:
        ctx.__exit__(None, None, None)


def test_engine_busts_price_cache_each_bar():
    """The time machine returns the bar's OWN close every bar (no stale-cache leak)."""
    engine, account, expert, ctx, ps = _build_run(account_id=2, expert_id=2)
    try:
        seen_prices = []
        orig = ps.set_clock

        def _spy(as_of):
            orig(as_of)
            seen_prices.append(account.get_instrument_current_price("AAPL"))

        ps.set_clock = _spy  # type: ignore[assignment]
        engine._indicator_provider = object()
        engine.run()

        # The per-bar price equals each bar's close, strictly increasing across the window.
        assert seen_prices == [c for (_d, _o, _h, _l, c) in BARS]
        assert seen_prices == sorted(seen_prices)
        assert len(set(seen_prices)) == len(BARS)  # all distinct -> never stale
    finally:
        ctx.__exit__(None, None, None)


def test_recommendation_to_expert_recommendation_skips_hold_and_skip():
    """SKIP and HOLD recommendations are not persisted as actionable rows."""
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.backtest_db import seed_expert_instance
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db("engine-unit-rec")
    ctx.__enter__()
    try:
        seed_account_definition(7, {})
        rid = seed_enter_long_ruleset()
        seed_expert_instance(
            account_id=7, expert_class_name="_StubExpert",
            enter_market_ruleset_id=rid, instance_id=7,
        )
        now = datetime(2024, 1, 2)

        hold = Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                              current_price=100.0, expected_profit_percent=0.0)
        skip = Recommendation(signal=OrderRecommendation.BUY, confidence=50.0,
                              current_price=100.0, skip=True, skip_reason="no data")
        buy = Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                             current_price=100.0, expected_profit_percent=5.0, details="d")

        assert _recommendation_to_expert_recommendation(
            hold, expert_instance_id=7, symbol="AAPL", as_of=now) is None
        assert _recommendation_to_expert_recommendation(
            skip, expert_instance_id=7, symbol="AAPL", as_of=now) is None
        rec_id = _recommendation_to_expert_recommendation(
            buy, expert_instance_id=7, symbol="AAPL", as_of=now)
        assert isinstance(rec_id, int) and rec_id > 0
    finally:
        ctx.__exit__(None, None, None)
