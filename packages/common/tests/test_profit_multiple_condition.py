"""``profit_multiple_of_premium`` -- a take-profit scaled to what a LONG structure paid.

value = current structure value / entry premium (net debit) = 1 + pnl_percent / 100,
riding the SAME ``_get_pnl_for_condition`` plumbing every option P&L condition uses
(single-leg via ``TransactionHelper.calculate_option_pnl``, multi-leg via
``_get_spread_pnl_via_transaction``) -- both already divide the dollar P&L by the entry
premium x contracts x multiplier, so the "+1" turns that percentage into the ratio of
current value to entry cost. Scale-free by the same construction as
``LossPctOfMaxLossCondition``: the denominator carries the same contract count the
numerator does, so 1 contract and 5 contracts read the identical multiple.

A "multiple of premium PAID" is only coherent for a DEBIT entry. The transaction's
``open_price`` is always stored as an absolute magnitude (see
``_get_spread_pnl_via_transaction``'s docstring); the SIGN lives in ``transaction.side``:
BUY == debit == positive premium, SELL == credit == negative. So the condition refuses --
NEVER fires, in EITHER operator direction -- whenever the transaction is not a BUY.

UNKNOWN NEVER FIRES -- the ``DaysToExpiryCondition``/``LossPctOfMaxLossCondition``
discipline. No existing_order, no transaction, a credit (SELL) entry, or a P&L that
``_get_pnl_for_condition`` cannot resolve (missing quote, missing multiplier, flat
structure) each leave ``calculated_value`` None and ``evaluate()`` False for EVERY
operator. The two defaults specifically refused:

* a credit structure reads as firing (denominator sign mishandled) -> a stop/TP fires on
  a position that never paid a premium to be "worth a multiple of";
* an unevaluable position reads as 0-and-fires on ``<`` -> any downward gate fires for
  every position we merely failed to price, while looking configured.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionQuote
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType,
    TransactionStatus,
)

SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
EXPIRY = date(2024, 7, 19)
LONG_CALL = "XYZ240719C00100000"
SHORT_CALL = "XYZ240719C00110000"


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "profit_multiple.sqlite"))
    db.init_db()
    yield


class FakeAccount(OptionsAccountInterface):
    """Options account serving canned per-contract quotes: {symbol: (bid, ask, last)}."""

    def __init__(self, quotes=None):
        self.id = 1
        self.quotes = quotes or {}

    def get_option_quote(self, contract_symbol):
        if contract_symbol not in self.quotes:
            return None
        bid, ask, last = self.quotes[contract_symbol]
        return OptionQuote(symbol=contract_symbol, bid=bid, ask=ask, last=last)

    # --- unused abstract bits
    def get_balance(self):
        return 100_000.0

    def get_instrument_current_price(self, symbol, price_type=None):
        return 100.0

    def get_current_price(self, symbol=None):
        return 100.0

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        return []

    def submit_option_order(self, *, legs, quantity, order_type, limit_price,
                            option_strategy, expert_recommendation_id=None,
                            transaction_id=None):
        return None

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        return None


def _rec():
    from types import SimpleNamespace
    return SimpleNamespace(created_at=SIM_AS_OF, instance_id=1, symbol="XYZ")


def _seed_single_leg(db, *, side, contracts=1, entry=2.00, contract=LONG_CALL):
    """A single-leg option parent order (contract_symbol set) -> the
    ``_get_option_pnl_via_transaction`` path."""
    from ba2_common.core.models import TradingOrder, Transaction

    txn = Transaction(
        symbol="XYZ", quantity=contracts, side=side,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=10),
        open_price=entry, asset_class=AssetClass.OPTION,
        option_strategy="long_call" if side == OrderDirection.BUY else "short_call",
        multiplier=100, expiry=EXPIRY,
    )
    txn_id = db.add_instance(txn)

    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=contracts, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=contracts, open_price=entry, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=contract,
        underlying_symbol="XYZ", option_type=OptionRight.CALL, strike=100.0,
        multiplier=100, expiry=EXPIRY,
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)
    return parent


def _cond(order, *, op=">=", value=3.0, quotes=None):
    from ba2_common.core.TradeConditions import ProfitMultipleOfPremiumCondition
    return ProfitMultipleOfPremiumCondition(
        account=FakeAccount(quotes if quotes is not None else {}),
        instrument_name="XYZ", expert_recommendation=_rec(),
        operator_str=op, value=value, existing_order=order,
    )


# ---------------------------------------------------------------------------
# fires at the threshold, hand-derived arithmetic
# ---------------------------------------------------------------------------
def test_fires_at_the_threshold_with_hand_derived_arithmetic():
    """Entry debit 2.00, structure now worth 6.50 -> multiple 3.25.
    pnl_amount = (6.50 - 2.00) x 1 x 100 = 450; cost_basis = 2.00 x 1 x 100 = 200;
    pnl_pct = 225 %; multiple = 1 + 2.25 = 3.25."""
    from ba2_common.core import db
    parent = _seed_single_leg(db, side=OrderDirection.BUY, entry=2.00)
    quotes = {LONG_CALL: (6.50, 6.60, 6.55)}

    cond = _cond(parent, op=">=", value=3.0, quotes=quotes)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(3.25, abs=1e-6)

    assert _cond(parent, op=">=", value=3.5, quotes=quotes).evaluate() is False


# ---------------------------------------------------------------------------
# credit structures never fire -- in either direction
# ---------------------------------------------------------------------------
def test_a_credit_single_leg_never_fires_in_either_direction():
    """A SHORT (SELL) option collected a credit -- there is no "multiple of premium paid"
    for it. Even though the position is deep in the money for the writer (a huge notional
    loss), the condition must refuse to evaluate rather than compute a bogus multiple."""
    from ba2_common.core import db
    parent = _seed_single_leg(db, side=OrderDirection.SELL, entry=2.00, contract=SHORT_CALL)
    quotes = {SHORT_CALL: (6.50, 6.60, 6.55)}

    hi = _cond(parent, op=">=", value=0.001, quotes=quotes)
    assert hi.evaluate() is False
    assert hi.calculated_value is None

    lo = _cond(parent, op="<", value=1e9, quotes=quotes)
    assert lo.evaluate() is False
    assert lo.calculated_value is None


def test_a_credit_spread_never_fires_in_either_direction():
    """A net-credit multi-leg structure (SELL parent): open_net is negative by
    construction (_get_spread_pnl_via_transaction), so the debit-multiple reading is
    undefined -- must refuse, not silently flip sign."""
    from ba2_common.core import db
    from ba2_common.core.models import TradingOrder, Transaction

    txn = Transaction(
        symbol="XYZ", quantity=1, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=10),
        open_price=1.50, asset_class=AssetClass.OPTION,
        option_strategy="bull_put_spread", multiplier=100, expiry=EXPIRY,
    )
    txn_id = db.add_instance(txn)
    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=1, open_price=1.50, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        option_strategy="bull_put_spread", underlying_symbol="XYZ", multiplier=100,
        expiry=EXPIRY, created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)

    short_put = "XYZ240719P00100000"
    long_put = "XYZ240719P00095000"
    for contract, side, entry in ((short_put, OrderDirection.SELL, 2.50),
                                  (long_put, OrderDirection.BUY, 1.00)):
        leg = TradingOrder(
            account_id=1, symbol="XYZ", quantity=1, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=1, open_price=entry, transaction_id=txn_id,
            parent_order_id=parent.id, asset_class=AssetClass.OPTION,
            contract_symbol=contract, underlying_symbol="XYZ",
            option_type=OptionRight.PUT,
            strike=100.0 if contract == short_put else 95.0,
            multiplier=100, expiry=EXPIRY, created_at=datetime.now(timezone.utc),
        )
        db.add_instance(leg, expunge_after_flush=True)

    quotes = {short_put: (0.10, 0.20, 0.15), long_put: (0.01, 0.05, 0.03)}
    hi = _cond(parent, op=">=", value=0.001, quotes=quotes)
    assert hi.evaluate() is False
    assert hi.calculated_value is None

    lo = _cond(parent, op="<", value=1e9, quotes=quotes)
    assert lo.evaluate() is False
    assert lo.calculated_value is None


# ---------------------------------------------------------------------------
# unknown never fires -- in either direction
# ---------------------------------------------------------------------------
def test_no_existing_order_never_fires():
    assert _cond(None, op=">=", value=0.001).evaluate() is False
    assert _cond(None, op="<", value=1e9).evaluate() is False


def test_an_order_with_no_transaction_never_fires():
    from ba2_common.core import db
    from ba2_common.core.models import TradingOrder

    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.BUY,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, contract_symbol=LONG_CALL,
        underlying_symbol="XYZ", multiplier=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)
    assert _cond(parent, op=">=", value=0.001).evaluate() is False
    assert _cond(parent, op="<", value=1e9).evaluate() is False


def test_an_unresolvable_position_never_fires():
    """BUY side, denominator resolvable, but no quote for the held contract -- the P&L
    machinery returns None. The ``<`` direction is the one a '0-by-default' bug fires."""
    from ba2_common.core import db
    parent = _seed_single_leg(db, side=OrderDirection.BUY, entry=2.00)

    hi = _cond(parent, op=">=", value=0.001, quotes={})
    assert hi.evaluate() is False
    assert hi.calculated_value is None
    assert _cond(parent, op="<", value=1e9, quotes={}).evaluate() is False


# ---------------------------------------------------------------------------
# scale-free across contract counts
# ---------------------------------------------------------------------------
def test_the_same_multiple_for_one_and_five_contracts():
    from ba2_common.core import db
    one = _seed_single_leg(db, side=OrderDirection.BUY, contracts=1, entry=2.00,
                           contract=LONG_CALL)
    five_contract = "XYZ240719C00100001"
    five = _seed_single_leg(db, side=OrderDirection.BUY, contracts=5, entry=2.00,
                            contract=five_contract)

    quotes = {LONG_CALL: (6.50, 6.60, 6.55), five_contract: (6.50, 6.60, 6.55)}
    c1 = _cond(one, op=">=", value=3.0, quotes=quotes)
    c5 = _cond(five, op=">=", value=3.0, quotes=quotes)
    assert c1.evaluate() is True and c5.evaluate() is True
    assert c1.calculated_value == pytest.approx(c5.calculated_value)


# ---------------------------------------------------------------------------
# wiring: a rule leaf can reach it, and it is documented
# ---------------------------------------------------------------------------
def test_a_rule_leaf_naming_the_field_becomes_a_trigger():
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    triggers = triggers_from_condition_tree({"type": "AND", "conditions": [
        {"id": "tp_mult", "field": "profit_multiple_of_premium", "op": ">=", "value": 3.0}]})
    (only,) = triggers.values()
    assert only == {"event_type": "profit_multiple_of_premium", "operator": ">=", "value": 3.0}


def test_it_is_documented_so_it_is_discoverable():
    from ba2_common.core.rules_documentation import get_event_type_documentation

    doc = get_event_type_documentation()["profit_multiple_of_premium"]
    assert doc["type"] == "numeric"
    assert "multiple" in doc["description"].lower()
    assert "not fire" in doc["description"].lower() or "never fire" in doc["description"].lower()


def test_it_is_in_the_numeric_field_registry():
    from ba2_common.core.types import ExpertEventType, get_numeric_event_values, is_numeric_event

    assert ExpertEventType.N_PROFIT_MULTIPLE_OF_PREMIUM.value == "profit_multiple_of_premium"
    assert "profit_multiple_of_premium" in get_numeric_event_values()
    assert is_numeric_event("profit_multiple_of_premium")


def test_classified_as_discretionary_not_forced():
    """The PROFIT side (``>=``) -- like ``opt_tp`` -- is discretionary, independently of
    whatever the LOSS side (``<``) is registered as. The LOSS side is a de-facto stop,
    affine-identical to ``profit_loss_percent < 0`` (``multiple = 1 + percent / 100``),
    registered in TradeActionEvaluator._LOSS_SIDE_STOP_OPERATORS under the SAME "<"/"<="
    convention as the ``profit_loss_*`` pair -- pinned separately by
    test_option_close_concession.py's review-table extension and
    test_profit_multiple_of_premium_below_one_classifies_as_forced, so the two directions
    are independently verified (this test must keep passing even if that row were
    dropped -- only the ``<`` behavior would break, never this one)."""
    from ba2_common.core.TradeActionEvaluator import forced_option_exit
    from types import SimpleNamespace

    action = SimpleNamespace(name="rule", triggers={
        "c0": {"event_type": "profit_multiple_of_premium", "operator": ">=", "value": 3.0}},
        actions={})
    assert forced_option_exit(action) is False
