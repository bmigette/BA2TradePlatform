"""RAIL_BREAKER_HALTED becomes reachable in a BACKTEST: a drawn-down sleeve stops entering.

Design ``docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`` 11.5. The
consolidated review proved this refusal was unreachable in the backtest: the breaker LATCH
was consulted by ``check_rails`` in both runtimes, but the TRANSITIONS were made only by
``option_lifecycle_service``, which lives in the live tree and runs off ``JobManager``. So
``get_breaker_state`` answered ``BreakerState()`` on every bar of every backtest and the
drawdown breaker gated exactly nothing -- the option grid would have been searching a
strategy space in which one of the sleeve's risk rails did not exist.

This is the end-to-end proof of the fix, at the level that matters: a real
``DailyBacktestEngine.run()``, a real option entry through the real
``TradeActionEvaluator``/``admit_option_entry`` path, and a sleeve that loses enough to trip
its own breaker MID-RUN and is then refused the next entry it tries to make.

THE CONTROL IS THE POINT. "No second option was opened" is worth nothing on its own -- a
fixture typo produces the same observation. The identical run with a breaker slack enough not
to trip DOES open it, so the only difference between the two outcomes is the breaker.

Run from the backend dir (with the worktree on PYTHONPATH):
    python -m pytest tests/backtest/test_option_breaker_halts_backtest_entries.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from typing import Any, Dict, List

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


# --------------------------------------------------------------------------- #
# Fixture: AAPL from the first bar, MSFT only from bar 5.
#
# ``resolve_universe`` filters the configured instruments to those with a bar on the day, so
# MSFT is simply not analysable until 2024-02-07. That is what makes the SECOND entry attempt
# land AFTER the sleeve has already drawn down -- no schedule trickery, and no reliance on the
# order in which two same-bar entries would have been evaluated.
# --------------------------------------------------------------------------- #
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 8)
EXPIRY = date(2024, 3, 15)                     # 43 DTE at the run start
AAPL_OCC = "AAPL240315C00180000"
MSFT_OCC = "MSFT240315C00400000"

#                    (date,             open, high, low, close)
AAPL_BARS = [
    (date(2024, 2, 1), 180, 181, 179, 180),    # entry bar: buy_call fires
    (date(2024, 2, 2), 180, 181, 179, 180),    # the call FILLS at the premium open
    (date(2024, 2, 5), 180, 181, 179, 180),    # held, flat
    (date(2024, 2, 6), 120, 121, 119, 120),    # the underlying collapses
    (date(2024, 2, 7), 120, 121, 119, 120),    # MSFT joins the universe HERE
    (date(2024, 2, 8), 120, 121, 119, 120),
]
MSFT_BARS = [
    (date(2024, 2, 7), 400, 401, 399, 400),
    (date(2024, 2, 8), 400, 401, 399, 400),
]

#: The premium follows the underlying: the ATM call is worth ~4.6 while spot is 180 and
#: almost nothing once spot is 120 (a 180-strike call, 43 DTE). The sleeve's whole option
#: outlay is therefore lost, which is the drawdown the breaker measures.
AAPL_PREMIUM = [
    (date(2024, 2, 2), 4.6, 4.7, 4.5, 4.6),
    (date(2024, 2, 5), 4.6, 4.7, 4.5, 4.6),
    (date(2024, 2, 6), 0.05, 0.06, 0.04, 0.05),
    (date(2024, 2, 7), 0.05, 0.06, 0.04, 0.05),
    (date(2024, 2, 8), 0.05, 0.06, 0.04, 0.05),
]
MSFT_PREMIUM = [
    (date(2024, 2, 7), 9.0, 9.2, 8.8, 9.0),
    (date(2024, 2, 8), 9.0, 9.2, 8.8, 9.0),
]

#: The sleeve loses ~4.6% of the account on the collapse. 2% therefore TRIPS (and stays
#: tripped: the re-arm line is half of it, and the sleeve never recovers), 90% cannot.
BREAKER_TRIPS = 2.0
BREAKER_SLACK = 90.0

RAILS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 40.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
}

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

ENTRY_ACTION = {
    "action_type": "buy_call",
    "option_strike_method": "percent_otm",
    "option_strike_param": 0.0,          # ~ATM
    "option_dte_min": 20,
    "option_dte_max": 60,
    "option_sizing": 5.0,                # 5% of the sleeve
}


class _BuyExpert(MarketExpertInterface):
    """Always bullish, so the enter gate is decided by the RAILS and nothing else."""

    def __init__(self, id: int):
        super().__init__(id)

    @classmethod
    def description(cls) -> str:            # abstract
        return "Stub always-BUY expert for the backtest breaker-halt test."

    def render_market_analysis(self, market_analysis) -> str:   # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:   # abstract
        return None

    def analyze_as_of(self, as_of, context):
        # The engine pins the symbol under analysis on the expert before each call (the seam
        # the real experts read), so one stub covers both underlyings.
        symbol = getattr(self, "_gather_symbol", "AAPL")
        price = context.account.get_instrument_current_price(symbol)
        return Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                              current_price=float(price), details=f"buy {symbol}",
                              raw_outputs={})


def _bar_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _seed_cache(db_path: str) -> None:
    """One ~ATM call per underlying, plus the premium bars that price and mark it."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    for underlying, occ, strike, (bid, ask) in (
        ("AAPL", AAPL_OCC, 180.0, (4.5, 4.7)),
        ("MSFT", MSFT_OCC, 400.0, (8.8, 9.2)),
    ):
        cache.write_chain_rows(
            underlying, START.date().isoformat(),
            [{"occ_symbol": occ, "option_type": "call", "strike": strike,
              "expiry": EXPIRY.isoformat(), "bid": bid, "ask": ask,
              "last": (bid + ask) / 2, "iv": 0.30, "delta": 0.50,
              "open_interest": 5000}])
    bars = []
    for occ, prem, strike, underlying in ((AAPL_OCC, AAPL_PREMIUM, 180.0, "AAPL"),
                                          (MSFT_OCC, MSFT_PREMIUM, 400.0, "MSFT")):
        for (d, o, h, low, c) in prem:
            bars.append({"occ_symbol": occ, "date": d.isoformat(), "open": o, "high": h,
                         "low": low, "close": c, "volume": 400, "underlying": underlying,
                         "option_type": "call", "strike": strike,
                         "expiry": EXPIRY.isoformat()})
    cache.write_bar_rows(bars)


def _run(*, breaker_pct: float, account_id: int, expert_id: int):
    """One full engine run with a ``classic_options`` sleeve.

    Returns ``(the underlyings this sleeve has an option order for, the breaker at the end
    of the run)``.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    tmpdir = tempfile.mkdtemp(prefix="breaker-halt-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"breaker-halt-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_ruleset_from_tree(buy_tree=None, name=f"breaker-halt-{account_id}",
                                            entry_action=ENTRY_ACTION)
        seed_expert_instance(account_id=account_id, expert_class_name="_BuyExpert",
                             enter_market_ruleset_id=ruleset_id,
                             open_positions_ruleset_id=None, instance_id=expert_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _bar_rows(AAPL_BARS))
        ps.load_bars("MSFT", _bar_rows(MSFT_BARS))
        ps.set_clock(START)

        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)

        expert = _BuyExpert(expert_id)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "risk_manager_mode": ("classic_options", "str"),
            "circuit_breaker_pct": (breaker_pct, "float"),
            **{k: (v, "int" if isinstance(v, int) else "float") for k, v in RAILS.items()},
        })
        resolver.register_expert(expert_id, expert)

        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, expert.settings, ruleset_id)],
            price_source=ps,
            config={"start_date": START, "end_date": END,
                    "enabled_instruments": ["AAPL", "MSFT"], "seed": 42,
                    "entry_action": ENTRY_ACTION},
            indicator_provider=object())
        engine.run()
        # READ THE LATCH INSIDE THE RUN CONTEXT. The sleeve's process state is keyed
        # (thread, expert) while a backtest's in-memory trade store is active and
        # (None, expert) otherwise, so a read taken after the context exits would look up the
        # LIVE key and answer BreakerState() no matter what the run did.
        import ba2_common.core.OptionRiskManagement as rm

        return _option_underlyings(account), rm.get_breaker_state(expert_id)
    finally:
        ctx.__exit__(None, None, None)


def _option_underlyings(account) -> List[str]:
    """Every underlying this sleeve has an option ORDER for.

    Orders, not positions: an entry that was ADMITTED but had not filled by the last bar is
    still an entry the breaker failed to stop, and folding it into "no position" would let a
    fill-timing accident pass for a refusal.
    """
    from ba2_common.core.trade_store import orders_where
    from ba2_common.core.types import AssetClass

    out = set()
    for order in orders_where():
        if getattr(order, "asset_class", None) is AssetClass.OPTION:
            underlying = getattr(order, "underlying_symbol", None) or getattr(
                order, "symbol", None)
            if underlying:
                out.add(underlying)
    return sorted(out)


@pytest.fixture(autouse=True)
def _clean_breaker_state():
    import ba2_common.core.OptionRiskManagement as rm

    rm.reset_state()
    yield
    rm.reset_state()


def test_a_slack_breaker_lets_the_SECOND_option_entry_through():
    """The control. Same fixture, same drawdown, a breaker that cannot trip -- and MSFT's
    entry opens. Without this the halt test below is indistinguishable from a broken fixture.
    """
    underlyings, breaker = _run(breaker_pct=BREAKER_SLACK, account_id=9301, expert_id=9301)
    assert underlyings == ["AAPL", "MSFT"], underlyings
    assert breaker.halted is False


def test_a_drawn_down_sleeve_stops_opening_option_entries_MID_RUN():
    """RAIL_BREAKER_HALTED, reached from a backtest for the first time.

    The AAPL call is opened on the first bar and is worth almost nothing three bars later;
    the sleeve's equity has fallen further than ``circuit_breaker_pct``, so the per-bar
    transition stands it down -- and the MSFT entry that becomes available on the next bar is
    refused. Only AAPL's order exists at the end of the run.

    MUTATION KILLS:
      * delete the per-bar ``update_sleeve_breaker`` call from ``daily_engine.run()``: the
        latch is never set, MSFT opens, and this test reads ["AAPL", "MSFT"] -- exactly the
        pre-2026-09-01 behaviour;
      * restore ``sleeve_equity`` to ``account.get_balance()``: the sleeve's cash never falls
        (the option outlay is small and cash only RISES as the position loses value), so the
        breaker never trips and MSFT opens again.
    """
    underlyings, breaker = _run(breaker_pct=BREAKER_TRIPS, account_id=9302, expert_id=9302)
    assert underlyings == ["AAPL"], underlyings
    # ...and it is the BREAKER that stopped it, not an exhausted rail or a missing chain.
    assert breaker.halted is True
