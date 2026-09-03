"""The two lineages COMPOSE: the capacity downsize, the RM gate, the cap and the breaker.

WHY THIS FILE EXISTS (review 2026-08-30, dev-merge FIX 5)
--------------------------------------------------------
The merge brought two independently-tested lineages together and tested neither against
the other:

  * side A (the options branch): ``_downsize_to_delivery_capacity`` clamps a credit
    structure to the units the account could actually take delivery on, and
    ``_submit_option_order`` then hands the entry to ``admit_option_entry`` and stamps
    ``option_reserve`` + ``max_loss_per_contract`` on the order row;
  * dev: ``BacktestAccount``'s equity cap, and the sleeve breaker reading past it
    (``true_equity``) so a clamped equity series cannot hide a drawdown.

Each side has its own tests. NEITHER side had a test in which both are true at once, and
that is precisely where the interesting failure lives: the ORDER of the two operations.
The downsize happens in the builder, BEFORE ``_submit_option_order``, so the risk manager
must be handed the DOWNSIZED quantity. Hand it the requested one and the sleeve is charged
for 132 condors it never sent, every later candidate in the cycle is refused against a
book that does not exist, and the stamped reserve disagrees with the order it sits on.
Nothing in either lineage's own tests notices.

THE FIXTURE MAKES BOTH LINEAGES BIND AT ONCE, which is the point. The account holds
30,000 of cash under a 20,000 equity cap, and BOTH money readers go through the cap
(``get_balance`` -> ``min(cash, deployed_equity)``, and ``cash_available_for_delivery``
reads the same figure off the snapshot). What separates the two numbers is what each
DIVIDES that 20,000 by:

  * the SIZER divides by COLLATERAL      -- 90 % of 20,000 over a 136.00 per-contract
    reserve wants **132** condors;
  * the CAPACITY gate divides by DELIVERY -- 20,000 against a 100-strike short put,
    10,000 of delivery per unit, so **2** units fit and 3 do not;
  * the RISK MANAGER must see **2**.

The cap is load-bearing in both: on the uncapped 30,000 the same two formulas want 198
and admit 3. So this is one run in which the equity cap, the collateral sizer and the
delivery gate are all binding simultaneously, and the entry that comes out has to satisfy
all three. The capacity control below is measured AT THE MOMENT OF THE DECISION, inside
the wrapped gate call, because the entry itself creates the exposure that would make the
same probe answer False afterwards.

MUTATION KILLS (executed 2026-09-01, not merely asserted):
  * feed ``admit_option_entry`` / ``_submit_option_order`` the REQUESTED quantity instead
    of the clamped one (``quantity = _wanted_qty`` after the downsize in
    ``OpenIronCondorAction._build_and_submit``) -> 3 failures:
    ``test_the_risk_manager_is_handed_the_DOWNSIZED_quantity``,
    ``test_the_order_row_carries_the_downsized_size_and_both_entry_facts``,
    ``test_every_leg_row_is_written_at_the_downsized_size_too``;
  * point ``update_sleeve_breaker`` at ``sleeve_equity`` (the capped reader) ->
    ``test_a_capped_accounts_breaker_halts_on_the_TRUE_equity_series`` fails.

Run from the backend dir (with the worktree on PYTHONPATH):
    python -m pytest tests/backtest/test_cross_lineage_composition.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pytest

import ba2_common.core.OptionRiskManagement as rm
from ba2_common.core.interfaces.ExtendableSettingsInterface import (
    ExtendableSettingsInterface,
)
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.option_book import BreakerState, update_breaker
from ba2_common.core.types import OrderDirection, OrderRecommendation, Recommendation

# ===========================================================================
# The arithmetic, spelled out so a fixture edit that breaks it is visible.
# ===========================================================================
CASH = 30_000.0          # what the ledger holds
CAP = 20_000.0           # the equity cap -- what BOTH money readers actually see
SIZING_PCT = 90.0

SPOT = 125.0
SHORT_PUT, LONG_PUT = 100.0, 98.0
SHORT_CALL, LONG_CALL = 150.0, 152.0
WIDTH = 2.0
#: 1.00 + 1.00 - 0.68 - 0.68 -- the four quotes seeded below.
NET_CREDIT = 0.64
#: (width - credit) x 100. The per-contract collateral AND the measured max loss.
RESERVE_PER_CONTRACT = (WIDTH - NET_CREDIT) * 100.0            # 136.00
#: What ONE unit costs to take delivery on, if the short put is assigned.
DELIVERY_PER_UNIT = SHORT_PUT * 100.0                          # 10,000.00

#: floor(CAP x SIZING_PCT% / RESERVE_PER_CONTRACT) = floor(18,000 / 136) = 132.
WANTED = 132
#: floor(CAP / DELIVERY_PER_UNIT) = floor(20,000 / 10,000) = 2.
ADMITTED = 2

START = datetime(2024, 2, 1)
END = datetime(2024, 2, 6)
EXPIRY = date(2024, 3, 8)                                      # 36 DTE at the run start
RUN_DATES = [date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5), date(2024, 2, 6)]

#                (date, open, high, low, close) -- flat at SPOT, so the strikes are stable.
AAPL_BARS = [(d, SPOT, SPOT + 1, SPOT - 1, SPOT) for d in RUN_DATES]

#: (occ, right, strike, bid, ask). Exactly four contracts: the condor and nothing else, so
#: the selector cannot wander onto a strike the arithmetic above did not price.
CHAIN = [
    ("AAPL240308P00100000", "put", SHORT_PUT, 1.00, 1.10),
    ("AAPL240308P00098000", "put", LONG_PUT, 0.60, 0.68),
    ("AAPL240308C00150000", "call", SHORT_CALL, 1.00, 1.10),
    ("AAPL240308C00152000", "call", LONG_CALL, 0.60, 0.68),
]

RAILS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 40.0,
    # Deliberately slack: the rail under test is the QUANTITY the gate is handed, and a
    # candidate that also tripped leverage would pass this test for the wrong reason.
    "max_notional_leverage": 100.0,
    "undefined_risk_max_pct": 20.0,
    "circuit_breaker_pct": 90.0,        # cannot trip on this flat path
}

CFG = {
    "starting_cash": CASH,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "equity_cap": CAP,
}

ENTRY_ACTION = {
    "action_type": "open_iron_condor",
    "option_strike_method": "percent_otm",
    "option_strike_param": 20.0,        # 125 -> 100 put / 150 call, exactly
    "option_dte_min": 25,
    "option_dte_max": 45,
    "option_sizing": SIZING_PCT,
    "option_wing_width_pct": 2.0,       # 100 -> 98 exactly; 150 -> 153 (nearest 152)
}


class _BuyExpert(MarketExpertInterface):
    """Always bullish, so the entry is decided by the gates under test and nothing else."""

    def __init__(self, id: int):
        super().__init__(id)

    @classmethod
    def description(cls) -> str:            # abstract
        return "Stub always-BUY expert for the cross-lineage composition test."

    def render_market_analysis(self, market_analysis) -> str:   # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:   # abstract
        return None

    def analyze_as_of(self, as_of, context):
        symbol = getattr(self, "_gather_symbol", "AAPL")
        price = context.account.get_instrument_current_price(symbol)
        return Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                              current_price=float(price), details=f"buy {symbol}",
                              raw_outputs={})


def _bar_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _seed_cache(db_path: str) -> None:
    """The four condor legs, quoted on every run date and marked on every bar."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    rows = [{"occ_symbol": occ, "option_type": right, "strike": strike,
             "expiry": EXPIRY.isoformat(), "bid": bid, "ask": ask,
             "last": (bid + ask) / 2, "iv": 0.30, "delta": 0.30,
             "open_interest": 5000}
            for (occ, right, strike, bid, ask) in CHAIN]
    for d in RUN_DATES:
        cache.write_chain_rows("AAPL", d.isoformat(), rows)
    bars = []
    for (occ, right, strike, bid, ask) in CHAIN:
        mid = (bid + ask) / 2
        for d in RUN_DATES:
            bars.append({"occ_symbol": occ, "date": d.isoformat(), "open": mid,
                         "high": ask, "low": bid, "close": mid, "volume": 400,
                         "underlying": "AAPL", "option_type": right, "strike": strike,
                         "expiry": EXPIRY.isoformat()})
    cache.write_bar_rows(bars)


@pytest.fixture(autouse=True)
def _clean_sleeve_state():
    """The sleeve's latches and journals are process state keyed by (thread, expert)."""
    rm.reset_state()
    yield
    rm.reset_state()


class _Run:
    """What one engine run produced, for the assertions below."""

    def __init__(self, admit_calls, option_orders, capacity_at_entry,
                 capped_balance, true_equity):
        self.admit_calls = admit_calls
        self.option_orders = option_orders
        self.capacity_at_entry = capacity_at_entry
        self.capped_balance = capped_balance
        self.true_equity = true_equity


def _engine_run(*, account_id: int, expert_id: int) -> _Run:
    """A real ``DailyBacktestEngine.run()`` over a real ``BacktestAccount``.

    ``admit_option_entry`` is WRAPPED, not replaced: the real gate still runs and still
    decides, and the wrapper only records the keyword arguments it was handed. A stub
    would make "the RM saw 2" a statement about the stub.
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
    import ba2_common.core.TradeActions as ta

    tmpdir = tempfile.mkdtemp(prefix="cross-lineage-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"cross-lineage-{account_id}")
    ctx.__enter__()
    admit_calls: List[Dict[str, Any]] = []
    probe: List[Dict[int, bool]] = []
    balances: List[float] = []
    real_admit = ta.admit_option_entry
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_ruleset_from_tree(buy_tree=None,
                                            name=f"cross-lineage-{account_id}",
                                            entry_action=ENTRY_ACTION)
        seed_expert_instance(account_id=account_id, expert_class_name="_BuyExpert",
                             enter_market_ruleset_id=ruleset_id,
                             open_positions_ruleset_id=None, instance_id=expert_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _bar_rows(AAPL_BARS))
        ps.set_clock(START)

        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)

        expert = _BuyExpert(expert_id)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "risk_manager_mode": ("classic_options", "str"),
            # The per-instrument ceiling defaults to 10 %, which would bind long before
            # option_sizing does and make WANTED a different number entirely.
            "max_virtual_equity_per_instrument_percent": (100.0, "float"),
            **{k: (v, "int" if isinstance(v, int) else "float") for k, v in RAILS.items()},
        })
        resolver.register_expert(expert_id, expert)

        def _recording_admit(**kwargs):
            admit_calls.append(dict(kwargs))
            # THE CONTROL, measured at the moment of the decision rather than after it.
            # Taken post-run it would read the book the entry itself created (two short
            # puts already owing 20,000 of delivery) and answer False for every size.
            probe.append(_capacity_probe(account))
            balances.append(account.get_balance())
            return real_admit(**kwargs)

        ta.admit_option_entry = _recording_admit

        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, expert.settings, ruleset_id)],
            price_source=ps,
            config={"start_date": START, "end_date": END,
                    "enabled_instruments": ["AAPL"], "seed": 42,
                    "entry_action": ENTRY_ACTION},
            indicator_provider=object())
        engine.run()

        # Read inside the run context: the in-memory trade store is scoped to it.
        return _Run(admit_calls, _option_orders(), probe[0] if probe else {},
                    balances[0] if balances else account.get_balance(),
                    account.true_equity())
    finally:
        ta.admit_option_entry = real_admit
        ctx.__exit__(None, None, None)


def _option_orders() -> List[Any]:
    """Every OPTION order this run wrote, parents included."""
    from ba2_common.core.trade_store import orders_where
    from ba2_common.core.types import AssetClass

    return [o for o in orders_where()
            if getattr(o, "asset_class", None) is AssetClass.OPTION]


def _parent_orders(orders) -> List[Any]:
    """The multi-leg PARENTS -- the rows the entry facts are stamped on."""
    return [o for o in orders if getattr(o, "parent_order_id", None) is None]


def _capacity_probe(account) -> Dict[int, bool]:
    """THE CONTROL, executed on the account itself rather than asserted about it.

    The clamp is only meaningful if the requested size genuinely does not fit and the
    admitted one genuinely does. Both are measured here, through the account's own gate,
    at the instant the entry is being decided.
    """
    return {n: account.check_short_put_assignment_capacity(
        strike=SHORT_PUT, contracts=n).ok for n in (WANTED, ADMITTED, ADMITTED + 1)}


# =========================================================================== #
# (a) probe 2 -- the downsize, the RM gate and the stamp, in one run
# =========================================================================== #
def test_the_capacity_gate_really_binds_at_two_units():
    """The control. Without it, every assertion below could be satisfied by an entry that
    was clamped for some entirely different reason -- or not clamped at all.

    It also pins that the CAP is what both readers see: the ledger holds 30,000 and every
    money reader answers 20,000. Uncapped, this same fixture wants 198 and admits 3, so
    neither number below is reachable without the other lineage in play."""
    run = _engine_run(account_id=9401, expert_id=9401)

    # true_equity is read at the END of the run, so it carries the condor's credit.
    assert run.true_equity > CAP                        # the ledger holds more than the cap
    assert run.capped_balance == pytest.approx(CAP)      # ...and every reader sees 20,000

    assert run.capacity_at_entry[WANTED] is False        # 132 units cannot take delivery
    assert run.capacity_at_entry[ADMITTED + 1] is False  # nor can 3
    assert run.capacity_at_entry[ADMITTED] is True       # 2 can


def test_the_risk_manager_is_handed_the_DOWNSIZED_quantity():
    """THE joint property. ``_downsize_to_delivery_capacity`` runs in the builder, before
    ``_submit_option_order``, so the quantity that reaches ``admit_option_entry`` is the
    CLAMPED one. Handing it the requested 132 would charge the sleeve for a book that was
    never sent and refuse every later candidate in the cycle against it."""
    run = _engine_run(account_id=9402, expert_id=9402)

    assert len(run.admit_calls) == 1, run.admit_calls
    call = run.admit_calls[0]
    assert call["option_strategy"] == "iron_condor"
    assert call["underlying"] == "AAPL"
    assert call["quantity"] == ADMITTED, call["quantity"]
    assert call["quantity"] != WANTED
    # The max loss is HANDED to the gate, per contract, never re-derived there.
    assert call["max_loss_per_contract"] == pytest.approx(RESERVE_PER_CONTRACT)
    # ...and it really is a four-legged condor that was weighed.
    assert len(call["legs"]) == 4
    assert sorted(leg.strike for leg in call["legs"]) == [LONG_PUT, SHORT_PUT,
                                                          SHORT_CALL, LONG_CALL]


def test_the_order_row_carries_the_downsized_size_and_both_entry_facts():
    """The two lineages' outputs must agree ON THE ROW: the quantity the capacity gate
    admitted, the reserve re-derived for THAT size (not the requested one), and the
    measured max loss the ``loss_pct_of_max_loss`` exit reads its denominator back from."""
    run = _engine_run(account_id=9403, expert_id=9403)

    parents = _parent_orders(run.option_orders)
    assert len(parents) == 1, [(o.id, o.symbol, o.quantity) for o in run.option_orders]
    parent = parents[0]

    assert parent.quantity == ADMITTED, parent.quantity
    data = parent.data or {}
    # Re-derived for the CLAMPED size: 2 x 136, not 132 x 136.
    assert data["option_reserve"] == pytest.approx(RESERVE_PER_CONTRACT * ADMITTED)
    assert data["max_loss_per_contract"] == pytest.approx(RESERVE_PER_CONTRACT)


def test_every_leg_row_is_written_at_the_downsized_size_too():
    """A parent clamped to 2 over legs still written at 132 would be a book the account
    cannot pay for, assembled out of two individually-correct lineages."""
    run = _engine_run(account_id=9404, expert_id=9404)

    legs = [o for o in run.option_orders if getattr(o, "parent_order_id", None) is not None]
    assert len(legs) == 4, [(o.id, o.symbol, o.quantity) for o in legs]
    assert {o.quantity for o in legs} == {ADMITTED}


# =========================================================================== #
# (b) probe 1 -- the breaker reads past the cap on the SAME kind of account
# =========================================================================== #
#: A 20k account under a 20k cap holding 200 shares at 100. The price runs to 150 and back
#: down; the true equity peaks at 30k while the capped series is pinned at 20k.
BREAKER_CAP = 20_000.0
BREAKER_SHARES = 200
BREAKER_PCT = 20.0
BREAKER_BARS = [
    (datetime(2024, 1, 2), 100, 101, 99, 100),
    (datetime(2024, 1, 3), 100, 101, 99, 100),     # fill @100 -> 20,000
    (datetime(2024, 1, 4), 110, 126, 109, 125),    # 25,000
    (datetime(2024, 1, 5), 140, 151, 139, 150),    # 30,000  <- the TRUE peak
    (datetime(2024, 1, 8), 120, 121, 114, 115),    # 23,000  <- -23.3 %: the TRUE breaker trips
    (datetime(2024, 1, 9), 105, 106, 99, 100),     # 20,000
    (datetime(2024, 1, 10), 95, 96, 89, 90),       # 18,000
    (datetime(2024, 1, 11), 85, 86, 79, 80),       # 16,000  <- where a CAPPED breaker trips
]

BREAKER_CFG = {
    "starting_cash": BREAKER_CAP,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "equity_cap": BREAKER_CAP,
}

BREAKER_RAILS: Dict[str, Any] = {
    "max_concurrent_structures": 10,
    "max_deployment_pct": 50.0,
    "max_notional_leverage": 3.0,
    "undefined_risk_max_pct": 20.0,
    "circuit_breaker_pct": BREAKER_PCT,
}


class _SleeveExpert:
    """The sleeve's rails read through the REAL settings accessor, borrowed not imitated."""

    get_setting_with_interface_default = (
        ExtendableSettingsInterface.get_setting_with_interface_default)
    get_merged_settings_definitions = MarketExpertInterface.get_merged_settings_definitions

    def __init__(self, instance_id: int, settings: Dict[str, Any]):
        self.id = instance_id
        self.settings = dict(settings)


def _capped_account(account_id: int):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    wire_backtest_seams()
    ctx = backtest_trading_db(f"cross-lineage-breaker-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, BREAKER_CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _bar_rows(BREAKER_BARS))
    account = BacktestAccount(account_id, ps, BREAKER_CFG)
    wire_backtest_seams().register_account(account_id, account)
    return account, ctx, ps


def _buy_and_hold(account, ps) -> None:
    """Deploy the whole account into 200 shares at 100, through the REAL order path."""
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderStatus, OrderType

    ps.set_clock(BREAKER_BARS[0][0])
    order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=BREAKER_SHARES,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.NEW, comment="cross-lineage breaker fixture")
    account.submit_order(order)
    account.refresh_orders()
    filled = account.get_order(order.broker_order_id)
    assert filled.status == OrderStatus.FILLED, filled.status
    assert filled.filled_qty == BREAKER_SHARES, filled.filled_qty


def _first_halted(walk) -> Optional[int]:
    for i, row in enumerate(walk, start=1):
        if row["state"].halted:
            return i
    return None


def test_a_capped_accounts_breaker_halts_on_the_TRUE_equity_series():
    """The other lineage, on the account shape (a) runs on: the sizer keeps the clamp and
    the breaker looks past it.

    The control is EXECUTED, not asserted: the identical ``option_book.update_breaker``,
    fed the numbers ``sleeve_equity`` actually returned on those same bars, stands down
    three evaluations later. So "the cap would have hidden the loss" is a measurement of
    this run.
    """
    account, ctx, ps = _capped_account(9405)
    try:
        _buy_and_hold(account, ps)
        expert = _SleeveExpert(9405, {"risk_manager_mode": "classic_options",
                                      **BREAKER_RAILS})
        walk = []
        for bar in BREAKER_BARS[1:]:
            ps.set_clock(bar[0])
            walk.append({
                "true": rm.sleeve_true_equity(account, 9405),
                "capped": rm.sleeve_equity(account, 9405),
                "deployed": account.deployed_equity(),
                "state": rm.update_sleeve_breaker(expert=expert, account=account,
                                                  expert_instance_id=9405),
            })

        # The cap really does clamp the sizer's view, and never the breaker's.
        assert [r["true"] for r in walk] == [20_000.0, 25_000.0, 30_000.0, 23_000.0,
                                             20_000.0, 18_000.0, 16_000.0]
        assert [r["deployed"] for r in walk] == [20_000.0, 20_000.0, 20_000.0, 20_000.0,
                                                 20_000.0, 18_000.0, 16_000.0]
        assert [r["capped"] for r in walk] == [r["deployed"] for r in walk]

        assert _first_halted(walk) == 4
        assert walk[2]["state"].peak_equity == 30_000.0      # the peak the cap concealed

        capped_control, state = None, BreakerState()
        for i, row in enumerate(walk, start=1):
            state = update_breaker(state, row["capped"],
                                   {"circuit_breaker_pct": BREAKER_PCT})
            if state.halted:
                capped_control = i
                break
        assert capped_control == 7
    finally:
        ctx.__exit__(None, None, None)
