"""Entry-bracket engine tests (Task 2 of the entry-time TP/SL bracket plan).

An entry-rule bracket (``adjust_take_profit``/``adjust_stop_loss`` actions on the
ENTER_MARKET rule) attaches its protective leg via ``TradeActionEvaluator``'s
Phase 1.5, which runs BEFORE the risk manager sizes the entry order — so at leg
creation time the entry order is still PENDING with ``quantity == 0`` and
``BacktestAccount._replace_leg`` naively copies that 0 onto the leg. This file
proves the leg is re-synced to the entry's REAL sized quantity at the
WAITING_TRIGGER -> ACCEPTED promotion (which runs exactly when the parent entry
order reaches FILLED), so the bracket closes the whole position instead of 0
shares.

This file grows in later tasks of the same plan (RM-safeguard vs ruleset-SL
precedence, end-to-end config wiring) — see
``docs/plans/2026-07-03-entry-tp-sl-bracket-actions.md``.

Task 3 of the same plan (below): the classic RM's safeguard stop (``order.stop_price``,
synthesized by ``TradeRiskManagement`` in ``risk_atr`` sizing mode when the strategy sets
none) and an entry-bracket ruleset SL (``Transaction.stop_loss``, attached by Phase 2's
``adjust_stop_loss`` action BEFORE the RM sizes the order) can now legitimately coexist —
``daily_engine._size_and_submit`` must make the TIGHTER of the two win rather than letting
the safeguard silently clobber a tighter strategy stop (or vice versa).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_entry_bracket_engine.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

# No slippage / no commission so price/quantity assertions are exact.
CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

D1 = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)
D3 = datetime(2024, 1, 4)


class _ConfigWiredStubExpert(MarketExpertInterface):
    """Module-level (constructible as ``expert_cls(expert_id)``, matching
    ``daily_backtest_handler._build_experts``'s plain-construction contract) BUY-every-bar stub.

    Used ONLY by ``test_config_entry_rules_reach_build_experts_and_set_stop_loss`` (Task 6) to
    prove ``config['entry_rules']`` reaches the REAL ``_build_experts`` ->
    ``seed_ruleset_from_tree(entry_actions=...)`` wiring end-to-end — not just the seeder in
    isolation (Task 4/5's ``test_entry_bracket_seeding.py``)."""

    @classmethod
    def description(cls) -> str:
        return "Stub expert for the entry_rules config-wiring test (Task 6)."

    @classmethod
    def get_settings_definitions(cls) -> dict:
        # No decision settings of its own (no ``_SETTING_KEYS`` either) — _build_experts'
        # _expert_decision_settings just needs a dict (not None) back from this call.
        return {}

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(
            signal=OrderRecommendation.BUY,
            confidence=80.0,
            current_price=100.0,
            details="config-wiring test buy",
            expected_profit_percent=10.0,
        )


def _bars(rows):
    """rows: list of (date, open, high, low, close) -> OHLCV row dicts."""
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


def _acct(rows, cfg=CFG, symbol="AAPL", account_id=1):
    """Build a wired BacktestAccount over a fresh per-run backtest DB + hand-built bars.

    Mirrors ``test_backtest_account_fills.py``'s ``_acct`` helper (the existing
    OCO/fill-test fixture pattern). Returns (account, db_context, price_source);
    the caller MUST close the context.
    """
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
    )
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"entry-bracket-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(rows))
    acct = BacktestAccount(account_id, ps, cfg)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ctx, ps


def test_bracket_closes_full_sized_position_even_if_set_before_sizing():
    """The lean bracket closes the FULL filled quantity even when TP/SL were recorded while the
    entry was still PENDING qty=0 (entry-rule bracket, sized by the RM afterwards). Because the
    exit reads the actual held NET position at close time, it can never close 0 (or a stale) qty —
    the invariant the old WAITING_TRIGGER qty-sync guarded, now true by construction."""
    from ba2_common.core.types import OrderDirection, OrderType, OrderStatus, OrderOpenType
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.db import add_instance, get_instance, update_instance

    acct, ctx, ps = _acct(
        [(D1, 100, 101, 99, 100), (D2, 102, 103, 101, 102), (D3, 104, 107, 103, 106)]
    )
    try:
        ps.set_clock(D1)
        # 1. Entry PENDING qty=0 + its transaction (Phase 1.5 analog, BEFORE the RM sizes it).
        entry = TradingOrder(
            account_id=1, symbol="AAPL", side=OrderDirection.BUY, quantity=0.0,
            order_type=OrderType.MARKET, status=OrderStatus.PENDING,
            open_type=OrderOpenType.AUTOMATIC, comment="entry-bracket-test",
            created_at=datetime.now(timezone.utc))
        entry_id = add_instance(entry)
        entry = get_instance(TradingOrder, entry_id)
        acct._create_transaction_for_order(entry)
        update_instance(entry)
        txn = get_instance(Transaction, entry.transaction_id)

        # 2. Record TP/SL while the entry is still qty=0 — stored on the transaction, no leg order.
        assert acct.adjust_tp_sl(txn, new_tp_price=106.0, new_sl_price=90.0) is True
        assert [o for o in acct.get_orders() if o.depends_on_order == entry.id] == []

        # 3. RM sizes the entry (quantity=7), submit + fill on the next bar.
        entry.quantity = 7.0
        update_instance(entry)
        acct.submit_order(entry)
        ps.set_clock(D1)
        acct.refresh_orders()
        acct.refresh_transactions()  # entry fills 7 @102, WAITING -> OPENED
        assert acct.get_order(entry.broker_order_id).filled_qty == 7

        # 4. Bar D3 crosses TP@106 -> the bracket closes the FULL 7 shares.
        ps.set_clock(D2)
        acct.refresh_orders()
        acct.refresh_transactions()
        assert acct.get_positions() == []  # fully flat (not 0-share, not partial)
        rts = acct.get_round_trip_trades()
        assert len(rts) == 1
        assert rts[0]["size"] == 7.0
        assert rts[0]["exit_reason"] == "take_profit"
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Task 3: RM safeguard vs ruleset entry-bracket SL — tighter-wins at submit.
#
# NOTE ON TEST STYLE (deviation from the plan's literal fixture-reuse suggestion): the
# plan's example fixture (``bt_engine_fixture``) doesn't exist; the closest reusable
# thing is ``test_daily_engine_unit.py``'s ``_build_run`` (drives a FULL bar-by-bar
# ``engine.run()`` through the real ``TradeActionEvaluator`` + stub ``analyze_as_of``).
# Reusing that verbatim would require the entry-bracket ruleset to ALSO be wired through
# ``seed_ruleset_from_tree(..., entry_sl_percent=...)`` and would leave the RM's safeguard
# stop price to a chain of as_of/fill-timing-dependent references (ORDER_OPEN_PRICE for the
# bracket vs the RM's own current-price lookup) that are hard to pin down deterministically
# without duplicating a lot of the engine's own machinery in the test.
#
# Instead these tests build the exact PENDING-entry + Transaction state Phase 1.5 leaves
# behind (mirroring ``test_waiting_leg_quantity_syncs_to_parent_fill`` above: manually
# ``add_instance`` the PENDING qty=0 entry order, call the REAL
# ``account._create_transaction_for_order``, then call the REAL ``account.adjust_sl`` to
# attach the ruleset's bracket SL to the transaction — exactly what the entry rule's
# ``adjust_stop_loss`` action does), configure the expert for deterministic ``risk_atr``
# sizing (ATR disabled -> the safeguard is purely ``risk_per_trade_pct`` floored at
# ``min_stop_loss_pct``, both set to 8% here so the safeguard is EXACTLY -8% of the $100
# current price = $92), and then drive the REAL ``DailyBacktestEngine._size_and_submit``
# (the method under test, unchanged) end-to-end. This proves the actual precedence code
# added to ``daily_engine.py``, not a reimplementation of it, while sidestepping the
# fragile timing/reference-price wiring a full ``engine.run()`` would need.
# ---------------------------------------------------------------------------

def _precedence_setup(account_id: int, expert_id: int, ruleset_sl_price: "float | None"):
    """Wire an engine + a PENDING qty=0 entry order with a REAL Transaction, optionally
    carrying a pre-attached entry-bracket SL (``Transaction.stop_loss``) — the exact state
    Phase 2's ``adjust_stop_loss`` action leaves behind BEFORE the classic RM sizes the
    order. The expert is configured for ``risk_atr`` sizing with ATR disabled and
    risk_per_trade_pct == min_stop_loss_pct == 8%, so ``synthesize_safeguard_stop`` always
    produces a deterministic safeguard of exactly -8% off the $100 current price ($92 for a
    long). Returns (engine, account, transaction_id, ctx); caller MUST close ctx.
    """
    from app.services.backtest.backtest_db import seed_expert_instance
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.daily_engine import (
        DailyBacktestEngine,
        _recommendation_to_expert_recommendation,
    )
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.types import (
        OrderDirection, OrderType, OrderStatus, OrderOpenType,
        OrderRecommendation, Recommendation,
    )
    from ba2_common.core.models import TradingOrder, Transaction
    from ba2_common.core.db import add_instance, get_instance, update_instance

    cfg = {
        "starting_cash": 100_000.0,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }
    as_of = datetime(2024, 1, 2)

    # Reuse _acct's bootstrap (wire_backtest_seams -> backtest_trading_db ->
    # seed_account_definition -> AsOfPriceSource -> BacktestAccount -> register_account)
    # instead of re-implementing it here.
    account, ctx, ps = _acct([(as_of, 100, 101, 99, 100)], cfg=cfg, account_id=account_id)
    ps.set_clock(as_of)
    resolver = wire_backtest_seams()  # idempotent, same resolver _acct already registered on

    ruleset_id = seed_enter_long_ruleset()

    class _PrecedenceStubExpert(MarketExpertInterface):
        @classmethod
        def description(cls) -> str:
            return "Stub expert for the SL-precedence tests."

        def render_market_analysis(self, market_analysis) -> str:
            return ""

        def run_analysis(self, symbol: str, market_analysis) -> None:
            return None

    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_PrecedenceStubExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    expert = _PrecedenceStubExpert(expert_id)
    expert.save_settings(
        {
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        }
    )
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": as_of,
        "end_date": as_of,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, {}, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=None,
    )

    # Persist the BUY recommendation + PENDING qty=0 entry order — the Phase-1/1.5 analog
    # (TradeActionEvaluator's BUY action creates exactly this row before the RM sizes it).
    rec = Recommendation(
        signal=OrderRecommendation.BUY,
        confidence=80.0,
        current_price=100.0,
        details="sl-precedence test",
        expected_profit_percent=10.0,
    )
    rec_id = _recommendation_to_expert_recommendation(
        rec, expert_instance_id=expert_id, symbol="AAPL", as_of=as_of,
    )
    entry = TradingOrder(
        account_id=account_id,
        symbol="AAPL",
        side=OrderDirection.BUY,
        quantity=0.0,
        order_type=OrderType.MARKET,
        status=OrderStatus.PENDING,
        open_type=OrderOpenType.AUTOMATIC,
        expert_recommendation_id=rec_id,
        comment="entry-bracket-precedence-test",
        created_at=as_of,
    )
    entry_id = add_instance(entry)
    entry = get_instance(TradingOrder, entry_id)
    account._create_transaction_for_order(entry)
    update_instance(entry)

    # Simulate the entry-bracket rule's adjust_stop_loss action (Phase 2) having already
    # attached the ruleset SL to the transaction, via the REAL adjust_sl (creates the
    # WAITING_TRIGGER SL leg + sets Transaction.stop_loss — exactly what the rule's action
    # does), when a bracket SL is given for this scenario.
    if ruleset_sl_price is not None:
        txn = get_instance(Transaction, entry.transaction_id)
        assert account.adjust_sl(txn, ruleset_sl_price, source="entry_bracket") is True

    return engine, account, entry.transaction_id, ctx


def test_ruleset_sl_tighter_than_safeguard_wins():
    """Entry bracket SL at -3% (tighter) vs RM safeguard at -8%: the submitted
    protective stop must be the -3% one (transaction.stop_loss unchanged)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    ruleset_sl = 97.0  # -3% off the $100 current price -> tighter than the -8% safeguard.
    engine, account, txn_id, ctx = _precedence_setup(
        account_id=201, expert_id=201, ruleset_sl_price=ruleset_sl,
    )
    try:
        engine._size_and_submit(201, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert txn.stop_loss == pytest.approx(ruleset_sl)
    finally:
        ctx.__exit__(None, None, None)


def test_safeguard_tighter_than_ruleset_sl_wins():
    """Entry bracket SL at -15% (looser) vs RM safeguard at -8%: the safeguard
    replaces it (long: max of the two stop prices)."""
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    ruleset_sl = 85.0  # -15% off the $100 current price -> looser than the -8% safeguard.
    engine, account, txn_id, ctx = _precedence_setup(
        account_id=202, expert_id=202, ruleset_sl_price=ruleset_sl,
    )
    try:
        engine._size_and_submit(202, indicator_provider=None, as_of_dt=datetime(2024, 1, 2))
        txn = get_instance(Transaction, txn_id)
        assert txn.stop_loss == pytest.approx(92.0)  # $100 - 8% RM safeguard
    finally:
        ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Task 6: config['entry_rules'] wired through daily_backtest_handler._build_experts.
# ---------------------------------------------------------------------------
def test_config_entry_rules_reach_build_experts_and_set_stop_loss(monkeypatch):
    """A config carrying ``entry_rules=[{adjust_stop_loss...}]`` reaches the REAL
    ``daily_backtest_handler._build_experts`` -> ``seed_ruleset_from_tree(entry_actions=...)``
    wiring, and the seeded action attaches ``Transaction.stop_loss`` once the entry fills.
    The leg-qty-sync (Task 2) and RM-safeguard-precedence (Task 3) mechanics are already
    proven above by the two tests just before this one; this test's only job is the
    config-to-engine plumbing Task 6 adds (daily_backtest_handler/optimizer forwarding
    ``entry_rules`` into ``_build_experts``)."""
    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.types import OrderDirection, OrderStatus
    from ba2_common.core.models import Transaction
    from ba2_common.core.db import get_instance

    # Register the module-level stub under a fake "supported expert" name so _build_experts'
    # real importlib-based construction path runs against it (no live provider/API key needed).
    monkeypatch.setitem(
        H._SUPPORTED_EXPERTS, "_ConfigWiredStubExpert",
        "tests.backtest.test_entry_bracket_engine",
    )

    account_id = 301
    account, ctx, ps = _acct(
        [
            (D1, 100, 101, 99, 100),
            (D2, 102, 103, 101, 102),
            (D3, 104, 107, 103, 106),
        ],
        account_id=account_id,
    )
    try:
        ps.set_clock(D1)
        resolver = wire_backtest_seams()  # idempotent, same resolver _acct already registered on

        config = {
            "experts": ["_ConfigWiredStubExpert"],
            "enabled_instruments": ["AAPL"],
            "entry_rules": [
                {"id": "e_sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": -5.0},
            ],
        }
        built = H._build_experts(config, resolver, account_id)
        assert len(built) == 1
        expert, expert_id, decision_settings, ruleset_id = built[0]

        engine = DailyBacktestEngine(
            account=account,
            experts=[(expert, expert_id, decision_settings, ruleset_id)],
            price_source=ps,
            config={
                "start_date": D1, "end_date": D3,
                "enabled_instruments": ["AAPL"], "seed": 42,
            },
            indicator_provider=None,  # notional sizing -> ATR not needed
        )
        # _ensure_safeguard_stop always attempts an ATR lookup now regardless of sizing_mode;
        # None is its documented no-ATR-available fallback, not "never touched".
        engine._indicator_provider = None
        engine.run()

        filled = [
            o for o in account.get_orders()
            if o.symbol == "AAPL" and o.side == OrderDirection.BUY
            and o.depends_on_order is None and o.status == OrderStatus.FILLED
        ]
        assert len(filled) == 1, "the entry order did not fill"
        entry = filled[0]
        txn = get_instance(Transaction, entry.transaction_id)
        # The adjust_stop_loss action runs in Phase 2, right after the entry order is
        # CREATED (still PENDING/unfilled) — so ``order_open_price`` falls back to the
        # CURRENT market price at evaluation time (D1's ~100, see TradeActions.py's
        # "falling back to current market price" path), not the eventual D2-open fill
        # price. -5% of 100 -> 95.0.
        assert txn.stop_loss == pytest.approx(95.0)
    finally:
        ctx.__exit__(None, None, None)


def test_config_entry_rules_explicitly_empty_seeds_zero_rules_not_default(monkeypatch):
    """Regression: ``entry_rules=[]`` (present, non-None, but every rule pruned) must seed a
    ruleset with ZERO EventActions -- NOT silently fall back to the generic bullish+flat
    default. ``decode_params`` only ever emits `[]` for a Strategy that HAS a unified-model
    entry_rules template which the GA pruned down to nothing (every rule/branch disabled); that
    is a deliberate "never enter" decision, not "not configured". Before the fix,
    ``_seed_enter``'s `if buy_tree or entry_action or entry_rules:` treated `[]` as falsy --
    indistinguishable from "no unified rules at all" -- so the fallback below fired and quietly
    re-armed the trial with an unrelated default ruleset, corrupting both the GA's fitness
    signal for that individual and any persisted top-N re-run (real trades recorded against a
    genome that had explicitly disabled every entry path). See the real incident this pins:
    scr-mid-FMPRating-S1-goal6's TOP1 (all four buy-N branches disabled) recorded 153 phantom
    trades instead of the zero its genome actually specified."""
    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_db
    from ba2_common.core.models import RulesetEventActionLink
    from sqlmodel import select

    monkeypatch.setitem(
        H._SUPPORTED_EXPERTS, "_ConfigWiredStubExpert",
        "tests.backtest.test_entry_bracket_engine",
    )

    account_id = 302
    account, ctx, ps = _acct([(D1, 100, 101, 99, 100)], account_id=account_id)
    try:
        ps.set_clock(D1)
        resolver = wire_backtest_seams()

        config = {
            "experts": ["_ConfigWiredStubExpert"],
            "enabled_instruments": ["AAPL"],
            "entry_rules": [],  # explicitly empty, NOT absent/None
        }
        built = H._build_experts(config, resolver, account_id)
        assert len(built) == 1
        _expert, _expert_id, _decision_settings, ruleset_id = built[0]

        with get_db() as session:
            links = session.exec(
                select(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id
                )
            ).all()
        assert links == [], (
            "entry_rules=[] must seed ZERO EventActions, not fall back to the default "
            "bullish+flat ruleset"
        )
    finally:
        ctx.__exit__(None, None, None)
