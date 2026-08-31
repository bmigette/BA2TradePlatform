"""``loss_pct_of_max_loss`` -- a stop scaled to DEFINED risk, not to credit.

value = unrealized loss / (persisted ``max_loss_per_contract`` x contracts) x 100,
POSITIVE while losing. +100 means the whole defined risk is gone. Scale-free by
construction: the denominator multiplies the same contract count the P&L already carries.

The denominator is READ BACK off the parent order's ``data``, where the submit path
persisted it (test_max_loss_persisted_at_submit.py) -- no leg reconstruction, no OCC
parsing. That makes "contracts that support it" a property of the DATA: a structure whose
max loss was not MEASURED at submit has no key, and this condition is then UNEVALUABLE.

UNKNOWN NEVER FIRES -- in EITHER direction. The governing discipline is
``DaysToExpiryCondition``'s: when the denominator is absent, zero, negative, stringly
typed, or the position's P&L cannot be resolved, ``calculated_value`` stays ``None`` and
``evaluate()`` is False for every operator. The two defaults this file pins against:

* absence read as some number (say the credit, or 1.0) -> a stop fires on a position
  whose risk was never measured;
* an unevaluable read as 0 % -> ``loss_pct_of_max_loss < N`` fires on sight for any
  position we merely failed to price.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_types import OptionQuote
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderStatus, OrderType,
    TransactionStatus,
)

SIM_AS_OF = datetime(2024, 6, 15, 15, 30, tzinfo=timezone.utc)
EXPIRY = date(2024, 7, 19)
SHORT_PUT = "XYZ240719P00100000"
LONG_PUT = "XYZ240719P00095000"


@pytest.fixture(autouse=True)
def _own_db(tmp_path):
    from ba2_common.core import db
    db.configure_db(str(tmp_path / "loss_pct_of_max_loss.sqlite"))
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
    return SimpleNamespace(created_at=SIM_AS_OF, instance_id=1, symbol="XYZ")


def _seed_bull_put(db, *, structures=1, data=None):
    """Open bull put spread: SELL 100p @ 2.50, BUY 95p @ 1.00 -> net credit 1.50.
    Max loss of one contract = (5.00 - 1.50) x 100 = $350."""
    from ba2_common.core.models import TradingOrder, Transaction

    txn = Transaction(
        symbol="XYZ", quantity=structures, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=10),
        open_price=1.50, asset_class=AssetClass.OPTION,
        option_strategy="bull_put_spread", multiplier=100, expiry=EXPIRY,
    )
    txn_id = db.add_instance(txn)

    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=structures, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        filled_qty=structures, open_price=1.50, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        option_strategy="bull_put_spread", underlying_symbol="XYZ", multiplier=100,
        expiry=EXPIRY, data=data,
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)

    for contract, side, entry in ((SHORT_PUT, OrderDirection.SELL, 2.50),
                                  (LONG_PUT, OrderDirection.BUY, 1.00)):
        leg = TradingOrder(
            account_id=1, symbol="XYZ", quantity=structures, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            filled_qty=structures, open_price=entry, transaction_id=txn_id,
            parent_order_id=parent.id, asset_class=AssetClass.OPTION,
            contract_symbol=contract, underlying_symbol="XYZ",
            option_type=OptionRight.PUT,
            strike=100.0 if contract == SHORT_PUT else 95.0,
            multiplier=100, expiry=EXPIRY,
            created_at=datetime.now(timezone.utc),
        )
        db.add_instance(leg, expunge_after_flush=True)
    return parent


#: Marks that put the structure $100/contract underwater: buying the short back costs
#: 4.00, the long sells for 1.50 -> net now 2.50 vs 1.50 collected. Loss = $100/contract
#: = 100/350 = 28.571 % of max loss.
LOSING_QUOTES = {SHORT_PUT: (3.90, 4.00, 3.95), LONG_PUT: (1.50, 1.60, 1.55)}


def _cond(order, *, op=">", value=25.0, quotes=None):
    from ba2_common.core.TradeConditions import LossPctOfMaxLossCondition
    return LossPctOfMaxLossCondition(
        account=FakeAccount(quotes if quotes is not None else LOSING_QUOTES),
        instrument_name="XYZ", expert_recommendation=_rec(),
        operator_str=op, value=value, existing_order=order,
    )


STAMP = {"max_loss_per_contract": 350.0}


# ---------------------------------------------------------------------------
# fires at the threshold, off the persisted denominator
# ---------------------------------------------------------------------------
def test_fires_at_the_threshold_with_the_persisted_denominator(tmp_path):
    from ba2_common.core import db
    parent = _seed_bull_put(db, data=dict(STAMP))

    cond = _cond(parent, op=">", value=25.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(100.0 / 350.0 * 100.0, abs=0.01)

    assert _cond(parent, op=">", value=30.0).evaluate() is False


def test_the_same_percentage_for_one_and_five_contracts(tmp_path):
    """SCALE-FREE. The whole point of a max-loss-basis stop: 5 contracts lose 5x the
    dollars against 5x the defined risk, so the percentage is identical."""
    from ba2_common.core import db
    one = _seed_bull_put(db, structures=1, data=dict(STAMP))
    five = _seed_bull_put(db, structures=5, data=dict(STAMP))

    c1, c5 = _cond(one), _cond(five)
    assert c1.evaluate() is True and c5.evaluate() is True
    assert c1.calculated_value == pytest.approx(c5.calculated_value)


def test_a_profitable_position_reads_negative_and_a_loss_stop_cannot_fire(tmp_path):
    """Positive means LOSING. A winner reads negative, so ``> N`` can never fire on it."""
    from ba2_common.core import db
    parent = _seed_bull_put(db, data=dict(STAMP))
    winning = {SHORT_PUT: (0.90, 1.00, 0.95), LONG_PUT: (0.50, 0.60, 0.55)}

    cond = _cond(parent, op=">", value=0.0, quotes=winning)
    assert cond.evaluate() is False
    assert cond.calculated_value is not None and cond.calculated_value < 0


def test_the_single_leg_path_prices_off_the_option_premium(tmp_path):
    """A cash-secured put (single-leg parent WITH a contract_symbol) routes down the
    single-leg option P&L path and divides by the same persisted denominator.
    Entry 4.00 credit, buy-back now 6.00 -> $200/contract loss over a persisted
    (100 - 4) x 100 = $9,600 -> 2.083 %."""
    from ba2_common.core import db
    from ba2_common.core.models import TradingOrder, Transaction

    txn = Transaction(
        symbol="XYZ", quantity=2, side=OrderDirection.SELL,
        status=TransactionStatus.OPENED, open_date=SIM_AS_OF - timedelta(days=5),
        open_price=4.00, asset_class=AssetClass.OPTION,
        option_strategy="cash_secured_put", multiplier=100, expiry=EXPIRY,
    )
    txn_id = db.add_instance(txn)
    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=2, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, filled_qty=2,
        open_price=4.00, transaction_id=txn_id, asset_class=AssetClass.OPTION,
        contract_symbol=SHORT_PUT, underlying_symbol="XYZ",
        option_type=OptionRight.PUT, strike=100.0, multiplier=100, expiry=EXPIRY,
        data={"max_loss_per_contract": 9600.0},
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)

    cond = _cond(parent, op=">", value=2.0,
                 quotes={SHORT_PUT: (5.90, 6.00, 5.95)})
    assert cond.evaluate() is True
    assert cond.calculated_value == pytest.approx(400.0 / 19200.0 * 100.0, abs=0.01)


# ---------------------------------------------------------------------------
# unknown never fires -- in either direction
# ---------------------------------------------------------------------------
def test_absence_of_the_persisted_max_loss_never_fires_in_either_direction(tmp_path):
    """THE ABSENCE GATE (mutation target). The position is losing badly and the P&L is
    perfectly computable -- but no ``max_loss_per_contract`` was persisted, so there is
    no denominator. An implementation that substitutes ANY number here fires one of
    these two operators; refusing to evaluate fires neither."""
    from ba2_common.core import db
    parent = _seed_bull_put(db, data=None)

    gt = _cond(parent, op=">", value=0.001)
    assert gt.evaluate() is False
    assert gt.calculated_value is None

    lt = _cond(parent, op="<", value=1e9)
    assert lt.evaluate() is False
    assert lt.calculated_value is None


def test_a_data_dict_without_the_key_never_fires(tmp_path):
    """Same gate when ``data`` exists but carries only its neighbours (option_reserve)."""
    from ba2_common.core import db
    parent = _seed_bull_put(db, data={"option_reserve": 350.0})
    assert _cond(parent, op=">", value=0.001).evaluate() is False
    assert _cond(parent, op="<", value=1e9).evaluate() is False


@pytest.mark.parametrize("bad", [0.0, -350.0, "350.0", True, float("nan")])
def test_a_zero_negative_or_stringly_persisted_value_never_fires(tmp_path, bad):
    """Zero is not a denominator, a negative max loss is not a measurement, a string is
    a bug to surface, True is an int in a trenchcoat, NaN passes every comparison."""
    from ba2_common.core import db
    parent = _seed_bull_put(db, data={"max_loss_per_contract": bad})
    gt = _cond(parent, op=">", value=0.001)
    assert gt.evaluate() is False
    assert gt.calculated_value is None
    assert _cond(parent, op="<", value=1e9).evaluate() is False


def test_an_unresolvable_position_never_fires(tmp_path):
    """Denominator present, P&L NOT computable (no quote for a held leg): unevaluable.
    The ``<`` direction is the one a '0 % by default' implementation would fire."""
    from ba2_common.core import db
    parent = _seed_bull_put(db, data=dict(STAMP))

    gt = _cond(parent, op=">", value=0.001, quotes={})
    assert gt.evaluate() is False
    assert gt.calculated_value is None
    assert _cond(parent, op="<", value=1e9, quotes={}).evaluate() is False


def test_an_order_with_no_transaction_never_fires(tmp_path):
    from ba2_common.core import db
    from ba2_common.core.models import TradingOrder

    parent = TradingOrder(
        account_id=1, symbol="XYZ", quantity=1, side=OrderDirection.SELL,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED,
        asset_class=AssetClass.OPTION, contract_symbol=None,
        underlying_symbol="XYZ", multiplier=100, data=dict(STAMP),
        created_at=datetime.now(timezone.utc),
    )
    db.add_instance(parent, expunge_after_flush=True)
    assert _cond(parent, op=">", value=0.001).evaluate() is False
    assert _cond(parent, op="<", value=1e9).evaluate() is False


def test_no_existing_order_never_fires(tmp_path):
    assert _cond(None, op=">", value=0.001).evaluate() is False
    assert _cond(None, op="<", value=1e9).evaluate() is False


# ---------------------------------------------------------------------------
# wiring: a rule leaf can reach it, and it is documented
# ---------------------------------------------------------------------------
def test_a_rule_leaf_naming_the_field_becomes_a_trigger():
    """The registry-closure suite (test_condition_registry_coverage) enforces the maps
    stay total; this pins the FIELD NAME a ruleset writes."""
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    triggers = triggers_from_condition_tree({"type": "AND", "conditions": [
        {"id": "sl_ml", "field": "loss_pct_of_max_loss", "op": ">", "value": 50}]})
    (only,) = triggers.values()
    assert only == {"event_type": "loss_pct_of_max_loss", "operator": ">", "value": 50}


def test_it_is_documented_so_it_is_discoverable():
    from ba2_common.core.rules_documentation import get_event_type_documentation

    doc = get_event_type_documentation()["loss_pct_of_max_loss"]
    assert doc["type"] == "numeric"
    assert "max loss" in doc["description"].lower()
    assert "not fire" in doc["description"].lower() or "unevaluable" in doc["description"].lower()
