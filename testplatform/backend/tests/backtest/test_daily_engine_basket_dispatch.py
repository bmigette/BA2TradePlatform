"""Root-cause confirmation (Task 1 of the senate-basket-dispatch plan): FMPSenateTraderCopy's
analyze_as_of already returns List[Recommendation], but daily_engine.py has no code path that
expects a list. This test proves that today, BEFORE any engine change, so Task 3's fix has a
real before/after.

**Deviation from the plan's "Critical finding" write-up (verified against a live run of this
harness, not just a code read):** the plan's prose says the AttributeError is "silently caught
by the per-symbol except Exception at line ~815, logged as 'analyze_as_of failed for
{symbol}'", implying the loop degrades gracefully to zero recommendations while continuing to
analyse every universe symbol. That is NOT what happens. Reading ``_run_expert_bar`` closely:
the ``try/except`` at daily_engine.py:813-823 wraps ONLY the ``expert.analyze_as_of(as_of, ctx)``
call (line 814). The very next statement, ``_recommendation_to_expert_recommendation(rec, ...)``
(line 825), is OUTSIDE that try block — and it is THAT call which does ``getattr(rec, "skip",
False)`` then ``action = rec.signal`` (daily_engine.py:284-286), which is where the
``AttributeError: 'list' object has no attribute 'signal'`` actually gets raised when ``rec`` is
a list. Because that call is unguarded, the AttributeError propagates out of
``_run_expert_bar``, out of the per-bar ``for expert, ... in self.experts:`` loop, and out of
``engine.run()`` itself uncaught (confirmed empirically: a standalone harness run raises
``AttributeError`` from ``engine.run()``, and ``daily_backtest_handler.py``'s ``engine.run()``
call site (~line 687) has no try/except around it either, so the SAME crash would surface all
the way up through a real backtest handler invocation).

Net effect, verified: the FIRST universe symbol (alphabetically/positionally first, "AAPL" in
this test) is analysed once, then the crash aborts the run before any later symbol/bar is ever
reached — so ``call_count`` on the stub expert is 1, NOT ``len(universe)`` as originally
predicted, and there is no "analyze_as_of failed for {symbol}" log line (that log line is
genuinely dead code for this exact failure — it only fires for exceptions raised BY
``analyze_as_of`` itself, not for exceptions raised while consuming the value it returned).
The "zero ExpertRecommendation rows" part of the original prediction DOES hold (nothing gets
persisted before the crash), just for a different reason (crash-before-persist, not
swallow-and-skip).

This matters for Task 2/3 downstream: the current per-symbol try/except is too narrow to be
"the bug" on its own — the ``analyzes_as_basket`` dispatch mode Task 2 adds must not merely
avoid the list-vs-single-object mismatch, it must also not reintroduce a bare
``_recommendation_to_expert_recommendation`` call outside a try/except when processing a basket
item, or one bad recommendation in the list will still abort the whole bar.

Run from testplatform/backend:
    "C:\\Users\\basti\\ba2-venvs\\test\\Scripts\\python.exe" -m pytest tests/backtest/test_daily_engine_basket_dispatch.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


# 5 daily bars, 2-symbol universe (AAPL, MSFT) so "called once, not once-per-symbol" is
# meaningful. Shape copied from test_daily_engine_bypass.py's BARS/START/END/_bar_rows.
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


class _StubListReturningExpert(MarketExpertInterface):
    """Mimics FMPSenateTraderCopy's CURRENT (broken-in-backtest) shape: analyze_as_of
    returns a list, but carries none of the special markers this plan is about to add
    (no ``analyzes_as_basket``, no ``bypasses_classic_rm``) — from the engine's point of
    view this looks like an ordinary per-symbol expert, just one whose return value happens
    to be the wrong type."""

    def __init__(self, id: int):
        super().__init__(id)
        self.call_count = 0

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub list-returning expert (pre-fix shape)."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.call_count += 1
        return [
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
                details="stub basket rec",
            )
        ]


def _build_run(account_id=52, expert_id=52):
    """Wire a backtest fixture for the stub list-returning expert.

    Mirrors test_daily_engine_bypass.py's ``_build_run`` almost verbatim, substituting
    ``_StubListReturningExpert`` and a 2-symbol universe. Returns
    (engine, account, expert, db_ctx, price_source). Caller MUST close db_ctx.
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
    ctx = backtest_trading_db(f"engine-basket-dispatch-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    # ExpertInstance.enter_market_ruleset_id is non-nullable; seed a ruleset to satisfy the FK
    # (the crash happens before the ruleset would ever be evaluated for this stub).
    ruleset_id = seed_enter_long_ruleset(name="backtest-basket-dispatch-stub")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_StubListReturningExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))
    ps.load_bars("MSFT", _bar_rows(BARS))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    expert = _StubListReturningExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL", "MSFT"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),  # this stub never reaches indicator-consuming code
    )
    return engine, account, expert, ctx, ps


def test_list_returning_expert_crashes_the_backtest_run_today():
    """Documents the CURRENT broken behavior for real (verified by actually running the
    engine, not just reading the source): a list-returning ``analyze_as_of`` is NOT gracefully
    degraded to "zero recommendations, keep going" — it raises an uncaught ``AttributeError``
    out of ``engine.run()`` itself, after processing exactly the FIRST universe symbol.

    When Task 3 lands, an equivalent expert marked ``analyzes_as_basket = True`` must instead
    produce real ExpertRecommendation rows via the new Task 2 dispatch path, with NO crash.
    """
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertRecommendation

    engine, account, expert, ctx, ps = _build_run()
    try:
        with pytest.raises(AttributeError, match="signal"):
            engine.run()

        # The loop got exactly ONE symbol in (AAPL, first in the universe list) before the
        # unguarded `_recommendation_to_expert_recommendation(rec, ...)` call (daily_engine.py
        # line 825, outside the try/except at 813-823) raised on `rec.signal`. It did NOT reach
        # MSFT, and it did NOT retry/continue — this is the loop dying, not "per-symbol skip".
        assert expert.call_count == 1

        # Nothing was ever persisted (the crash happens before any DB write for this rec).
        assert get_all_instances(ExpertRecommendation) == []
    finally:
        ctx.__exit__(None, None, None)
