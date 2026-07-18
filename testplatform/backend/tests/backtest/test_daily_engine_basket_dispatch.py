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


# ---------------------------------------------------------------------------
# Task 2: the analyzes_as_basket dispatch mode (fixes the crash Task 1 documented above).
# ---------------------------------------------------------------------------
class _StubBasketExpert(MarketExpertInterface):
    """Basket expert: ONE analyze_as_of call per bar returns recommendations for
    MULTIPLE symbols. Each Recommendation carries its own symbol in raw_outputs['symbol']
    (the engine has no per-symbol loop to infer it from anymore)."""

    analyzes_as_basket = True

    def __init__(self, id: int):
        super().__init__(id)
        self.call_count = 0

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub basket expert (one call, many symbols)."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.call_count += 1
        return [
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
                details="stub basket rec", raw_outputs={"symbol": "AAPL"},
            ),
            Recommendation(
                signal=OrderRecommendation.BUY, confidence=60.0, current_price=50.0,
                details="stub basket rec", raw_outputs={"symbol": "MSFT"},
            ),
        ]


class _StubBasketExpertReturnsSingleRec(MarketExpertInterface):
    """Basket expert with a SHAPE BUG: declares ``analyzes_as_basket = True`` (so the engine
    dispatches it through ``_run_basket_expert_bar``, expecting ``List[Recommendation]``) but
    ``analyze_as_of`` actually returns a single ``Recommendation`` -- exactly the mistake a
    future dual-mode ``analyze_as_of`` (symbol-pinned vs. basket-mode branching, as planned for
    Task 5) could make by taking the wrong branch. Used to prove the engine's type guard logs
    + skips the bar instead of letting an uncaught ``TypeError`` (``'Recommendation' object is
    not iterable``) propagate out of ``engine.run()``."""

    analyzes_as_basket = True

    def __init__(self, id: int):
        super().__init__(id)
        self.call_count = 0

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub basket expert that (buggily) returns a single Recommendation."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.call_count += 1
        # BUG (deliberate, for the test): a single Recommendation, NOT a list.
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=80.0, current_price=100.0,
            details="stub single rec (wrong shape for a basket expert)",
            raw_outputs={"symbol": "AAPL"},
        )


def _build_basket_run(account_id=53, expert_id=53, expert_cls=None):
    """Wire a backtest fixture for a stub BASKET expert (``analyzes_as_basket = True``).

    Mirrors ``_build_run`` above (the pre-fix, unmarked list-returning stub) and
    ``test_daily_engine_unit.py``'s ``_build_run``, but registers a basket-marked stub expert
    (``_StubBasketExpert`` by default; pass ``expert_cls`` for a different shape, e.g. the
    non-list-returning stub used to test the type guard) and enables the settings the classic
    RM / live-parity gates need to actually fund an order (``allow_automated_trade_opening`` /
    ``enable_buy`` — interface defaults are False/True and the RM gates on them). Returns
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

    expert_cls = expert_cls or _StubBasketExpert

    cfg = {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"engine-basket-dispatch-real-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    ruleset_id = seed_enter_long_ruleset(name="backtest-basket-dispatch-real")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name=expert_cls.__name__,
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))
    ps.load_bars("MSFT", _bar_rows(BARS))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    expert = expert_cls(expert_id)
    # Enable automated opening + buy (interface defaults are False/True; RM gates on these) --
    # same settings test_daily_engine_unit.py's classic-path stub needs to actually fund an order.
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
        "enabled_instruments": ["AAPL", "MSFT"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),  # notional sizing -> ATR not needed
    )
    return engine, account, expert, ctx, ps


def test_basket_expert_analyzed_once_per_bar_not_per_symbol():
    """A basket expert's ``analyze_as_of`` is called ONCE per bar (not once per universe
    symbol), and each item in the returned list persists its OWN ``ExpertRecommendation`` row
    keyed by the symbol in ``raw_outputs['symbol']`` -- proving the new ``analyzes_as_basket``
    dispatch path (Task 2) actually replaces the per-symbol loop for this expert, rather than
    merely tolerating a list return (which is what Task 1 showed crashes today)."""
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertRecommendation

    engine, account, expert, ctx, ps = _build_basket_run()
    try:
        engine.run()

        # ONE analyze_as_of call per bar -- NOT len(universe) times per bar (this stub has no
        # execution_schedule_enter_market setting, so every bar is an entry bar -- see
        # test_daily_engine_unit.py's equivalent "clock advanced once per bar" assertion).
        assert expert.call_count == len(BARS)

        rows = get_all_instances(ExpertRecommendation)
        # TWO rows per analysed bar (one per basket item), for every bar.
        assert len(rows) == 2 * len(BARS)

        by_bar: dict = {}
        for row in rows:
            by_bar.setdefault(row.created_at, []).append(row)
        assert len(by_bar) == len(BARS)
        for bar_rows in by_bar.values():
            assert len(bar_rows) == 2
            assert {r.symbol for r in bar_rows} == {"AAPL", "MSFT"}
    finally:
        ctx.__exit__(None, None, None)


def test_basket_expert_still_uses_classic_rm_and_ruleset(monkeypatch):
    """Unlike the BYPASS path (``bypasses_classic_rm``, e.g. FactorRanker), a basket expert
    KEEPS the full classic pipeline: ``TradeActionEvaluator`` evaluates the enter ruleset per
    symbol, and ``TradeRiskManagement`` sizes the resulting equity candidates -- a funded
    ``TradingOrder`` appears via the classic per-instrument-cap sizing logic, not a
    target-weight rebalance (contrast with test_daily_engine_bypass.py's
    ``test_bypass_expert_rebalances_and_rm_not_invoked``, which asserts the OPPOSITE: TAE/RM
    NEVER invoked for a bypass expert)."""
    import ba2_common.core.TradeActionEvaluator as TAE_mod
    import ba2_common.core.TradeRiskManagement as RM_mod

    engine, account, expert, ctx, ps = _build_basket_run(account_id=54, expert_id=54)
    try:
        tae_calls: list = []
        orig_tae_init = TAE_mod.TradeActionEvaluator.__init__

        def _spy_tae_init(self, *a, **kw):
            tae_calls.append((a, kw))
            return orig_tae_init(self, *a, **kw)

        monkeypatch.setattr(TAE_mod.TradeActionEvaluator, "__init__", _spy_tae_init, raising=True)

        rm_calls: list = []
        orig_size = RM_mod.TradeRiskManagement.size_candidate_orders

        def _spy_size(self, *a, **kw):
            rm_calls.append((a, kw))
            return orig_size(self, *a, **kw)

        monkeypatch.setattr(
            RM_mod.TradeRiskManagement, "size_candidate_orders", _spy_size, raising=True
        )

        engine.run()

        # TradeActionEvaluator WAS constructed (unlike the bypass path, which never touches it).
        assert len(tae_calls) >= 1
        # TradeRiskManagement candidate sizing WAS invoked (unlike the bypass path, which routes
        # straight to a FactorPortfolioManager rebalance and never sizes via the classic RM).
        assert len(rm_calls) >= 1

        # A funded order actually opened a position -- classic per-instrument-cap sizing, not a
        # target-weight rebalance (no `raw_outputs["targets"]` key involved anywhere here).
        positions = account.get_positions()
        assert len(positions) >= 1
        assert {p["symbol"] for p in positions} <= {"AAPL", "MSFT"}
        assert all(p["qty"] > 0 for p in positions)
    finally:
        ctx.__exit__(None, None, None)


def test_basket_expert_manage_open_positions_runs_when_entry_ok_false(monkeypatch):
    """Regression guard for the dispatch branch's OPEN_POSITIONS fall-through.

    UNLIKE the BYPASS branch (whose rebalance IS the management, so it legitimately
    ``continue``s past the shared ``manage_ok`` pass), a basket expert must NOT skip
    ``_manage_open_positions`` on a bar where only the MANAGE cadence fires (``entry_ok``
    False, ``manage_ok`` True) -- exactly like the classic per-symbol branch (which has no
    ``continue`` and always falls through to the shared ``if manage_ok:`` check). An earlier
    version of the ``analyzes_as_basket`` dispatch branch mirrored the bypass branch's
    ``continue`` verbatim, which silently skipped OPEN_POSITIONS management (Adjust-TP/SL/
    Close/Sell) for a basket expert on EVERY bar -- this test is the regression that would have
    caught it (it did not exist when that bug was introduced, which is exactly why it slipped
    through the original Task 2 test suite).

    Uses ``run_schedule_override``/``manage_schedule_override`` to decouple the two cadences:
    entry fires ONLY on the first bar (Tuesday 2024-01-02), management fires every day.
    """
    from app.services.backtest.daily_engine import DailyBacktestEngine

    engine, account, expert, ctx, ps = _build_basket_run(account_id=55, expert_id=55)
    try:
        # Entry cadence: Tuesday only -> entry_ok True on bar 1 (2024-01-02), False on every
        # later bar (Wed/Thu/Fri/Mon). Manage cadence: every day -> manage_ok True on ALL bars,
        # INCLUDING the later entry_ok=False bars this test targets.
        engine.config["run_schedule_override"] = {
            "days": {"monday": False, "tuesday": True, "wednesday": False, "thursday": False,
                     "friday": False, "saturday": False, "sunday": False},
        }
        engine.config["manage_schedule_override"] = {
            "days": {"monday": True, "tuesday": True, "wednesday": True, "thursday": True,
                     "friday": True, "saturday": True, "sunday": True},
        }

        manage_calls: list = []
        orig_manage = DailyBacktestEngine._manage_open_positions

        def _spy_manage(self, expert_arg, expert_id_arg, settings_arg, as_of_arg):
            manage_calls.append(as_of_arg)
            return orig_manage(self, expert_arg, expert_id_arg, settings_arg, as_of_arg)

        monkeypatch.setattr(
            DailyBacktestEngine, "_manage_open_positions", _spy_manage, raising=True
        )

        engine.run()

        # The entry pass (analyze_as_of) fires ONLY on the Tuesday bar.
        assert expert.call_count == 1

        # _manage_open_positions fires on EVERY bar (the manage cadence is unrestricted),
        # INCLUDING the later bars where entry_ok is False -- proving the basket branch falls
        # through to the shared manage_ok pass instead of `continue`-ing past it.
        assert len(manage_calls) == len(BARS)
    finally:
        ctx.__exit__(None, None, None)


def test_basket_expert_non_list_analyze_as_of_return_skips_bar_without_crashing():
    """Type-guard regression: a basket expert (``analyzes_as_basket = True``) whose
    ``analyze_as_of`` returns a single ``Recommendation`` instead of ``List[Recommendation]``
    must not crash ``engine.run()``.

    Without the guard, ``if not recs:`` does NOT catch this (a ``Recommendation`` dataclass
    instance is truthy), so ``for rec in recs:`` raises an uncaught ``TypeError``
    (``'Recommendation' object is not iterable``) that propagates out of
    ``_run_basket_expert_bar``/``engine.run()``, aborting the whole run -- reproducing, one
    layer up, the exact failure class Task 1 of the senate-basket-dispatch plan documented (a
    shape mismatch between what ``analyze_as_of`` returns and what the caller expects, left
    unguarded). This is a real risk for a future dual-mode ``analyze_as_of`` (Task 5's planned
    symbol-pinned vs. basket-mode branching) accidentally taking the wrong branch.
    """
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertRecommendation

    engine, account, expert, ctx, ps = _build_basket_run(
        account_id=56, expert_id=56, expert_cls=_StubBasketExpertReturnsSingleRec
    )
    try:
        # Must NOT raise -- the type guard logs + skips the bar instead of crashing the run.
        engine.run()

        # analyze_as_of was still called once per bar (the shape bug is in the RETURN value,
        # not the call itself) -- the crash Task 1 documented also called analyze_as_of before
        # dying on the caller's consumption of the return value.
        assert expert.call_count == len(BARS)

        # Every bar was skipped cleanly: zero ExpertRecommendation rows, no position opened.
        assert get_all_instances(ExpertRecommendation) == []
        assert account.get_positions() == []

        # The run still completed all bars (one equity snapshot per bar) -- proof the bad-shape
        # bar was skipped, not that the run silently stopped partway through.
        assert len(account.get_balance_history()) == len(BARS)
    finally:
        ctx.__exit__(None, None, None)
