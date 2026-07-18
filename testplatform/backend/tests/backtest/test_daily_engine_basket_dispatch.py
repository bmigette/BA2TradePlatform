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


# ---------------------------------------------------------------------------
# Task 3 (senate-basket-dispatch plan): FMPSenateTraderCopy wired onto analyzes_as_basket for
# real -- proves the previously-dead backtest path (Task 1's crash, documented above) now
# actually produces ExpertRecommendation rows through the FULL engine, using a REAL expert
# instance (not a stub).
#
# This test intentionally lives here (testplatform/backend/tests/backtest/), NOT under
# packages/experts/tests/, even though the plan's Task 3 write-up suggested the latter as the
# default location. Reason (a directional-dependency + import-feasibility check, not a
# preference): packages/experts is a standalone installable package that testplatform/backend
# CONSUMES (see CLAUDE.md's Phase 6 packages note) -- the dependency only points one way. A
# test under packages/experts/tests/ cannot import app.services.backtest.daily_engine (the
# DailyBacktestEngine harness this test needs) because `app` is testplatform/backend's
# FastAPI-local package, not an installed dependency of the ba2_experts venv -- confirmed
# empirically: `python -c "import app.services.backtest.daily_engine"` raises
# ModuleNotFoundError when the cwd/rootdir is the repo root (or packages/experts), and only
# resolves when cwd is testplatform/backend itself. This matches the repo's own existing
# convention: every other engine-level integration test for a REAL ba2_experts class
# (FactorRanker, FMPEarningsDrift, FMPInsiderClusterBuy, FMPRating) already lives under
# testplatform/backend/tests/backtest/, not packages/experts/tests/ -- see
# test_daily_engine_bypass.py, test_backtest_perf_assertions.py, test_engine_golden_regression.py,
# test_size_candidate_orders_parity.py. A companion class-attribute-only check (cheap, no `app`
# dependency) was added to packages/experts/tests/test_senate_gather_process.py instead
# (test_copy_declares_analyzes_as_basket).
# ---------------------------------------------------------------------------
def _senate_trade(first, last, sym, ttype, disclose, exec_date, amount="$15,001 - $50,000"):
    """Hermetic FMP senate/house trade fixture row. Shape matches ``_copy_trade()`` in
    packages/experts/tests/test_senate_gather_process.py -- that file's docstring documents
    this as the repo's established convention for fabricating FMP trade data without hitting
    the network (test_tools/test_fmp_senate_copy.py, the other candidate reference named in
    the Task 3 plan text, turns out to be a pre-Phase-6 manual `--all` CLI harness against the
    OLD `ba2_trade_platform.modules.experts` import path and the real network/DB -- it does
    NOT mock _fetch_congress_trades or seed a disk cache, so it isn't a hermetic-fixture
    reference; test_senate_gather_process.py's _copy_expert()/_copy_trade() is the real one)."""
    return {
        "firstName": first, "lastName": last, "symbol": sym, "type": ttype,
        "disclosureDate": disclose, "transactionDate": exec_date, "amount": amount,
    }


class _FakeOHLCV:
    """OHLCV provider installed via ``set_backtest_ohlcv_override`` -- a real basket expert's
    ``_gather`` (unlike the stub experts above) calls ``providers.price_at_date(symbol,
    as_of)``, which resolves through ``LiveProviderBundle.ohlcv()`` -> the TradeConditions
    provider resolver -> this override, exactly the seam ``daily_backtest_handler.py`` uses
    for the real (network/disk-cache) OHLCV path -- see seam_wiring.py's
    ``_wire_provider_resolver`` docstring.

    Deliberately duplicates ``FakeOHLCV`` in packages/experts/tests/test_senate_gather_process.py
    (same shape) rather than importing it: this file cannot import from packages/experts/tests
    (that package's tests aren't on this venv's collected path in the same way _senate_trade's
    docstring explains for _copy_trade), and the directional-dependency constraint documented
    above the Task 3 test block means a real shared import isn't available either way."""

    def __init__(self, price_map):
        self._price_map = price_map  # symbol(upper) -> close price

    def get_ohlcv_data(self, symbol, end_date=None, lookback_days=7, interval="1d"):
        import pandas as pd
        price = self._price_map.get(str(symbol).upper())
        if price is None:
            return pd.DataFrame({"Close": []})
        return pd.DataFrame({"Close": [price]})


# Task 3's own fixture defaults for FMPSenateTraderCopy -- module-level (rather than inlined
# in _build_real_copy_run) so they can be referenced as the function's explicit defaults.
_COPY_SENATE_TRADES_DEFAULT = [
    _senate_trade("Nancy", "Pelosi", "AAPL", "purchase", "2023-12-15", "2023-12-01"),
]
_COPY_HOUSE_TRADES_DEFAULT = [
    _senate_trade("Josh", "Gottheimer", "MSFT", "purchase", "2023-12-15", "2023-12-01"),
]
# copy_trade_names has NO usable declared default ("" -- nobody to copy, a real per-expert
# decision, see daily_backtest_handler.py's _expert_decision_settings docstring) -- must be
# supplied explicitly, matching what a real backtest run's payload override would provide.
_COPY_SETTINGS_DEFAULT = {
    "copy_trade_names": "Nancy Pelosi, Josh Gottheimer",
    "max_disclose_date_days": 365,
    "max_trade_exec_days": 365,
}


def _build_real_copy_run(
    account_id=57,
    expert_id=57,
    expert_cls=None,
    expert_class_name=None,
    senate_trades=None,
    house_trades=None,
    settings=None,
    price_map=None,
):
    """Wire a REAL expert instance (not a stub) into the same harness the stub basket tests
    above use. Defaults to ``FMPSenateTraderCopy`` with Task 3's original fixture data/settings
    (so the existing call site stays byte-identical); pass ``expert_cls``/``senate_trades``/
    ``house_trades``/``settings``/``price_map`` to reuse this harness for a DIFFERENT real
    basket expert (e.g. a future ``FMPSenateTraderWeight`` engine-integration test) instead of
    copy-pasting this ~100-line function.

    Hermetic: ``_fetch_senate_trades``/``_fetch_house_trades`` are monkeypatched directly on
    the instance (mirrors ``_copy_expert()`` in test_senate_gather_process.py -- the FMP-http
    fetchers aren't in the ``get_provider`` registry per that file's docstring, so they're
    stubbed on the instance rather than through the provider seam), and
    ``providers.price_at_date`` is routed through ``_FakeOHLCV`` via
    ``set_backtest_ohlcv_override`` instead of the network.

    Args:
        expert_cls: the ba2_experts class to construct (default ``FMPSenateTraderCopy``). Any
            class with the ``_fetch_senate_trades``/``_fetch_house_trades`` instance-attribute
            override points and an ``analyzes_as_basket = True`` marker works here.
        expert_class_name: the ``ExpertInstance.expert`` string seeded into the DB (default:
            ``expert_cls.__name__``). Exposed separately from ``expert_cls`` only for the
            unlikely case a caller wants to seed under a different registered name.
        senate_trades / house_trades: hermetic fixture rows returned by the monkeypatched
            fetchers (default: Task 3's Nancy Pelosi/AAPL + Josh Gottheimer/MSFT fixtures).
        settings: the per-expert decision-settings dict fed into the engine's ``experts`` tuple
            (default: Task 3's ``copy_trade_names``/age-window settings). A different expert's
            ``_SETTING_KEYS`` will need a different dict here.
        price_map: symbol(upper) -> close price fed to ``_FakeOHLCV`` (default: AAPL/MSFT at
            Task 3's original prices).

    Returns (engine, account, expert, ctx, ps). Caller MUST call
    ``set_backtest_ohlcv_override(None)`` AND close ``ctx`` in a ``finally`` block.
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
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams

    if expert_cls is None:
        from ba2_experts.FMPSenateTraderCopy import FMPSenateTraderCopy
        expert_cls = FMPSenateTraderCopy
    expert_class_name = expert_class_name or expert_cls.__name__
    senate_trades = _COPY_SENATE_TRADES_DEFAULT if senate_trades is None else senate_trades
    house_trades = _COPY_HOUSE_TRADES_DEFAULT if house_trades is None else house_trades
    settings = dict(_COPY_SETTINGS_DEFAULT if settings is None else settings)
    price_map = {"AAPL": 100.0, "MSFT": 50.0} if price_map is None else price_map

    cfg = {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"engine-basket-dispatch-real-copy-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    ruleset_id = seed_enter_long_ruleset(name="backtest-basket-dispatch-real-copy")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name=expert_class_name,
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BARS))
    ps.load_bars("MSFT", _bar_rows(BARS))

    account = BacktestAccount(account_id, ps, cfg)
    resolver.register_account(account_id, account)

    # Real constructor: __init__ runs _load_expert_instance (needs the seeded row above) and
    # _get_fmp_api_key (warns, does not raise, when unconfigured -- fine, since we bypass the
    # network fetcher this pins over entirely).
    expert = expert_cls(expert_id)
    # Hermetic fetch: fixed disclosures, well before the BARS window (2024-01-02..01-08), so
    # every bar's no-lookahead / age-window checks pass identically across the whole run.
    expert._fetch_senate_trades = lambda symbol=None: senate_trades
    expert._fetch_house_trades = lambda symbol=None: house_trades
    # Enable automated opening + buy (interface defaults are False/True; the RM gates on
    # these) -- same settings the stub basket tests above need to actually fund an order.
    expert.save_settings(
        {
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
        }
    )
    resolver.register_expert(expert_id, expert)

    set_backtest_ohlcv_override(_FakeOHLCV(price_map))

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL", "MSFT"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, settings, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),  # notional sizing -> ATR not needed
    )
    return engine, account, expert, ctx, ps


def test_fmp_senate_trader_copy_produces_recommendations_in_backtest():
    """Was broken before Task 2/3 (zero recommendations -- Task 1's test above documents the
    real pre-fix crash: an uncaught AttributeError out of engine.run() after the first
    symbol). Now: FMPSenateTraderCopy.analyzes_as_basket routes it through
    _run_basket_expert_bar (one real analyze_as_of call per bar), and ExpertRecommendation
    rows appear for the symbols with qualifying copy-trades (AAPL via Nancy Pelosi, MSFT via
    Josh Gottheimer)."""
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertRecommendation

    engine, account, expert, ctx, ps = _build_real_copy_run()
    try:
        engine.run()

        rows = get_all_instances(ExpertRecommendation)
        assert len(rows) > 0, (
            "FMPSenateTraderCopy produced zero ExpertRecommendation rows -- the dead "
            "backtest path Task 1 documented is still dead"
        )
        symbols = {r.symbol for r in rows}
        assert symbols <= {"AAPL", "MSFT"}

        # A funded order actually opened a position -- proves the recommendations flowed all
        # the way through TradeActionEvaluator + TradeRiskManagement sizing (the full classic
        # pipeline a basket expert keeps, per Task 2), not merely persisted-then-ignored.
        positions = account.get_positions()
        assert len(positions) >= 1
        assert {p["symbol"] for p in positions} <= {"AAPL", "MSFT"}
    finally:
        set_backtest_ohlcv_override(None)
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Task 5 (senate-basket-dispatch plan): FMPSenateTraderWeight wired onto the SAME
# analyzes_as_basket dispatch mode Task 2/3 built, reusing Task 3's parameterized
# _build_real_copy_run harness (expert_cls/senate_trades/house_trades/settings/price_map are
# all overridable specifically so this test doesn't need to copy-paste that ~100-line
# function -- see _build_real_copy_run's own docstring above, which calls this out by name).
#
# Weight needs TWO extra hermetic stubs beyond what Copy needs: _get_price_at_date and
# _fetch_trader_history. Copy's _process never resolves a per-trade EXECUTION price (it only
# compares disclosure/exec dates for its age filter, no price-delta logic) and has no notion
# of "trader history" at all, so _build_real_copy_run never stubs either method. Discovered
# empirically running this test (not just reasoned about): this harness's DB actually SEEDS an
# FMP_API_KEY app setting ("test-fmp-key", unlike packages/experts/tests' bare __new__
# fixtures, which have no API key at all) -- so, left unstubbed, BOTH methods fall through to
# REAL network calls against FMP with that fake key, each one taking 1-2s to 401 rather than
# failing fast, and _fetch_trader_history additionally returns [] (empty history) on every
# call, which makes _calculate_trader_confidence's symbol_focus_pct resolve to 0 for every
# trader (no "yearly activity" to compute a focus % from) -- collapsing net_symbol_focus to
# exactly 0 and producing a HOLD signal (silently, no crash) instead of the intended BUY.
# Weight's _filter_trades ALSO needs a per-trade execution price (pre-resolved by _gather_all
# into exec_price_by_trade via _get_price_at_date) for the price-delta filter and trade_details
# -- a None exec price would make _filter_trades' `if not exec_price: continue` guard drop
# every trade. Both stubs mirror the SAME hermetic stubs the per-symbol fixtures in
# packages/experts/tests/test_senate_gather_process.py already apply for the identical reasons
# (see that file's `_weight_expert`).
# ---------------------------------------------------------------------------
_WEIGHT_SENATE_TRADES_DEFAULT = [
    _senate_trade("Alice", "Aa", "AAPL", "purchase", "2023-12-15", "2023-12-01"),
    _senate_trade("Bob", "Bb", "AAPL", "purchase", "2023-12-16", "2023-12-02"),
]
_WEIGHT_HOUSE_TRADES_DEFAULT = [
    _senate_trade("Carol", "Cc", "MSFT", "purchase", "2023-12-15", "2023-12-01"),
    _senate_trade("Dave", "Dd", "MSFT", "purchase", "2023-12-16", "2023-12-02"),
]
# Each trader's OWN "yearly history" for the confidence-modifier calc: fixed to a single past
# buy of the SAME symbol they're disclosed trading above, so their whole yearly activity is
# this symbol (100% focus, capped by symbol_focus_cap_pct) -- otherwise (see the comment block
# above) an unstubbed/empty history collapses every trader's symbol-focus % to 0 and the net
# signal to a silent HOLD.
_WEIGHT_HISTORY_DEFAULT = {
    "Alice Aa": [_senate_trade("Alice", "Aa", "AAPL", "purchase", "2023-11-01", "2023-10-20")],
    "Bob Bb": [_senate_trade("Bob", "Bb", "AAPL", "purchase", "2023-11-02", "2023-10-21")],
    "Carol Cc": [_senate_trade("Carol", "Cc", "MSFT", "purchase", "2023-11-01", "2023-10-20")],
    "Dave Dd": [_senate_trade("Dave", "Dd", "MSFT", "purchase", "2023-11-02", "2023-10-21")],
}
# copy_trade_names has no equivalent here -- Weight's REQUIRED (direct dict access, no
# _setting_or_default) settings keys are max_disclose_date_days/max_trade_exec_days/
# max_trade_price_delta_pct/growth_confidence_multiplier/confidence_to_profit_factor/
# min_traders/min_trades (see FMPSenateTraderWeight._process). min_traders=min_trades=2
# matches the two-distinct-traders-per-symbol fixture data above so a real BUY signal (not a
# below-minimums HOLD) comes out for each symbol.
_WEIGHT_SETTINGS_DEFAULT = {
    "max_disclose_date_days": 365,
    "max_trade_exec_days": 365,
    "max_trade_price_delta_pct": 1000.0,
    "growth_confidence_multiplier": 5.0,
    "confidence_to_profit_factor": 0.15,
    "min_traders": 2,
    "min_trades": 2,
    # Disabled so this hermetic engine-level test never touches the real on-disk skill/
    # hold-days scoring cache (packages/experts/tests isolates CACHE_FOLDER per test via an
    # autouse fixture; this test doesn't, so keeping these knobs at their "off" default value
    # keeps the run hermetic without needing that isolation fixture here too).
    "skill_signal_weight": 0.0,
    "skill_confidence_weight": 0.0,
}
_WEIGHT_EXEC_PRICE_BY_SYMBOL = {"AAPL": 95.0, "MSFT": 57.0}


def test_fmp_senate_trader_weight_produces_recommendations_in_backtest():
    """Mirrors test_fmp_senate_trader_copy_produces_recommendations_in_backtest above, but for
    FMPSenateTraderWeight: proves the SAME analyzes_as_basket dispatch mode (Task 2) also
    carries Weight's basket path (Task 5's _gather_all/_process_all + analyze_as_of dual-mode
    dispatch) end-to-end through a real DailyBacktestEngine run -- one analyze_as_of call per
    bar, ExpertRecommendation rows for the symbols with qualifying (min_traders/min_trades
    -satisfying) disclosures, and a funded order via the full classic
    TradeActionEvaluator/TradeRiskManagement pipeline (not a target-weight rebalance)."""
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override
    from ba2_common.core.db import get_all_instances
    from ba2_common.core.models import ExpertRecommendation
    from ba2_experts.FMPSenateTraderWeight import FMPSenateTraderWeight

    engine, account, expert, ctx, ps = _build_real_copy_run(
        account_id=58, expert_id=58, expert_cls=FMPSenateTraderWeight,
        senate_trades=_WEIGHT_SENATE_TRADES_DEFAULT,
        house_trades=_WEIGHT_HOUSE_TRADES_DEFAULT,
        settings=_WEIGHT_SETTINGS_DEFAULT,
        price_map={"AAPL": 100.0, "MSFT": 60.0},
    )
    # Extra hermetic stubs Weight needs beyond what Copy needs -- see the block comment above.
    expert._get_price_at_date = lambda sym, date: _WEIGHT_EXEC_PRICE_BY_SYMBOL.get(
        str(sym).upper(), 50.0)
    expert._fetch_trader_history = lambda name: _WEIGHT_HISTORY_DEFAULT.get(name)
    try:
        engine.run()

        rows = get_all_instances(ExpertRecommendation)
        assert len(rows) > 0, (
            "FMPSenateTraderWeight produced zero ExpertRecommendation rows via the basket "
            "dispatch path"
        )
        symbols = {r.symbol for r in rows}
        assert symbols <= {"AAPL", "MSFT"}

        # A funded order actually opened a position -- proves the recommendations flowed all
        # the way through TradeActionEvaluator + TradeRiskManagement sizing (the full classic
        # pipeline a basket expert keeps, per Task 2), not merely persisted-then-ignored.
        positions = account.get_positions()
        assert len(positions) >= 1
        assert {p["symbol"] for p in positions} <= {"AAPL", "MSFT"}
    finally:
        set_backtest_ohlcv_override(None)
        ctx.__exit__(None, None, None)
