"""Per-bar EQUITY-loss STOP for BYPASS experts (FactorRanker) in the daily engine.

A bypass expert (``bypasses_classic_rm = True``) sizes by weight and skips the classic
risk manager, so between its scheduled rebalances a held name has NO downside protection.
The fix is a per-name stop that reuses ``risk_per_trade_pct`` as a max-loss-per-name cap
measured in % of TOTAL EQUITY: a held name is sold (full exit) when its unrealized dollar
loss reaches ``equity * risk_per_trade_pct / 100``. The stop runs ONLY on NON-rebalance bars
(the rebalance owns the book on its scheduled bars) and is lookahead-safe (it submits a
MARKET sell that fills on a later bar per the fill model).

This module has TWO layers:

  * an END-TO-END engine bar test (the real ``DailyBacktestEngine.run`` loop, the simulated
    AsOfPriceSource + BacktestAccount harness): a held bypass position that breaches the
    equity-loss cap on a non-rebalance bar is EXITED; an otherwise-identical position whose
    loss stays within the cap is NOT sold;
  * gating unit tests for the ``_apply_bypass_stops`` helper (no-op when ``risk_per_trade_pct``
    is unset / non-positive).

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

        # The exit was a real SELL FILL (not a cancel / a no-op): the stop submitted a market
        # sell that filled on a later bar per the fill model.
        sell_fills = [
            t for t in account.get_filled_trades(symbol="AAPL")
            if str(t.get("direction") or t.get("side") or "").lower() == "sell"
        ]
        assert sell_fills, f"expected a filled stop SELL for AAPL, got {account.get_filled_trades('AAPL')}"
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


# --------------------------------------------------------------------------- #
# Gating unit tests for the _apply_bypass_stops helper
# --------------------------------------------------------------------------- #

def test_apply_bypass_stops_noop_when_risk_pct_unset(monkeypatch):
    """When ``risk_per_trade_pct`` resolves to None/0, the helper does NOT touch the book
    (no FactorPortfolioManager.apply_stop_losses call)."""
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_experts.FactorRanker import portfolio as pf_mod

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)  # no __init__ / no DB needed

    class _Expert:
        bypasses_classic_rm = True

        def get_setting_with_interface_default(self, key, log_warning=False):
            assert key == "risk_per_trade_pct"
            return None  # unset

    calls = []
    monkeypatch.setattr(
        pf_mod.FactorPortfolioManager, "apply_stop_losses",
        lambda self, *a, **kw: calls.append((a, kw)), raising=True,
    )

    engine._apply_bypass_stops(_Expert(), 99, {}, datetime(2024, 1, 5))
    assert calls == []


def test_apply_bypass_stops_invokes_manager_when_risk_pct_set(monkeypatch):
    """When ``risk_per_trade_pct`` is positive, the helper builds (once) and calls
    FactorPortfolioManager(expert_id).apply_stop_losses(float(stop_pct), equity=...).

    The manager + virtual_equity_pct are cached per run (perf #47): the per-bar stop reuses the
    same manager and passes a cheaply-computed equity (account.get_balance() * pct) instead of
    re-querying ExpertInstance twice per bar."""
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_experts.FactorRanker import portfolio as pf_mod
    import ba2_common.core.db as _db_mod

    engine = DailyBacktestEngine.__new__(DailyBacktestEngine)
    # Per-run caches normally set in __init__ (bypassed here via __new__).
    engine._bypass_pm = {}
    engine._bypass_veq_pct = {}

    # The flat-account fast path gates on account.get_positions(); give the engine a stub
    # account that reports a held position (so the helper proceeds) and a cash balance (used to
    # compute the stop equity = balance * virtual_equity_pct/100).
    class _Account:
        def get_positions(self):
            return [{"symbol": "AAPL", "qty": 1}]

        def get_balance(self):
            return 1000.0

    engine.account = _Account()

    class _Expert:
        bypasses_classic_rm = True

        def get_setting_with_interface_default(self, key, log_warning=False):
            return 1.0

    init_calls = []
    apply_calls = []

    def _fake_init(self, expert_instance_id):
        init_calls.append(expert_instance_id)

    class _Inst:
        virtual_equity_pct = 100.0

    monkeypatch.setattr(pf_mod.FactorPortfolioManager, "__init__", _fake_init, raising=True)
    monkeypatch.setattr(
        pf_mod.FactorPortfolioManager, "apply_stop_losses",
        lambda self, stop_pct, equity=None, prices=None: apply_calls.append((stop_pct, equity)),
        raising=True,
    )
    # _bypass_manager reads virtual_equity_pct via get_instance — stub it (no DB in this unit test).
    monkeypatch.setattr(_db_mod, "get_instance", lambda *a, **k: _Inst(), raising=True)

    engine._apply_bypass_stops(_Expert(), 77, {}, datetime(2024, 1, 5))
    # Manager built ONCE for the expert; stop invoked with the pct and equity = 1000 * 100/100.
    assert init_calls == [77]
    assert apply_calls == [(1.0, 1000.0)]

    # A SECOND bar reuses the cached manager (no new construction) — the perf win.
    engine._apply_bypass_stops(_Expert(), 77, {}, datetime(2024, 1, 6))
    assert init_calls == [77]
    assert apply_calls == [(1.0, 1000.0), (1.0, 1000.0)]
