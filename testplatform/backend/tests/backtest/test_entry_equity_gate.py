"""Live-parity equity-sufficiency entry gate (Phase 1c residual of the live↔backtest unification
plan). _run_expert_bar now skips an entry when the expert lacks sufficient available equity —
mirroring TradeManager.process_expert_recommendations_after_analysis:1146-1155, which calls the
SAME shared MarketExpertInterface.has_sufficient_equity_for_trading (available balance must be
>= minimum_equity_threshold_percent of virtual balance, default 5%).

The gate is inert for a well-funded flat account (available == 100% of virtual >> 5%); it only
bites once enough capital is already deployed — proven here by seeding a heavy OPENED position so
available drops below the 5% threshold, then asserting the next entry is skipped.
"""
from datetime import date, datetime

from ba2_common.core.db import add_instance
from ba2_common.core.models import Transaction
from ba2_common.core.types import (OrderDirection, OrderRecommendation, Recommendation,
                                   TransactionStatus)
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

_CFG = {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
        "slippage_bps": 0.0, "fill_model": "next_bar_open"}
_D = datetime(2024, 1, 3)


def _bars(px):
    return [{"Date": datetime(2024, 1, d), "Open": px, "High": px, "Low": px,
             "Close": px, "Volume": 1000} for d in (2, 3, 4)]


class _BuyExpert(MarketExpertInterface):
    """Deterministic expert: always BUY the analysed symbol (high confidence / profit)."""

    def __init__(self, id, price_source):
        super().__init__(id)
        self._ps = price_source

    @classmethod
    def description(cls):
        return "equity-gate test expert"

    def render_market_analysis(self, market_analysis):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None

    def analyze_as_of(self, as_of, context):
        close = self._ps.close_at(self._gather_symbol, as_of) or 100.0
        return Recommendation(signal=OrderRecommendation.BUY, confidence=90.0,
                              current_price=float(close), details="buy",
                              expected_profit_percent=25.0)


def _build(expert_id=8801, account_id=8801, held_qty=0.0):
    """Wire seams + backtest DB + account + ruleset + expert; optionally seed a heavy MSFT hold."""
    from app.services.backtest.backtest_db import (backtest_trading_db,
                                                   seed_account_definition, seed_expert_instance)
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.daily_engine import DailyBacktestEngine

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"equitygate-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, _CFG)
    ruleset_id = seed_enter_long_ruleset()
    seed_expert_instance(account_id=account_id, expert_class_name="_BuyExpert",
                         enter_market_ruleset_id=ruleset_id, instance_id=expert_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bars(100.0))
    ps.load_bars("MSFT", _bars(100.0))
    ps.set_clock(_D)

    account = BacktestAccount(account_id, ps, _CFG)
    resolver.register_account(account_id, account)

    expert = _BuyExpert(expert_id, ps)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                          "enable_buy": (True, "bool")})
    resolver.register_expert(expert_id, expert)

    if held_qty > 0:
        # a heavy OPENED position so _calculate_used_balance consumes most of the virtual balance
        add_instance(Transaction(expert_id=expert_id, symbol="MSFT", quantity=held_qty,
                                 open_price=100.0, side=OrderDirection.BUY,
                                 status=TransactionStatus.OPENED))

    engine = DailyBacktestEngine(
        account=account, experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps, config={"start_date": date(2024, 1, 2), "end_date": date(2024, 1, 4),
                                 "enabled_instruments": ["AAPL"], "seed": 42},
        indicator_provider=None)
    engine._indicator_provider = object()  # notional sizing -> no ATR build
    return engine, account, expert, expert_id, ruleset_id, ctx


def _aapl_orders(account):
    # After the temp-list flow, a funded entry is SIZED + SUBMITTED (qty>0), not left as a qty=0
    # PENDING order — so we assert on any AAPL order the bar created (funded => quantity>0).
    return [o for o in account.get_orders() if o.symbol == "AAPL"]


def test_equity_sufficient_when_flat_opens_entry():
    engine, account, expert, expert_id, ruleset_id, ctx = _build(held_qty=0.0)
    try:
        ok, reason = expert.has_sufficient_equity_for_trading()
        assert ok is True, reason  # flat account: available == 100% of virtual >> 5%
        created = engine._run_expert_bar(expert, expert_id, {}, ruleset_id, ["AAPL"], _D)
        assert created is True
        aapl = _aapl_orders(account)
        assert aapl, "a flat, funded account should create the AAPL entry"
        assert any(o.quantity and o.quantity > 0 for o in aapl), "entry must be RM-sized (qty>0)"
    finally:
        ctx.__exit__(None, None, None)


def test_equity_insufficient_blocks_entry():
    # 970 shares * $100 = $97k used of a $100k virtual balance -> ~3% available < 5% threshold.
    engine, account, expert, expert_id, ruleset_id, ctx = _build(held_qty=970.0)
    try:
        ok, reason = expert.has_sufficient_equity_for_trading()
        assert ok is False and "threshold" in reason
        created = engine._run_expert_bar(expert, expert_id, {}, ruleset_id, ["AAPL"], _D)
        assert created is False
        # temp-list flow: an unfunded/blocked entry is NEVER persisted (no qty=0 churn either).
        assert not _aapl_orders(account), "insufficient equity must skip the entry (no order created)"
    finally:
        ctx.__exit__(None, None, None)
