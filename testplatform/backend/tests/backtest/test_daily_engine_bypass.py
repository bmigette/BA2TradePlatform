"""Engine BYPASS routing (piece 1b): a ``bypasses_classic_rm`` expert rebalances directly.

An expert that declares ``bypasses_classic_rm = True`` (the marker FactorRanker carries) does
NOT use the enter/exit ruleset OR the classic risk manager. It emits ``{symbol: weight}`` target
weights once per bar and rebalances via its OWN FactorPortfolioManager. This test drives the
``DailyBacktestEngine`` against a deterministic STUB bypass expert (no providers, no network)
and asserts the engine takes the rebalance path:

  * ``FactorPortfolioManager.rebalance`` IS called (the targets are routed to the portfolio
    manager, which prices each name off the account and submits the buy/sell deltas);
  * ``TradeRiskManagement.review_and_prioritize_pending_orders`` is NEVER invoked (the classic
    RM / position-sizing path is fully skipped);
  * ``TradeActionEvaluator`` is NEVER constructed (no enter-ruleset evaluation);
  * a real long position opens (so the rebalance actually went through ``account.submit_order``);
  * one equity snapshot per bar is recorded.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_daily_engine_bypass.py -v
"""
from __future__ import annotations

from datetime import date, datetime

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


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


class _StubBypassExpert(MarketExpertInterface):
    """Deterministic BYPASS expert: targets 100% AAPL every bar.

    Declares ``bypasses_classic_rm = True`` (the FactorRanker marker) and returns a single
    basket-level ``Recommendation`` whose ``raw_outputs['targets']`` is the ``{symbol: weight}``
    book. ``current_price`` is None (the decision is cross-sectional, like FactorRanker), and no
    ExpertRecommendation seam is used — the engine routes the targets straight to the portfolio
    manager. Records every as_of it was asked about so the test can assert one call per bar.
    """

    bypasses_classic_rm = True

    def __init__(self, id: int):
        super().__init__(id)
        self.seen_as_of: list = []

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub bypass expert (targets 100% AAPL, rebalances via portfolio manager)."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    # Target a PARTIAL weight (30%) so the book is stable bar-to-bar: with FactorRanker's
    # cash-based virtual balance, a 100% target would consume all cash on the first buy and
    # then thrash (sell-all / re-buy) as the available cash collapses. A 30% target keeps cash
    # on hand so the holding is recognised and roughly held across bars — the realistic
    # rebalance behaviour we want to exercise.
    TARGET_WEIGHT = 0.30

    def analyze_as_of(self, as_of, context):
        self.seen_as_of.append(as_of.date() if hasattr(as_of, "date") else as_of)
        return Recommendation(
            signal=OrderRecommendation.OVERWEIGHT,
            confidence=0.0,
            current_price=None,  # basket-level (cross-sectional), like FactorRanker
            details="stub bypass targets",
            raw_outputs={
                "targets": {"AAPL": self.TARGET_WEIGHT},
                "book": {"universe_size": 1},
            },
        )


def _build_run(account_id=51, expert_id=51):
    """Wire a backtest fixture for the stub BYPASS expert (no ruleset, no RM gates).

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
    ctx = backtest_trading_db(f"engine-bypass-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    # ExpertInstance.enter_market_ruleset_id is non-nullable; seed a ruleset to satisfy the FK
    # even though the BYPASS path never evaluates it.
    ruleset_id = seed_enter_long_ruleset(name="backtest-bypass-stub")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_StubBypassExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    expert = _StubBypassExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    # ruleset_id is passed as None in the engine tuple to mirror the handler's bypass tuple.
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, None)],
        price_source=ps,
        config=config,
        indicator_provider=object(),  # bypass path never builds an ATR provider
    )
    return engine, account, expert, ctx, ps


def test_bypass_expert_rebalances_and_rm_not_invoked(monkeypatch):
    """A bypass expert routes targets to FactorPortfolioManager.rebalance; the classic RM
    (TradeRiskManagement) and the ruleset evaluator (TradeActionEvaluator) are never used."""
    import ba2_common.core.TradeRiskManagement as RM_mod
    from ba2_experts.FactorRanker import portfolio as pf_mod

    engine, account, expert, ctx, ps = _build_run()
    try:
        # --- Spy: count FactorPortfolioManager.rebalance calls (the bypass route) ---
        rebalance_calls: list = []
        orig_rebalance = pf_mod.FactorPortfolioManager.rebalance

        def _spy_rebalance(self, target_weights, equity=None):
            rebalance_calls.append(dict(target_weights))
            return orig_rebalance(self, target_weights, equity)

        monkeypatch.setattr(
            pf_mod.FactorPortfolioManager, "rebalance", _spy_rebalance, raising=True
        )

        # --- Guard: the classic RM must NEVER be constructed or invoked on the bypass path ---
        rm_calls: list = []
        orig_review = RM_mod.TradeRiskManagement.review_and_prioritize_pending_orders

        def _forbidden_review(self, *a, **kw):
            rm_calls.append((a, kw))
            return orig_review(self, *a, **kw)

        monkeypatch.setattr(
            RM_mod.TradeRiskManagement,
            "review_and_prioritize_pending_orders",
            _forbidden_review,
            raising=True,
        )

        # --- Guard: the enter-ruleset evaluator must NEVER be constructed on the bypass path ---
        import ba2_common.core.TradeActionEvaluator as TAE_mod

        tae_calls: list = []
        orig_tae_init = TAE_mod.TradeActionEvaluator.__init__

        def _spy_tae_init(self, *a, **kw):
            tae_calls.append((a, kw))
            return orig_tae_init(self, *a, **kw)

        monkeypatch.setattr(
            TAE_mod.TradeActionEvaluator, "__init__", _spy_tae_init, raising=True
        )

        engine.run()

        # analyze_as_of called once per bar (the bypass expert resolves its own universe).
        assert expert.seen_as_of == [d for (d, *_rest) in BARS]

        # The targets WERE routed through the portfolio manager's rebalance (>=1 call).
        assert len(rebalance_calls) >= 1
        assert rebalance_calls[0] == {"AAPL": _StubBypassExpert.TARGET_WEIGHT}

        # The classic RM / position-sizing path was NEVER taken.
        assert rm_calls == []
        # The enter-ruleset evaluator was NEVER constructed.
        assert tae_calls == []

        # The rebalance actually submitted a real order -> a long AAPL position opened.
        positions = account.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["qty"] > 0

        # One equity snapshot per simulated bar.
        assert len(account.get_balance_history()) == len(BARS)
    finally:
        ctx.__exit__(None, None, None)


def test_real_factorranker_carries_bypass_marker():
    """Sanity: the real FactorRanker class declares the bypass marker the engine branches on,
    and a non-bypass clean expert does not (so the engine routes them differently)."""
    from ba2_experts.FactorRanker import FactorRanker
    from ba2_experts.FMPEarningsDrift import FMPEarningsDrift

    assert getattr(FactorRanker, "bypasses_classic_rm", False) is True
    assert getattr(FMPEarningsDrift, "bypasses_classic_rm", False) is False
