"""Entry-option path: an enter_market ruleset whose action is ``buy_call`` opens an
OPTION position from FLAT — no equity leg.

Mirrors ``test_options_rule_e2e`` (same fixture AAPL underlying + CALL chain + premium
bars + options-capable BacktestAccount), but the option action is the ENTRY (seeded into
the ENTER_MARKET ruleset via ``seed_ruleset_from_tree(buy_tree=None, entry_action=...)``),
NOT an open_positions overlay on a held equity position. This exercises
``BacktestDailyEngine._run_expert_bar``'s direct-submit branch: an option entry sizes +
submits itself (``submit_to_broker=True``) so an option fills with NO equity order ever
opened.

The expert returns BUY every analysis bar (bullish) so the enter gate (bullish + flat)
fires; the re-entry guard (``HasNoPositionCondition`` -> ``has_expert_position`` over the
OPENED Transaction whose symbol is the underlying) keeps it to a SINGLE option open while
the position is held.

Run from the backend dir:
    ~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_option_entry_path.py -q
"""
from __future__ import annotations

from datetime import date, datetime

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

# Reuse the sibling module's fixture data + cache/underlying seeding helpers verbatim.
from tests.backtest.test_options_rule_e2e import (  # type: ignore
    CFG,
    END,
    START,
    _OCC_180,
    _AAPL_BARS,
    _seed_cache,
    _underlying_rows,
)


class _BuyExpert(MarketExpertInterface):
    """Deterministic always-BUY expert.

    Every analysis bar it returns a bullish BUY recommendation so the enter_market gate
    (bullish AND has_no_position) fires. Once the option entry fills, ``has_no_position``
    is False (the OPENED option Transaction is keyed by the underlying symbol), so the
    entry does NOT re-fire — proving the re-entry guard counts the option position.
    """

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}  # no entry schedule -> every bar

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub always-BUY expert for the entry-option-path e2e test."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        try:
            price = context.account.get_instrument_current_price("AAPL")
        except Exception:  # noqa: BLE001
            price = None
        return Recommendation(
            signal=OrderRecommendation.BUY,
            confidence=1.0,
            current_price=price if price is not None else 180.0,
            details="buy (entry-option path)",
            raw_outputs={},
        )


def _build_engine_with_option_entry(*, action_type, strike_method, strike_param,
                                    dte_min, dte_max, sizing):
    """Wire the full engine with an ENTER_MARKET ruleset whose action is an option action.

    No open_positions ruleset; an always-BUY stub expert. Returns
    (engine, account, expert, ctx, expert_id, entry_action). Caller MUST close ctx.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    account_id = 82
    expert_id = 82

    import tempfile, os
    tmpdir = tempfile.mkdtemp(prefix="opt-entry-e2e-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-entry-e2e")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)

    # THE ruleset under test: the ENTER_MARKET action is the option action. The option
    # selection params flow through action_from_rule -> the option action ctor.
    entry_action = {
        "action_type": action_type,
        "option_strike_method": strike_method,
        "option_strike_param": strike_param,
        "option_dte_min": dte_min,
        "option_dte_max": dte_max,
        "option_sizing": sizing,
    }
    enter_ruleset_id = seed_ruleset_from_tree(
        buy_tree=None, name=f"opt-entry-{account_id}", entry_action=entry_action
    )

    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_BuyExpert",
        enter_market_ruleset_id=enter_ruleset_id,
        open_positions_ruleset_id=None,  # no overlay: the option is the ENTRY
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _underlying_rows(_AAPL_BARS))
    ps.set_clock(START)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)

    expert = _BuyExpert(expert_id)
    # Enable automated-trading gates so the RM/engine sizes+submits like the real path.
    try:
        expert.settings["allow_automated_trade_opening"] = True
        expert.settings["enable_buy"] = True
    except Exception:  # noqa: BLE001
        pass
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
        "entry_action": entry_action,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, enter_ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),
    )
    return engine, account, expert, ctx, expert_id, entry_action


def test_buy_call_entry_opens_option_no_equity():
    """An ENTER_MARKET ruleset with action ``buy_call`` opens an OPTION from flat (no
    equity leg) and fires only ONCE while the option is held."""
    engine, account, expert, ctx, expert_id, _ = _build_engine_with_option_entry(
        action_type="buy_call", strike_method="percent_otm", strike_param=2.0,
        dte_min=20, dte_max=45, sizing=5.0,
    )
    try:
        engine.run()

        # An OPTION position exists (the buy_call entry FIRED + FILLED off the cache).
        opt_positions = account.get_option_positions()
        assert opt_positions, (
            "expected the buy_call ENTRY to have FIRED + FILLED an option off the cache, "
            "but no option position is held."
        )
        occ = {p.contract_symbol for p in opt_positions}
        assert _OCC_180 in occ, f"expected the ~ATM strike-180 call selected, got {occ}"

        # NO equity position was opened — the entry-option path must not open equity.
        from sqlmodel import Session, select
        from ba2_common.core.db import get_db
        from ba2_common.core.models import Transaction, TradingOrder
        from ba2_common.core.types import AssetClass, TransactionStatus

        with Session(get_db().bind) as s:
            txns = s.exec(
                select(Transaction).where(Transaction.expert_id == expert_id)
            ).all()
            orders = s.exec(select(TradingOrder)).all()

        # Equity orders are those that are NOT options (no option asset_class / contract).
        equity_orders = [
            o for o in orders
            if getattr(o, "asset_class", None) != AssetClass.OPTION
            and not getattr(o, "contract_symbol", None)
            and getattr(o, "underlying_symbol", None) is None
        ]
        assert not equity_orders, (
            f"entry-option path must not open equity; got equity orders {equity_orders}"
        )

        # Re-entry guard: the entry fired exactly ONCE while the option was held — exactly
        # one OPENED option Transaction over the whole run.
        opened_opt_txns = []
        for t in txns:
            if t.status != TransactionStatus.OPENED:
                continue
            entry = account._entry_order_for_transaction(t)
            if entry is not None and getattr(entry, "asset_class", None) == AssetClass.OPTION:
                opened_opt_txns.append(t)
        assert len(opened_opt_txns) == 1, (
            "entry should fire ONCE while the option is held (re-entry guard); "
            f"got {len(opened_opt_txns)} opened option transactions"
        )

        # And cash was debited by the filled option premium (real fill, not staged).
        assert account.get_balance() < CFG["starting_cash"], (
            "expected cash debited by the filled option premium"
        )
    finally:
        ctx.__exit__(None, None, None)
