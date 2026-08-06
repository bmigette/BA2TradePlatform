"""Downside protection for BYPASS-expert (FactorRanker) positions in the daily engine.

A bypass expert (``bypasses_classic_rm = True``) sizes by weight and skips the classic risk
manager, so a held name needs protection that does not come from the RM. Until 2026-08-06 the
engine supplied it with a per-bar pass that called back into expert code to price every holding
and submit market sells. That mechanism existed only in backtest — live FactorRanker positions
had no stop at all — so it was replaced by the thing a broker actually does: FactorRanker attaches
a RESTING ``SELL_STOP`` when it opens the position (``FactorPortfolioManager._submit_buy`` ->
``protective_stop_price``), priced from the same equity-loss budget (``risk_per_trade_pct`` % of
equity). Live, Alpaca holds it; here, ``BacktestAccount.refresh_orders`` fills it on the bar whose
low touches the stop.

These tests are deliberately BEHAVIOURAL, not mechanical: they assert that a position which
breaches the equity-loss budget is exited and one that does not is still held, without caring how
the engine gets there. That is why they survived the rewrite unchanged.

Note the fill timing DID change: the stop now fills AT the stop price on the bar that touches it,
rather than as a market order on the bar after a close-based check. Backtest numbers for
FactorRanker before and after 2026-08-06 are not directly comparable.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_engine_stop.py -v
"""
from __future__ import annotations

from datetime import date, datetime

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


# Trading days span two weeks. Entry is gated to TUESDAYS only (see ENTRY_SCHEDULE), so the
# expert rebalances ONLY on the two Tuesdays; every other bar is a NON-rebalance bar where the
# stop pass runs. AAPL opens ~100 on Tue 2024-01-02 then sells off into the following week.
#                              (date,          open, high, low,  close)   weekday
# The window ENDS on the Monday stop bar (no following Tuesday) so the rebalance never re-buys
# AAPL after the stop -> "AAPL fully exited, none held at end" is a clean assertion.
BARS_CRASH = [
    (date(2024, 1, 2),  100, 101, 99,  100),   # Tue  -> entry bar (rebalance buys AAPL)
    (date(2024, 1, 3),  100, 100, 99,  100),   # Wed  -> buy fills at 100 open; flat
    (date(2024, 1, 4),  100, 100, 99,  100),   # Thu  -> flat (loss 0)
    (date(2024, 1, 5),  100, 100, 79,  80),    # Fri  -> NON-rebalance: open still 100, no stop yet
    (date(2024, 1, 8),  80,  81,  79,  80),    # Mon  -> NON-rebalance: price 80 (-20%) -> STOP fires
    (date(2024, 1, 9),  80,  81,  79,  80),    # Tue  -> stop SELL fills here (next_bar_open); end
]

# Same window, but the drawdown stays SHALLOW (never reaches the equity cap) -> no stop.
BARS_SHALLOW = [
    (date(2024, 1, 2),  100, 101, 99,  100),   # Tue  -> entry bar
    (date(2024, 1, 3),  100, 100, 99,  100),   # Wed  -> buy fills at 100
    (date(2024, 1, 4),  100, 100, 99,  100),   # Thu
    (date(2024, 1, 5),  95,  96,  94,  95),    # Fri  -> -5% only
    (date(2024, 1, 8),  95,  96,  94,  95),    # Mon  -> -5% open (loss << cap) -> NO stop
    (date(2024, 1, 9),  95,  96,  94,  95),    # Tue  -> rebalance bar
]

START = datetime(2024, 1, 2)
END = datetime(2024, 1, 9)

# Only TUESDAY is an entry/rebalance day; all other bars are non-rebalance (stop runs there).
ENTRY_SCHEDULE = {
    "days": {
        "monday": False, "tuesday": True, "wednesday": False, "thursday": False,
        "friday": False, "saturday": False, "sunday": False,
    },
    "times": [],
}

# Target a 5% AAPL weight: at equity ~100k -> ~$5000 -> ~50 shares @ $100. The 1% equity cap is
# $1000, so a 20% price drop (100 -> 80) loses 50*$20 = $1000 >= cap (stop), while a 5% drop
# (100 -> 95) loses ~$250 << cap (no stop). This is the intended "small weight needs a big move".
TARGET_WEIGHT = 0.05


def _bar_rows(rows):
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


class _StubBypassExpert(MarketExpertInterface):
    """Deterministic BYPASS expert: targets 5% AAPL on its (Tuesday) rebalance bars.

    Declares ``bypasses_classic_rm = True`` and returns a single basket-level Recommendation
    whose ``raw_outputs['targets']`` is the ``{symbol: weight}`` book. ``risk_per_trade_pct``
    is left to its interface default (1.0) via ``self.settings`` so the engine's stop pass reads
    the real builtin cap. ``execution_schedule_enter_market`` gates rebalances to Tuesdays.
    """

    bypasses_classic_rm = True

    def __init__(self, id: int, settings=None):
        super().__init__(id)
        # ``settings`` is a read-only DB-backed property; inject via the instance cache it
        # short-circuits on. Drive the entry cadence (Tuesdays) and leave risk_per_trade_pct
        # ABSENT so get_setting_with_interface_default falls back to its builtin default (1.0).
        self._settings_cache = settings if settings is not None else {
            "execution_schedule_enter_market": ENTRY_SCHEDULE,
        }
        self.seen_as_of: list = []

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub bypass expert for the per-bar equity-loss stop test."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.seen_as_of.append(as_of.date() if hasattr(as_of, "date") else as_of)
        return Recommendation(
            signal=OrderRecommendation.OVERWEIGHT,
            confidence=0.0,
            current_price=None,  # basket-level (cross-sectional), like FactorRanker
            details="stub bypass targets",
            raw_outputs={"targets": {"AAPL": TARGET_WEIGHT}, "book": {"universe_size": 1}},
        )


def _build_run(bars, account_id, expert_id, settings=None):
    """Wire a backtest fixture for the stub BYPASS expert with a Tuesday entry cadence.

    Returns (engine, account, expert, db_ctx, price_source). Caller MUST close db_ctx.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    cfg = {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"engine-stop-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    ruleset_id = seed_enter_long_ruleset(name=f"backtest-stop-stub-{account_id}")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_StubBypassExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(bars))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    expert = _StubBypassExpert(expert_id, settings=settings)
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, None)],
        price_source=ps,
        config=config,
        indicator_provider=object(),
    )
    return engine, account, expert, ctx, ps


def test_e2e_bypass_stop_exits_breached_name():
    """END-TO-END: a held bypass position that loses >= risk_per_trade_pct% of equity on a
    NON-rebalance bar is fully exited by the per-bar stop (a real SELL fills and the position
    is gone). The naive price-stop would have churned; the equity-loss stop only fires on the
    ~20% drop the 5%-weight name needs."""
    engine, account, expert, ctx, ps = _build_run(BARS_CRASH, account_id=61, expert_id=61)
    try:
        engine.run()

        # The position was OPENED (rebalance bought ~50 AAPL) and then EXITED by the stop:
        # no open AAPL position survives the crash (the ledger net qty is back to flat).
        positions = account.get_positions()
        aapl = [p for p in positions if p["symbol"] == "AAPL" and p.get("qty", 0) > 0]
        assert aapl == [], f"expected AAPL exited by the stop, still held: {aapl}"

        # The exit was a real SELL FILL (not a cancel / a no-op).
        sell_fills = [
            t for t in account.get_filled_trades(symbol="AAPL")
            if str(t.get("direction") or t.get("side") or "").lower() == "sell"
        ]
        assert sell_fills, f"expected a filled stop SELL for AAPL, got {account.get_filled_trades('AAPL')}"

        # ...and it was a RESTING SELL_STOP attached to the entry, filled AT the stop price —
        # not a market sell submitted by a per-bar pass through expert code. This is the whole
        # point of the 2026-08-06 change, so assert the mechanism, not just the outcome.
        #
        # The stop price is the equity-loss budget solved for price: entry 100, 50 shares,
        # cap = 100k * 1% = $1000 -> 100 - 1000/50 = 80. The Friday bar (low 79) crosses it.
        from ba2_common.core.types import OrderDirection, OrderType

        stops = [o for o in account.get_orders()
                 if o.symbol == "AAPL" and o.order_type == OrderType.SELL_STOP]
        assert len(stops) == 1, f"expected exactly one resting SELL_STOP, got {stops}"
        stop = stops[0]
        assert stop.side == OrderDirection.SELL
        assert stop.stop_price == 80.0, f"stop price {stop.stop_price} != equity-budget price 80.0"
        assert stop.open_price == 80.0, "stop must fill AT the stop price, not at a market price"
        assert stop.depends_on_order is not None, "stop must be attached to the entry order"

        # And NOTHING else sold: a leftover per-bar stop pass would show up as a second,
        # depends_on_order-less MARKET sell here (the double-exit this change had to avoid).
        market_sells = [o for o in account.get_orders()
                        if o.symbol == "AAPL" and o.side == OrderDirection.SELL
                        and o.order_type == OrderType.MARKET]
        assert market_sells == [], f"unexpected market sell(s) alongside the stop: {market_sells}"
    finally:
        ctx.__exit__(None, None, None)


def test_e2e_bypass_stop_keeps_name_within_cap():
    """END-TO-END: an otherwise-identical held position whose loss stays WITHIN the equity cap
    (a shallow -5% drawdown) is NOT sold by the stop -> it is still held at the end of the run."""
    engine, account, expert, ctx, ps = _build_run(BARS_SHALLOW, account_id=62, expert_id=62)
    try:
        engine.run()

        positions = account.get_positions()
        aapl = [p for p in positions if p["symbol"] == "AAPL" and p.get("qty", 0) > 0]
        assert aapl, "expected AAPL still held (loss within the equity cap -> no stop)"
    finally:
        ctx.__exit__(None, None, None)


# The two gating unit tests that used to live here drove ``DailyBacktestEngine._apply_bypass_stops``
# directly (no risk_per_trade_pct -> no call; positive -> one cached manager + one apply_stop_losses).
# That helper was deleted on 2026-08-06 along with the whole per-bar expert-side stop pass, so the
# tests were testing a mechanism that no longer exists. What replaced them:
#
#   * the two E2E tests ABOVE still pin the observable behaviour (breach -> exit, within cap -> held);
#   * ``packages/experts/tests/test_factorranker_portfolio.py`` pins the stop PRICE and the fact that
#     _submit_buy attaches it, which is where the rule now lives.
