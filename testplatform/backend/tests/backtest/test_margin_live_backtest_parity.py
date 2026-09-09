"""THREE-WAY SIZING PARITY: BacktestAccount $4,000 == live $4,000 1x == live $2,000 2x.

THE REQUIREMENT (user-confirmed, docs/plans/2026-09-09-margin-live-backtest-parity.md §2)
------------------------------------------------------------------------------------------
Leverage is LIVE-ONLY; the unlevered backtest stays the strategy reference. A live
account with equity E, broker multiplier m and configured factor f deploys
``E x min(f, m)``, and at that state it must behave EXACTLY like an unlevered account
funded with that number -- same virtual/available balance, same quantities, same
TP/SL prices, same validator verdict. So a $2,000 account at 2x is a $4,000 account,
and nothing downstream is allowed to notice which one it is.

This file drives the REAL shared methods for all three arms and compares them:

    get_virtual_balance / get_available_balance   (MarketExpertInterface)
    TradeRiskManagement().size_candidate_orders   (the live enter path's sizing)
    AdjustTakeProfitAction/AdjustStopLossAction.compute_price
    AccountInterface._validate_position_size_limits

NOTHING is stubbed to the expected answer: the BT arm is a real ``BacktestAccount``
over a real price source with real fills, the two live arms are real
``AccountInterface`` subclasses answering from a canned broker snapshot, and all
three experts are real ``MarketExpertInterface`` instances reading real settings rows.
The only doubles are the BROKER (a snapshot dict) and the market (flat $100 bars).

THE TWO PINNED BLOCKERS (plan §6)
---------------------------------
Two pre-existing discrepancies are deliberately NOT fixed by the leverage feature,
because fixing either would move historical backtest results. Both are pinned here as
``xfail(strict=True)`` -- so the day someone corrects the contract, the pin XPASSes and
FAILS the suite, forcing the change to be acknowledged rather than absorbed:

  * FINDING 6 -- ``BacktestAccount.get_balance()`` is CASH while a live account's is
    EQUITY, so after a $1,000 purchase from $4,000 the shared expert math charges the
    position twice in the backtest ($3,000 virtual / $2,000 free) and once live
    ($4,000 / $3,000). Flat state is identical; the divergence begins at the first fill.
  * FINDING 4 -- the classic RM's per-instrument ceiling is ``available x ratio``, not
    ``virtual x ratio``, so a half-invested expert's 10% cap is 10% of what is LEFT.

Each pin has a companion NON-xfail test recording the CURRENT number, so the
divergence is documented in both directions and a silent drift in either fails.

Run from the backend dir:
    python -m pytest tests/backtest/test_margin_live_backtest_parity.py -q
"""
from __future__ import annotations

import contextlib
import itertools
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytest

# --------------------------------------------------------------------------- #
# The market: one flat $100 symbol (plus a second one to hold a position in).
# --------------------------------------------------------------------------- #
SYMBOL = "PARA"
HELD_SYMBOL = "PARB"
PRICE = 100.0
_BAR_DATES = [datetime(2024, 1, d) for d in (2, 3, 4, 5, 8, 9)]

TP_PCT = 10.0            # +10% -> 110 long / 90 short
SL_PCT = -5.0            # -5%  -> 95 long / 105 short
STOP_PRICE = 95.0        # the explicit SL the risk_atr mode sizes off


def _flat_bars() -> List[Dict[str, Any]]:
    """A perfectly flat series, so a next-bar-open fill is exactly $100 and the
    used-balance mark-to-market is exactly the entry price. Any price drift would
    make the BT arm's cash and the live arms' equity diverge for a reason that has
    nothing to do with the contract under test."""
    return [{"Date": d, "Open": PRICE, "High": PRICE, "Low": PRICE,
             "Close": PRICE, "Volume": 1_000} for d in _BAR_DATES]


# Unique ids per world, so a leaked registration from an earlier test can never be
# mistaken for this one's account/expert (the seam registry is thread-local, not
# per-test).
_IDS = itertools.count(7_100)


# --------------------------------------------------------------------------- #
# The live-shaped account: a real AccountInterface over a canned broker snapshot.
# --------------------------------------------------------------------------- #
def _live_account_cls():
    from ba2_common.core.interfaces.AccountInterface import AccountInterface

    class _LiveAccount(AccountInterface):
        """A trading account with every abstract stubbed and a canned broker snapshot,
        built bare (no ``__init__`` chain) exactly as
        ``packages/common/tests/test_stock_exposure_gate._Acct`` does.

        It is a REAL ``AccountInterface``: the margin accessors, the tradable balance,
        the exposure ceiling and every validator under test are the production ones.
        """

        def __init__(self, id_val, *, balance, snapshot, settings):
            self.id = id_val
            self._balance = balance
            self._snap = snapshot
            self._stored = settings
            self.submitted: List[Any] = []

        # -- settings ---------------------------------------------------------
        @property
        def settings(self):
            return self._stored

        @classmethod
        def get_settings_definitions(cls):
            return {}

        # -- read-only surface ------------------------------------------------
        def get_account_snapshot(self):
            return self._snap

        def get_balance(self):
            return self._balance

        def get_account_info(self):
            return {"buying_power": self._snap.buying_power,
                    "equity": self._snap.equity}

        def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
            # Overridden ABOVE the inherited caching layer on purpose: that cache is
            # class-global and keyed by account id, so it would leak a price between
            # worlds that reuse an id.
            if isinstance(symbol_or_symbols, (list, tuple, set)):
                return {s: PRICE for s in symbol_or_symbols}
            return PRICE

        def get_positions(self):
            return []

        def get_orders(self, status=None):
            return []

        def get_order(self, order_id):
            return None

        def symbols_exist(self, symbols):
            return {s: True for s in symbols}

        def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type="bid"):
            return PRICE

        def refresh_positions(self):
            return True

        def refresh_orders(self):
            return True

        def get_dividends(self, symbol=None, start_date=None, end_date=None):
            return []

        def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
            return []

        def get_balance_history(self, start_date=None, end_date=None):
            return []

        # -- trading surface --------------------------------------------------
        def _submit_order_impl(self, trading_order, tp_price=None, sl_price=None,
                               is_closing_order=False, use_complex_order=False):
            self.submitted.append(trading_order)
            return trading_order

        def cancel_order(self, order_id):
            return None

        def modify_order(self, order_id):
            return None

        def adjust_tp(self, transaction, new_tp_price, source=""):
            return True

        def adjust_sl(self, transaction, new_sl_price, source=""):
            return True

        def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
            return True

    return _LiveAccount


def _parity_expert_cls():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

    class _ParityExpert(MarketExpertInterface):
        """A real ``MarketExpertInterface``. It makes no decisions -- this file feeds
        candidates in directly -- it exists so the BALANCE and SIZING methods under test
        are the production ones, reading production settings rows."""

        @classmethod
        def description(cls) -> str:
            return "three-way margin parity test expert"

        @classmethod
        def get_settings_definitions(cls) -> dict:
            return {}

        def render_market_analysis(self, market_analysis) -> str:
            return ""

        def run_analysis(self, symbol, market_analysis):
            return None

    return _ParityExpert


# --------------------------------------------------------------------------- #
# The world: three accounts + three experts on one backtest DB.
# --------------------------------------------------------------------------- #
@dataclass
class _Arm:
    name: str
    account: Any
    expert: Any
    instance_id: int


@dataclass
class _World:
    bt: _Arm
    l1: _Arm
    l2: _Arm
    price_source: Any

    @property
    def arms(self) -> Tuple[_Arm, _Arm, _Arm]:
        return (self.bt, self.l1, self.l2)


#: Identical for all three experts. The whole point is that only the ACCOUNT differs.
def _expert_settings(sizing_mode: str) -> Dict[str, Tuple[Any, str]]:
    return {
        "allow_automated_trade_opening": (True, "bool"),
        "enable_buy": (True, "bool"),
        "enable_sell": (True, "bool"),
        "max_virtual_equity_per_instrument_percent": (100.0, "float"),
        "diversification_factor": (1.0, "float"),
        "min_available_balance_pct": (0.0, "float"),
        "sizing_mode": (sizing_mode, "str"),
        # Distinct stop and sizing-risk settings are exercised by the plan's own
        # resolver tests; here both are 1.0 so the risk_atr arithmetic is exact.
        "risk_per_trade_pct": (1.0, "float"),
        "atr_risk_budget_pct": (1.0, "float"),
        "atr_multiplier": (2.0, "float"),
        "atr_period": (14, "float"),
        "min_stop_loss_pct": (0.0, "float"),
        "use_atr_stop": (False, "bool"),
    }


def _snapshot(*, equity, multiplier, buying_power, long_mv=0.0, short_mv=0.0):
    from ba2_common.core.account_types import AccountSnapshot

    return AccountSnapshot(cash=equity, equity=equity, net_liquidation=equity,
                           buying_power=buying_power, margin_multiplier=multiplier,
                           long_market_value=long_mv, short_market_value=short_mv)


@contextlib.contextmanager
def parity_world(*, pct: float = 100.0, sizing_mode: str = "notional",
                 bt_cash: float = 4_000.0,
                 l1_balance: float = 4_000.0,
                 l2_balance: float = 2_000.0,
                 l2_multiplier: Optional[float] = 2.0,
                 l2_factor: float = 2.0,
                 l2_margin_enabled: bool = True):
    """Three accounts, three experts, one throwaway in-memory backtest DB.

    * **BT** -- a real ``BacktestAccount`` with ``starting_cash=bt_cash``, margin OFF and
      never switched on (that is the whole compatibility contract: a backtest cannot
      reach a line of the leverage feature).
    * **L1** -- live-shaped, ``get_balance() == l1_balance``, multiplier 1.0, margin OFF.
      The UNLEVERED live reference.
    * **L2** -- live-shaped, ``get_balance() == l2_balance``, broker multiplier
      ``l2_multiplier``, ``margin_factor=l2_factor``. The LEVERED account that must
      behave like L1.

    Commission is 0 and the market is flat at $100 everywhere, so any difference
    between the arms is the contract, not the fixture.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance,
    )
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    tag = next(_IDS)
    bt_account_id, l1_account_id, l2_account_id = tag * 10 + 1, tag * 10 + 2, tag * 10 + 3

    cfg = {
        "starting_cash": bt_cash,
        "commission_per_trade": 0.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"margin-parity-{tag}")
    ctx.__enter__()
    try:
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(SYMBOL, _flat_bars())
        ps.load_bars(HELD_SYMBOL, _flat_bars())
        ps.set_clock(_BAR_DATES[0])

        ruleset_id = seed_ruleset_from_tree(None, name=f"parity-enter-{tag}")
        live_cls, expert_cls = _live_account_cls(), _parity_expert_cls()

        seed_account_definition(bt_account_id, cfg)
        seed_account_definition(l1_account_id)
        seed_account_definition(l2_account_id)

        bt_account = BacktestAccount(bt_account_id, ps, cfg)
        l1_account = live_cls(
            l1_account_id, balance=l1_balance,
            snapshot=_snapshot(equity=l1_balance, multiplier=1.0, buying_power=l1_balance),
            settings={"margin_enabled": False, "margin_factor": 1.8,
                      "commission_per_trade": 0.0})
        # The broker lends up to equity x multiplier; that is the buying power a real
        # margin account publishes while flat.
        l2_bp = l2_balance * (l2_multiplier if (l2_multiplier or 0) > 1.0 else 1.0)
        l2_account = live_cls(
            l2_account_id, balance=l2_balance,
            snapshot=_snapshot(equity=l2_balance, multiplier=l2_multiplier,
                               buying_power=l2_bp),
            settings={"margin_enabled": l2_margin_enabled, "margin_factor": l2_factor,
                      "commission_per_trade": 0.0})

        arms = []
        for name, account, account_id in (("BT", bt_account, bt_account_id),
                                          ("L1", l1_account, l1_account_id),
                                          ("L2", l2_account, l2_account_id)):
            resolver.register_account(account_id, account)
            instance_id = seed_expert_instance(
                account_id=account_id, expert_class_name="_ParityExpert",
                enter_market_ruleset_id=ruleset_id, virtual_equity_pct=pct)
            expert = expert_cls(instance_id)
            expert.save_settings(_expert_settings(sizing_mode))
            resolver.register_expert(instance_id, expert)
            arms.append(_Arm(name=name, account=account, expert=expert,
                             instance_id=instance_id))

        yield _World(bt=arms[0], l1=arms[1], l2=arms[2], price_source=ps)
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# The measurements -- every one through a production method.
# --------------------------------------------------------------------------- #
def _candidate(symbol: str = SYMBOL, *, side=None, quantity: float = 0.0,
               stop_price: Optional[float] = None, account_id: Optional[int] = None):
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

    return TradingOrder(account_id=account_id, symbol=symbol, quantity=quantity,
                        side=side or OrderDirection.BUY, order_type=OrderType.MARKET,
                        status=OrderStatus.PENDING, open_price=PRICE,
                        stop_price=stop_price)


def _recommendation(instance_id: int, symbol: str = SYMBOL):
    """A REAL ExpertRecommendation (transient): the RM prioritizes on its
    ``expected_profit_percent``, so a namespace double would not exercise the same read."""
    from ba2_common.core.models import ExpertRecommendation
    from ba2_common.core.types import OrderRecommendation, RiskLevel, TimeHorizon

    return ExpertRecommendation(
        instance_id=instance_id, symbol=symbol,
        recommended_action=OrderRecommendation.BUY, expected_profit_percent=TP_PCT,
        price_at_date=PRICE, details="parity candidate", confidence=80.0,
        risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM)


def candidate_quantity(arm: _Arm, *, sizing_mode: str, symbol: str = SYMBOL) -> float:
    """The share count the LIVE enter path would fund, through the real classic RM.

    ``size_candidate_orders`` is the in-memory candidate flow the live enter path
    actually uses (``review_and_prioritize_pending_orders`` is the DB twin); it runs
    the same ``_size_prioritized_orders`` -> ``_calculate_order_quantities`` core.
    """
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement

    order = _candidate(symbol, account_id=arm.account.id,
                       stop_price=STOP_PRICE if sizing_mode == "risk_atr" else None)
    funded = TradeRiskManagement().size_candidate_orders(
        arm.instance_id, [(order, _recommendation(arm.instance_id, symbol))])
    return float(funded[0].quantity) if funded else 0.0


def bracket_prices(arm: _Arm) -> Dict[str, float]:
    """TP +10% / SL -5% off the entry price, long and short, through the real actions."""
    from ba2_common.core.TradeActions import AdjustStopLossAction, AdjustTakeProfitAction
    from ba2_common.core.types import OrderDirection, OrderRecommendation, ReferenceValue

    out: Dict[str, float] = {}
    reference = ReferenceValue.ORDER_OPEN_PRICE.value
    for label, side, rec in (("long", OrderDirection.BUY, OrderRecommendation.BUY),
                             ("short", OrderDirection.SELL, OrderRecommendation.SELL)):
        order = _candidate(side=side, quantity=1, account_id=arm.account.id)
        out[f"tp_{label}"] = AdjustTakeProfitAction(
            SYMBOL, arm.account, rec, order,
            reference_value=reference, percent=TP_PCT).compute_price(order)
        out[f"sl_{label}"] = AdjustStopLossAction(
            SYMBOL, arm.account, rec, order,
            reference_value=reference, percent=SL_PCT).compute_price(order)
    return out


def size_limit_verdict(arm: _Arm, *, quantity: float) -> Tuple[str, ...]:
    """``_validate_position_size_limits``'s verdict for an order of ``quantity`` shares.

    The messages carry only dollar figures and percentages -- no account or expert id --
    so they are directly comparable across arms, which is the point: the same order must
    be judged with the same words on all three.
    """
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    # open_price is deliberately left None: this is the WAITING transaction of an order
    # that has not been sent, so it must not become "used balance" for the very check it
    # is the subject of.
    transaction_id = add_instance(Transaction(
        symbol=SYMBOL, quantity=quantity, side=OrderDirection.BUY,
        status=TransactionStatus.WAITING, expert_id=arm.instance_id))
    order = _candidate(quantity=quantity, account_id=arm.account.id)
    order.transaction_id = transaction_id
    add_instance(order)
    return tuple(arm.account._validate_position_size_limits(order))


def measure(arm: _Arm, *, sizing_mode: str) -> Dict[str, Any]:
    """Every parity-relevant figure for one arm, in one dict, so a divergence names
    itself in the assertion diff instead of failing on the first of eight asserts.

    ORDER MATTERS: the balances and the RM quantity are taken BEFORE
    ``size_limit_verdict`` persists its probe transactions, so the probes cannot feed
    back into the numbers they were meant to observe.
    """
    figures: Dict[str, Any] = {
        "virtual": arm.expert.get_virtual_balance(),
        "available": arm.expert.get_available_balance(),
        "quantity": candidate_quantity(arm, sizing_mode=sizing_mode),
    }
    figures.update(bracket_prices(arm))
    figures["verdict_affordable"] = size_limit_verdict(arm, quantity=5)
    figures["verdict_oversized"] = size_limit_verdict(arm, quantity=100)
    return figures


# --------------------------------------------------------------------------- #
# 1. FLAT STATE: the three arms are indistinguishable.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pct", [25.0, 50.0, 100.0])
@pytest.mark.parametrize("sizing_mode", ["notional", "risk_atr"])
def test_flat_state_is_identical_across_backtest_and_both_live_fundings(pct, sizing_mode):
    """$4,000 backtest == $4,000 live 1x == $2,000 live 2x, at 25/50/100% allocation
    and in both sizing modes. The factor is applied ONCE, at the account boundary."""
    with parity_world(pct=pct, sizing_mode=sizing_mode) as world:
        readings = {arm.name: measure(arm, sizing_mode=sizing_mode) for arm in world.arms}

    assert readings["BT"] == readings["L1"] == readings["L2"]

    expected_virtual = 4_000.0 * (pct / 100.0)
    expected_qty = (expected_virtual / PRICE if sizing_mode == "notional"
                    # risk_atr: (equity x 1%) / $5 of risk per share
                    else float(int((expected_virtual * 0.01) // (PRICE - STOP_PRICE))))
    for name, reading in readings.items():
        assert reading["virtual"] == pytest.approx(expected_virtual), name
        assert reading["available"] == pytest.approx(expected_virtual), name
        assert reading["quantity"] == pytest.approx(expected_qty), name


def test_the_bracket_prices_do_not_move_with_the_funding_model():
    """A $100 long keeps $110/$95 and a short $90/$105 at either funding level: capital
    scales the SIZE of a position, never the DISTANCE of its protection."""
    with parity_world() as world:
        brackets = {arm.name: bracket_prices(arm) for arm in world.arms}

    assert brackets["BT"] == brackets["L1"] == brackets["L2"]
    for name, prices in brackets.items():
        assert prices["tp_long"] == pytest.approx(110.0), name
        assert prices["sl_long"] == pytest.approx(95.0), name
        assert prices["tp_short"] == pytest.approx(90.0), name
        assert prices["sl_short"] == pytest.approx(105.0), name


def test_the_oversized_order_is_refused_in_the_same_words_everywhere():
    """The parity claim covers REFUSALS too: an order none of the three may place must be
    rejected by all three, for the same reason and against the same ceiling. Without this
    the flat-state equality could be satisfied by three validators that all pass
    everything."""
    with parity_world() as world:
        verdicts = {arm.name: size_limit_verdict(arm, quantity=100) for arm in world.arms}

    assert verdicts["BT"] == verdicts["L1"] == verdicts["L2"]
    assert verdicts["BT"], "a $10,000 order on a $4,000 account must be refused"
    joined = " | ".join(verdicts["BT"])
    assert "exceeding expert's max allowed $4000.00" in joined
    assert "100.0% of virtual equity $4000.00" in joined
    assert "exceeds expert's available balance $4000.00" in joined


# --------------------------------------------------------------------------- #
# 2. THE FACTOR IS BOUNDED, AND SWITCHABLE OFF.
# --------------------------------------------------------------------------- #
def test_a_factor_above_the_broker_multiplier_buys_nothing():
    """``effective_factor = min(configured factor, broker multiplier)``. A factor of 3 on
    a broker that lends 2x is still 2x -- the platform never invents capacity the broker
    has not granted."""
    with parity_world(l2_factor=3.0) as world:
        greedy = measure(world.l2, sizing_mode="notional")
        reference = measure(world.l1, sizing_mode="notional")
    assert greedy == reference

    with parity_world(l2_factor=2.0) as world:
        exact = measure(world.l2, sizing_mode="notional")
    assert greedy == exact


def test_margin_off_makes_the_2000_account_a_2000_account_again():
    """The switch is the whole feature's containment: with ``margin_enabled`` False the
    same $2,000 account sizes at exactly HALF of the $4,000 reference. If this ever
    reported the levered figure, every backtest (margin always off) would have moved."""
    with parity_world(l2_margin_enabled=False) as world:
        unlevered = measure(world.l2, sizing_mode="notional")
        reference = measure(world.l1, sizing_mode="notional")

    assert unlevered["virtual"] == pytest.approx(2_000.0)
    assert unlevered["available"] == pytest.approx(2_000.0)
    assert unlevered["quantity"] == pytest.approx(reference["quantity"] / 2.0)
    assert unlevered["quantity"] == pytest.approx(20.0)


def test_a_non_finite_broker_multiplier_refuses_rather_than_sizing(monkeypatch):
    """Review finding 5 (fixed in Task 1), seen from the sizing end: NaN used to lose
    every comparison and hand back the full CONFIGURED factor -- an unpublished
    multiplier read as permission to lever. It must now be a refusal (``None`` = "cannot
    size"), never a number."""
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    with parity_world(l2_multiplier=float("nan")) as world:
        assert world.l2.expert.get_virtual_balance() is None
        assert world.l2.expert.get_available_balance() is None
        # ...and the unlevered arms are untouched by their neighbour's broken broker.
        assert world.l1.expert.get_virtual_balance() == pytest.approx(4_000.0)


@pytest.mark.parametrize("equity", [1_900.0, 2_000.0, 2_100.0])
def test_current_equity_equivalence_has_no_starting_capital_anchor(equity):
    """The user's confirmed table: $1,900/$2,000/$2,100 at 2x equal $3,800/$4,000/$4,200
    unlevered AT THAT STATE. Capital is recomputed from CURRENT equity every time -- there
    is no fixed starting-capital anchor and no synthetic P&L ledger."""
    reference = equity * 2.0
    with parity_world(l1_balance=reference, l2_balance=equity, bt_cash=reference) as world:
        levered = measure(world.l2, sizing_mode="notional")
        unlevered = measure(world.l1, sizing_mode="notional")
        backtest = measure(world.bt, sizing_mode="notional")

    assert levered == unlevered == backtest
    assert levered["virtual"] == pytest.approx(reference)


def test_the_capital_mapping_names_the_equivalent_unlevered_account():
    """Task 4's mapping is what makes a levered live run readable against the unlevered
    backtest it reproduces: its ``equivalent_unlevered_balance`` must literally BE the
    reference account's balance."""
    with parity_world() as world:
        mapping = world.l2.expert.describe_capital_mapping()
        assert "error" not in mapping, mapping
        assert mapping["equivalent_unlevered_balance"] == pytest.approx(
            world.l1.account.get_balance())
        assert mapping["balance"] == pytest.approx(2_000.0)
        assert mapping["effective_factor"] == pytest.approx(2.0)
        assert mapping["virtual_balance"] == pytest.approx(4_000.0)

        # The unlevered arm describes itself without a factor at all -- and without
        # reading a broker snapshot, which is the backtest's cost contract.
        flat = world.l1.expert.describe_capital_mapping()
        assert flat["margin_enabled"] is False
        assert flat["effective_factor"] == 1.0
        assert flat["equivalent_unlevered_balance"] == pytest.approx(4_000.0)


# --------------------------------------------------------------------------- #
# 3. POST-ENTRY STATE: the two live arms still agree; the backtest does not.
# --------------------------------------------------------------------------- #
ENTRY_QTY = 10.0
ENTRY_NOTIONAL = ENTRY_QTY * PRICE            # $1,000


def _open_backtest_position(world: _World) -> None:
    """Open 10 @ $100 on the REAL BacktestAccount: submit through the inherited
    ``submit_order`` (so every shared validator runs), then step the fill engine with
    ``refresh_orders`` so the ledger actually pays for the shares. Copied from
    ``test_backtest_account_contract.test_market_order_fills_and_updates_ledger``."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

    arm = world.bt
    # The transaction's expert_id is resolved from the recommendation (see
    # AccountInterface._create_transaction_for_order), which is how a real entry links a
    # position to the expert whose balance it consumes.
    recommendation_id = add_instance(_recommendation(arm.instance_id))
    order = TradingOrder(account_id=arm.account.id, symbol=SYMBOL, quantity=ENTRY_QTY,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         status=OrderStatus.NEW, expert_recommendation_id=recommendation_id,
                         comment="parity entry")
    world.price_source.set_clock(_BAR_DATES[0])
    arm.account.submit_order(order)
    assert order.broker_order_id is not None
    arm.account.refresh_orders()          # MARKET fills at the NEXT bar's open ($100)

    filled = arm.account.get_order(order.broker_order_id)
    assert filled.status == OrderStatus.FILLED
    assert filled.open_price == pytest.approx(PRICE)
    assert arm.account.get_balance() == pytest.approx(4_000.0 - ENTRY_NOTIONAL)


def _open_live_position(arm: _Arm) -> None:
    """The same position on a live-shaped arm: the broker marks $1,000 of long market
    value and withholds $1,000 of buying power, EQUITY IS UNCHANGED (cash left, shares
    arrived), and the platform records the Transaction that consumes the expert's slice."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, OrderStatus, TransactionStatus

    transaction_id = add_instance(Transaction(
        symbol=SYMBOL, quantity=ENTRY_QTY, side=OrderDirection.BUY,
        status=TransactionStatus.OPENED, open_price=PRICE, expert_id=arm.instance_id))
    entry = _candidate(quantity=ENTRY_QTY, account_id=arm.account.id)
    entry.transaction_id = transaction_id
    entry.status = OrderStatus.FILLED
    entry.broker_order_id = f"brk-{arm.instance_id}"
    add_instance(entry)

    arm.account._snap.long_market_value = ENTRY_NOTIONAL
    arm.account._snap.buying_power -= ENTRY_NOTIONAL


@contextlib.contextmanager
def invested_world():
    """A world in which every arm holds the SAME position: 10 shares at $100, at zero
    P&L (the market is flat), bought out of the same $4,000 of effective capital."""
    with parity_world() as world:
        _open_backtest_position(world)
        _open_live_position(world.l1)
        _open_live_position(world.l2)
        yield world


def test_after_the_first_entry_the_two_live_fundings_still_agree():
    """The leverage feature's OWN claim, isolated from the backtest's cash/equity debt:
    once invested, $2,000-at-2x still reports exactly what $4,000-at-1x reports."""
    with invested_world() as world:
        levered = measure(world.l2, sizing_mode="notional")
        unlevered = measure(world.l1, sizing_mode="notional")

    assert levered == unlevered
    assert unlevered["virtual"] == pytest.approx(4_000.0)
    assert unlevered["available"] == pytest.approx(3_000.0)
    # $3,000 free, less the $1,000 already allocated to this symbol under the 100% cap.
    assert unlevered["quantity"] == pytest.approx(20.0)


def test_the_backtest_charges_the_position_twice_TODAY():
    """COMPANION PIN to the strict xfail below -- the CURRENT backtest numbers, asserted
    positively so the divergence is recorded from both ends.

    ``BacktestAccount.get_balance()`` returns CASH. The shared expert math then subtracts
    the position AGAIN as "used balance", so a $1,000 purchase costs the backtest expert
    $2,000 of headroom. Live, ``get_balance()`` is EQUITY and the position is charged once.

    If this test starts failing, the capital contract was changed: that is a deliberate,
    separately versioned correction (it moves every backtest result) and the xfail below
    will have flipped to XPASS in the same run.
    """
    with invested_world() as world:
        backtest = measure(world.bt, sizing_mode="notional")

    assert backtest["virtual"] == pytest.approx(3_000.0)     # cash, not equity
    assert backtest["available"] == pytest.approx(2_000.0)   # cash minus the position again
    assert backtest["quantity"] == pytest.approx(10.0)       # vs 20 live


@pytest.mark.xfail(strict=True, reason=(
    "finding 6: BacktestAccount.get_balance() is cash, live is equity; shared expert math "
    "charges the position twice in the backtest ($3,000/$2,000 vs $4,000/$3,000). "
    "Deliberately NOT fixed in the leverage feature: changing it moves every backtest "
    "result and is a separately versioned correction. See "
    "docs/plans/2026-09-09-margin-live-backtest-parity.md §6."))
def test_backtest_and_live_agree_after_the_first_entry():
    """THE PINNED BLOCKER (plan §6, finding 6).

    Flat, the three arms are identical (tests above). The moment a position exists they
    are not, and the cause is the pre-existing cash-vs-equity contract, not leverage --
    L1 and L2 (both live-shaped) still agree exactly.

    The assertion is stated as PARITY plus the LIVE reference numbers, deliberately NOT
    as the current backtest numbers: pinning $3,000/$2,000 in here would make the test
    keep xfailing after a fix (failing on the first assert instead of the second), and
    the strict xfail would then never fire. Written this way it XPASSes -- and so FAILS
    the suite -- on the day the contract is corrected.
    """
    with invested_world() as world:
        backtest = measure(world.bt, sizing_mode="notional")
        live = measure(world.l1, sizing_mode="notional")

    assert live["virtual"] == pytest.approx(4_000.0)
    assert live["available"] == pytest.approx(3_000.0)
    assert backtest["virtual"] == pytest.approx(live["virtual"])
    assert backtest["available"] == pytest.approx(live["available"])
    assert backtest["quantity"] == pytest.approx(live["quantity"])


# --------------------------------------------------------------------------- #
# 4. FINDING 4: the per-instrument ceiling is a percentage of what is LEFT.
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def half_invested_world():
    """The review's finding-4 scenario, on a live-shaped unlevered account: $18,000 of
    virtual equity, $9,000 of it already in ANOTHER instrument, a 10% per-instrument cap,
    and a fresh candidate in a third symbol."""
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    with parity_world(l1_balance=18_000.0) as world:
        arm = world.l1
        add_instance(Transaction(
            symbol=HELD_SYMBOL, quantity=90.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=PRICE, expert_id=arm.instance_id))
        arm.account._snap.buying_power = 11_000.0
        assert arm.expert.get_virtual_balance() == pytest.approx(18_000.0)
        assert arm.expert.get_available_balance() == pytest.approx(9_000.0)
        yield world, arm


def _size_prioritized(arm: _Arm, ratio: float):
    """``_size_prioritized_orders`` directly -- the sizing core both RM entry points share
    -- because the per-instrument ceiling it computes is a RETURN VALUE, not something a
    quantity alone can distinguish."""
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    candidate = _candidate(account_id=arm.account.id)
    result = TradeRiskManagement()._size_prioritized_orders(
        arm.expert, get_instance(ExpertInstance, arm.instance_id), arm.instance_id,
        [(candidate, _recommendation(arm.instance_id))], ratio)
    return candidate, result


def test_the_instrument_ceiling_is_ten_percent_of_the_REMAINING_funds_TODAY():
    """COMPANION PIN to the strict xfail below: today's number, asserted positively.

    ``max_equity_per_instrument = available x ratio`` -- 10% of the $9,000 still free,
    not 10% of the $18,000 the expert is allocated. So a half-invested expert's
    "10% per instrument" silently becomes 5% of its book.
    """
    with half_invested_world() as (_world, arm):
        candidate, result = _size_prioritized(arm, 0.10)
        total_virtual_balance, max_equity_per_instrument = result[-2:]

    assert total_virtual_balance == pytest.approx(9_000.0)
    assert max_equity_per_instrument == pytest.approx(900.0)
    assert candidate.quantity == 9


@pytest.mark.xfail(strict=True, reason=(
    "finding 4: classic per-instrument ceiling is available x ratio (900), not virtual x "
    "ratio (1800); changing it changes historical sizing — deferred, see plan §6"))
def test_the_instrument_ceiling_is_ten_percent_of_the_virtual_equity():
    """THE PINNED BLOCKER (plan §6, finding 4).

    ``max_virtual_equity_per_instrument_percent`` says VIRTUAL EQUITY, and the setting's
    own tooltip describes a notional ceiling on the expert's book -- so 10% of $18,000 is
    $1,800, whatever fraction of the book happens to be deployed today. Correcting the
    denominator changes historical sizing for every classic-RM backtest ever run, so it is
    explicitly out of scope for the (result-neutral) leverage feature and pinned here
    instead.
    """
    with half_invested_world() as (_world, arm):
        candidate, result = _size_prioritized(arm, 0.10)
        max_equity_per_instrument = result[-1]

    assert max_equity_per_instrument == pytest.approx(1_800.0)
    assert candidate.quantity == 18
