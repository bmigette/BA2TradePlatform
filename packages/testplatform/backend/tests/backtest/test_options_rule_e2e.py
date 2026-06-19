"""Phase 2 Task 7: RULE-DRIVEN OPTIONS backtest end-to-end through ``DailyBacktestEngine.run()``.

Unlike ``test_options_e2e.py`` (which submits a long call DIRECTLY on the account before the
run and only proves the FILL/MARK/EXPIRY engine plumbing), this test proves the FULL
rule-driven chain — the path the optimizer/strategy actually exercises:

    strategy OPTION exit rule (action: buy_call, option_strike_method/param/dte/sizing)
      -> seed_open_positions_ruleset  (shared action_from_rule maps it to an EventAction
                                       whose action config carries the option selection params)
      -> the engine's _manage_open_positions evaluates that OPEN_POSITIONS ruleset on the
         analysis cadence for every HELD position
      -> TradeActionEvaluator builds the option TradeAction (BuyCallAction) from the action
         config and forwards strike_method/strike_param/dte_min/dte_max/sizing to its ctor
      -> the action fetches the chain from the options-capable BacktestAccount (off the
         fixture OptionsHistoryCache via HistoricalOptionsProvider), selects a contract, and
         submits an option order
      -> the BacktestAccount fills that order off the cached premium bar
      => an OPTION position exists at the end of the run.

To make _manage_open_positions evaluate at all, the expert must HOLD a position: the expert
returns BUY so the enter ruleset opens an AAPL EQUITY long on the first bar (fills next bar);
from then on the always-fire open_positions rule buys a call against the held name.

THE GAP this test surfaced (and the minimum fix): the option-entry action computed its
DTE/expiry window from the WALL CLOCK (``date.today()``), so against historical fixture data
(a 2024 contract) the chain was filtered to empty and the option entry never fired (and it
would have leaked look-ahead in any real run). The fix anchors ``_OptionEntryAction._today()``
on the account's simulated bar date when the account exposes one (the BacktestAccount's
``_as_of_date()``), duck-typed so LIVE behaviour is byte-identical (no ``_as_of_date`` ->
``date.today()``). See ba2_common/core/TradeActions.py::_OptionEntryAction._today.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_options_rule_e2e.py -q
"""
from __future__ import annotations

from datetime import date, datetime

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation


# --------------------------------------------------------------------------- #
# Fixture data — ONE underlying (AAPL) with a daily OHLCV climb, plus a CALL chain snapshot
# (dated at run start) holding a couple of strikes, and per-contract premium bars over the hold.
# The chain expiry (2024-03-15) sits inside the rule's DTE window measured from the SIMULATED
# clock (run start 2024-02-01) — proving the as-of-anchored selection (NOT wall clock).
# --------------------------------------------------------------------------- #
_EXPIRY = date(2024, 3, 15)
_OCC_180 = "AAPL240315C00180000"   # strike 180 call (the ~ATM strike the rule selects)
_OCC_190 = "AAPL240315C00190000"   # strike 190 call (a farther-OTM alternative)

START = datetime(2024, 2, 1)
END = datetime(2024, 2, 7)

#                       (date,            open, high, low,  close)  weekday
_AAPL_BARS = [
    (date(2024, 2, 1), 180, 181, 179, 180),   # Thu -> entry/analysis bar (clock starts)
    (date(2024, 2, 2), 181, 183, 180, 182),   # Fri -> equity BUY fills @ open 181
    (date(2024, 2, 5), 183, 186, 182, 185),   # Mon -> held; open_positions rule buys a call
    (date(2024, 2, 6), 186, 189, 185, 188),   # Tue -> call fills @ premium bar open
    (date(2024, 2, 7), 189, 192, 188, 191),   # Wed -> held / marked
]

# Per-contract premium bars (only the strike the rule selects needs bars to FILL; we seed both
# so a marking read never KeyErrors). Premium rises across the hold.
#                    (date,            open, high, low, close)
_PREMIUM_180 = [
    (date(2024, 2, 2), 4.0, 4.4, 3.8, 4.2),
    (date(2024, 2, 5), 4.2, 4.8, 4.1, 4.6),
    (date(2024, 2, 6), 4.6, 5.2, 4.5, 5.0),   # the call fills next_bar_open here (open 4.6)
    (date(2024, 2, 7), 5.0, 5.6, 4.9, 5.4),
]
_PREMIUM_190 = [
    (date(2024, 2, 2), 1.5, 1.7, 1.4, 1.6),
    (date(2024, 2, 5), 1.6, 1.9, 1.5, 1.8),
    (date(2024, 2, 6), 1.8, 2.1, 1.7, 2.0),
    (date(2024, 2, 7), 2.0, 2.3, 1.9, 2.2),
]

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


class _HoldExpert(MarketExpertInterface):
    """Deterministic HOLD expert.

    The held EQUITY position is seeded DIRECTLY on the backtest DB before the run (a filled
    entry order + OPENED transaction, exactly like the round-trip / target-bracket harness), so
    we do NOT rely on the RM to size+open an entry. That isolates the test to the OPTION rule
    path: every analysis bar the expert returns HOLD, but ``_manage_open_positions`` still runs
    its OPEN_POSITIONS ruleset against the held name — and that ruleset's always-fire ``buy_call``
    rule is what we are validating. The OPEN_POSITIONS recommendation is persisted with
    ``allow_hold=True`` so the rule (empty triggers => always true) fires regardless of signal.
    """

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}  # no entry schedule -> every bar
        self.seen_as_of: list = []

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub HOLD expert for the rule-driven options engine e2e test."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.seen_as_of.append(as_of.date() if hasattr(as_of, "date") else as_of)
        # price_at_date is non-null on the persisted row -> resolve the simulated spot.
        try:
            price = context.account.get_instrument_current_price("AAPL")
        except Exception:  # noqa: BLE001
            price = None
        return Recommendation(
            signal=OrderRecommendation.HOLD,
            confidence=1.0,
            current_price=price if price is not None else 180.0,
            details="hold (position is managed by the open_positions rule)",
            raw_outputs={},
        )


def _seed_held_equity_position(account, expert_id, *, qty=100, entry_px=180.0, open_date=None):
    """Seed an OPENED AAPL equity transaction + its FILLED entry order on the backtest DB.

    Mirrors the round-trip / target-bracket harness: this gives ``_manage_open_positions`` a
    held position to evaluate the OPEN_POSITIONS ruleset against, WITHOUT depending on the RM to
    size/open an entry (which the unit-level test isolation does not exercise)."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction, TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus

    txn = Transaction(
        symbol="AAPL", quantity=qty, side=OrderDirection.BUY, open_price=entry_px,
        status=TransactionStatus.OPENED, expert_id=expert_id, open_date=open_date,
    )
    txn_id = add_instance(txn)
    entry = TradingOrder(
        account_id=account.id, symbol="AAPL", quantity=qty, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, open_price=entry_px,
        filled_qty=qty, transaction_id=txn_id, broker_order_id=account._next_broker_id(),
        comment="seeded entry",
    )
    add_instance(entry)
    return txn_id


def _underlying_rows(rows):
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


def _seed_cache(db_path: str) -> None:
    """Seed a 2-strike CALL chain (dated at run start) + per-contract premium bars over the hold."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        START.date().isoformat(),  # chain snapshot dated at run start (2024-02-01)
        [
            {"occ_symbol": _OCC_180, "option_type": "call", "strike": 180.0,
             "expiry": _EXPIRY.isoformat(), "bid": 4.0, "ask": 4.2, "last": 4.1, "iv": 0.30,
             "delta": 0.55, "open_interest": 5000},
            {"occ_symbol": _OCC_190, "option_type": "call", "strike": 190.0,
             "expiry": _EXPIRY.isoformat(), "bid": 1.5, "ask": 1.7, "last": 1.6, "iv": 0.28,
             "delta": 0.30, "open_interest": 5000},
        ],
    )
    bars = []
    for occ, prem, stk in ((_OCC_180, _PREMIUM_180, 180.0), (_OCC_190, _PREMIUM_190, 190.0)):
        for (d, o, h, low, c) in prem:
            bars.append({
                "occ_symbol": occ, "date": d.isoformat(), "open": o, "high": h, "low": low,
                "close": c, "volume": 400, "underlying": "AAPL", "option_type": "call",
                "strike": stk, "expiry": _EXPIRY.isoformat(),
            })
    cache.write_bar_rows(bars)


def _run_rule_driven_options():
    """Wire the full engine with an OPEN_POSITIONS rule whose action is ``buy_call``.

    Returns (engine, account, expert, ctx). Caller MUST close ctx.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_enter_long_ruleset,
        seed_open_positions_ruleset,
    )
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    account_id = 81
    expert_id = 81

    import tempfile, os
    tmpdir = tempfile.mkdtemp(prefix="opt-rule-e2e-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-rule-e2e")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    enter_ruleset_id = seed_enter_long_ruleset(name=f"opt-rule-enter-{account_id}")

    # THE rule under test: an OPTION exit/open-positions rule in the Strategy decode shape.
    # No conditions -> the rule ALWAYS fires (empty triggers => "always true" in the evaluator),
    # so the only thing gating it is "is a position held". action buy_call + the option_*
    # selection params (percent_otm 0% ~ ATM, 30..60 DTE, 5% sizing) flow through
    # action_from_rule -> the BuyCallAction ctor.
    exit_rules = [{
        "id": "opt-buy-call",
        "conditions": None,
        "action_type": "buy_call",
        "option_strike_method": "percent_otm",
        "option_strike_param": 0.0,    # ~ ATM (nearest spot strike); spot ~183 -> 180 strike
        "option_dte_min": 30,
        "option_dte_max": 60,
        "option_sizing": 5.0,          # 5% of virtual equity
        "enabled": True,
    }]
    open_ruleset_id = seed_open_positions_ruleset(exit_rules, name=f"opt-rule-open-{account_id}")

    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_HoldExpert",
        enter_market_ruleset_id=enter_ruleset_id,
        open_positions_ruleset_id=open_ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _underlying_rows(_AAPL_BARS))
    ps.set_clock(START)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)

    # Seed the held EQUITY long DIRECTLY (filled entry + OPENED txn) so _manage_open_positions
    # has a position to evaluate the open_positions ruleset against — isolating the test to the
    # rule-driven OPTION path (no dependency on the RM sizing/opening an entry).
    _seed_held_equity_position(account, expert_id, qty=100, entry_px=180.0, open_date=START)

    expert = _HoldExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, enter_ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),
    )
    return engine, account, expert, ctx, open_ruleset_id, expert_id


def test_open_positions_rule_fires_and_buys_an_option_off_the_cache():
    """END-TO-END: an OPEN_POSITIONS rule with action ``buy_call`` FIRES on a held position and
    its option order FILLS off the fixture cache -> an option position exists at the end."""
    engine, account, expert, ctx, open_ruleset_id, expert_id = _run_rule_driven_options()
    try:
        results = engine.run()

        # Sanity: the seeded equity long is held in the SAME DB source _manage_open_positions
        # queries (Transaction.status == OPENED) — so the open_positions ruleset had a position
        # to evaluate the buy_call rule against.
        from sqlmodel import Session, select
        from ba2_common.core.db import get_db
        from ba2_common.core.models import Transaction
        from ba2_common.core.types import OrderDirection, TransactionStatus
        with Session(get_db().bind) as s:
            held_equity = s.exec(
                select(Transaction).where(
                    Transaction.symbol == "AAPL",
                    Transaction.expert_id == expert_id,
                    Transaction.status == TransactionStatus.OPENED,
                    Transaction.side == OrderDirection.BUY,
                )
            ).all()
        assert held_equity, "expected the seeded AAPL equity long held so open_positions evaluates"

        # THE assertion: the rule-driven option path worked end-to-end — the buy_call rule
        # FIRED and its order FILLED off the cache, leaving a held option position.
        opt_positions = account.get_option_positions()
        assert opt_positions, (
            "expected the buy_call open_positions rule to have FIRED and FILLED an option off "
            "the cache, but no option position is held."
        )

        # The selected contract is the ~ATM call (percent_otm 0% with spot ~180-185 -> strike
        # 180), bought LONG, off the SEEDED chain — proving strike selection used the fixture
        # chain AND that the DTE window was anchored on the SIMULATED clock (the 2024-03-15
        # expiry is ~43 days from the 2024-02-01 run start, inside the rule's 30..60 DTE window;
        # it would be EXCLUDED if the window were anchored on the wall clock).
        occ = {p.contract_symbol for p in opt_positions}
        assert _OCC_180 in occ, f"expected the ~ATM strike-180 call selected, got {occ}"
        pos = next(p for p in opt_positions if p.contract_symbol == _OCC_180)
        assert pos.side == OrderDirection.BUY, f"expected a LONG call, got {pos.side}"
        assert pos.quantity >= 1, f"expected >=1 contract, got {pos.quantity}"

        # And a real option BUY fill is recorded (the order was actually submitted + filled,
        # not merely staged): cash dropped by at least one premium debit (>=1 contract x 4.x x 100).
        assert account.get_balance() < CFG["starting_cash"], (
            "expected cash debited by the filled option premium"
        )
        assert results["initial_capital"] == 100_000.0
    finally:
        ctx.__exit__(None, None, None)


def test_seed_open_positions_ruleset_carries_option_selection_params():
    """Unit-level guard for the seeding leg of the chain: ``seed_open_positions_ruleset`` builds
    an EventAction whose action config carries the option action_type + the strike/dte/sizing
    selection params in the EXACT keys ``TradeActionEvaluator`` forwards to the option action."""
    from app.services.backtest.backtest_db import backtest_trading_db
    from app.services.backtest.default_rulesets import seed_open_positions_ruleset
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import EventAction, RulesetEventActionLink
    from sqlmodel import Session, select
    from ba2_common.core.db import get_db

    ctx = backtest_trading_db("options-rule-seed-unit")
    ctx.__enter__()
    try:
        rules = [{
            "id": "r1", "conditions": None, "action_type": "buy_call",
            "option_strike_method": "delta", "option_strike_param": 0.4,
            "option_dte_min": 20, "option_dte_max": 45, "option_sizing": 3.0, "enabled": True,
        }]
        rid = seed_open_positions_ruleset(rules, name="seed-unit")

        with Session(get_db().bind) as session:
            ea_ids = session.exec(
                select(RulesetEventActionLink.eventaction_id)
                .where(RulesetEventActionLink.ruleset_id == rid)
                .order_by(RulesetEventActionLink.order_index)
            ).all()
        assert ea_ids, "expected one EventAction linked for the buy_call rule"

        ea = get_instance(EventAction, ea_ids[0])
        # The single action config carries the option action_type + selection params.
        cfg = next(iter(ea.actions.values()))
        assert cfg["action_type"] == "buy_call"
        assert cfg["strike_method"] == "delta"
        assert cfg["strike_param"] == 0.4
        assert cfg["dte_min"] == 20
        assert cfg["dte_max"] == 45
        assert cfg["sizing"] == 3.0
    finally:
        ctx.__exit__(None, None, None)
