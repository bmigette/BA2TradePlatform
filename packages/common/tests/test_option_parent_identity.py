"""The filled parent row says WHICH contracts, and the Transaction says WHAT WAS MEANT.

Two facts measured on the live book motivated these tests:

  * of 28 FILLED option orders only 11 recorded which contract was traded, because
    ``submit_option_order`` nulled ``contract_symbol``/``option_type``/``strike``/``expiry``
    on a multi-leg parent — and the parent is the row the broker fills; and
  * ``OptionPortfolioManager._should_close`` reads ``parent.expiry``, which was NULL for
    every multi-leg, so the design's headline exit ("manage at 21 DTE to avoid end-of-life
    gamma") had **never fired** for ``put_credit_spread`` or ``short_strangle``. Every GA
    result for those families was produced with a dead roll gene.

Nulling ``contract_symbol``/``option_type``/``strike`` on a multi-leg parent is CORRECT and is
pinned here: four legs have four contracts and one parent, so any single one of them recorded
there would read as "the" contract of the position. ``expiry`` is the exception — Task 2's
single-expiry guard runs above every write, so by the time a parent is built the structure is
guaranteed to sit on one date, and that one date is a fact about the whole position.

Dates are frozen constants, never ``date.today()``: a date that happens to equal the wall clock
has let a mutation survive in this repo before.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance, get_instance, update_instance
from ba2_common.core.interfaces import OptionsAccountInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import AssetClass, OptionRight, OrderDirection, OrderStatus

#: One real monthly expiry, in the past relative to no particular clock.
AUG = date(2026, 8, 21)


def _occ(strike: float, right: OptionRight, expiry: date) -> str:
    """A real OCC contract string for ACN — the expiry and strike are encoded in it."""
    return (f"ACN{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
            f"{int(strike * 1000):08d}")


def _leg(strike=130.0, right=OptionRight.CALL, side=OrderDirection.BUY,
         intent="buy_to_open", expiry=AUG):
    """One leg on ACN. ``expiry=None`` is the shape the close paths rebuild (Task 2)."""
    return OptionLeg(
        contract_symbol=(_occ(strike, right, expiry) if expiry is not None
                         else _occ(strike, right, AUG)),
        side=side, position_intent=intent, option_type=right, strike=strike,
        expiry=expiry, underlying="ACN")


class _OptionAccount(OptionsAccountInterface):
    """Concrete option-capable account double: no broker SDK, no network, no chain fetch.

    Persistence runs through the REAL ``submit_option_order``; only the broker call and the
    transaction factory are stubbed. The factory below writes exactly the columns
    ``AccountInterface._create_transaction_for_order`` writes — symbol, quantity, side,
    multiplier — and deliberately NOT the three intent columns, so a test that sees them
    populated saw ``submit_option_order`` populate them.
    """

    def __init__(self, account_id: int = 1):
        self.id = account_id
        self.submitted: list = []

    # --- abstract market-data surface: unreachable in these tests -----------
    def get_option_chain(self, *a, **k):
        raise AssertionError("these tests must never fetch a chain")

    def get_option_quote(self, contract_symbol):
        raise AssertionError("these tests must never fetch a quote")

    def get_atm_implied_volatility(self, underlying):
        raise AssertionError("these tests must never fetch IV")

    def get_option_positions(self):
        return []

    def close_option_position(self, position, order_type="limit", limit_price=None):
        raise AssertionError("these tests never close through this entry point")

    # --- the two seams submit_option_order reaches into ---------------------
    def _create_transaction_for_order(self, trading_order):
        trading_order.transaction_id = add_instance(Transaction(
            symbol=trading_order.symbol, quantity=trading_order.quantity,
            side=trading_order.side, multiplier=100, expert_id=None))

    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        self.submitted.append((trading_order, list(legs), list(leg_orders or [])))
        trading_order.status = OrderStatus.FILLED
        trading_order.broker_order_id = f"double-{trading_order.id}"
        update_instance(trading_order)
        for child in (leg_orders or []):
            child.status = OrderStatus.FILLED
            update_instance(child)
        return trading_order


@pytest.fixture
def store():
    """A fresh in-RAM order/transaction store per test."""
    with ts.inmem_trades() as s:
        yield s


@pytest.fixture
def account():
    return _OptionAccount()


def _bull_call_spread(account, expiry=AUG, **kwargs):
    """The two-leg debit vertical: long 130 call / short 140 call."""
    legs = [_leg(strike=130.0, expiry=expiry),
            _leg(strike=140.0, side=OrderDirection.SELL, intent="sell_to_open", expiry=expiry)]
    return account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=4.0,
                                       option_strategy="bull_call_spread", **kwargs)


def _iron_condor(account, expiry=AUG, **kwargs):
    """Four legs, four contracts, one parent — the widest structure the platform supports."""
    legs = [
        _leg(strike=120.0, right=OptionRight.PUT, side=OrderDirection.SELL,
             intent="sell_to_open", expiry=expiry),
        _leg(strike=110.0, right=OptionRight.PUT, expiry=expiry),
        _leg(strike=140.0, side=OrderDirection.SELL, intent="sell_to_open", expiry=expiry),
        _leg(strike=150.0, expiry=expiry),
    ]
    return account.submit_option_order(legs, quantity=1, order_type="limit", limit_price=-1.2,
                                       option_strategy="iron_condor", **kwargs)


# --- the parent order -------------------------------------------------------

def test_a_multi_leg_parent_records_the_shared_expiry(store, account):
    """The roll-at-DTE gene reads parent.expiry. NULL there is why it has never fired."""
    parent = _bull_call_spread(account)

    assert parent.expiry == AUG


def test_a_four_leg_parent_records_the_shared_expiry_too(store, account):
    """Not a two-leg special case: an iron condor's four legs share one date as well."""
    parent = _iron_condor(account)

    assert parent.expiry == AUG


def test_a_multi_leg_parent_still_has_no_single_contract(store, account):
    """Four legs, four contracts, one parent. contract_symbol stays None — honest, not lazy."""
    parent = _iron_condor(account)

    assert parent.contract_symbol is None, "one of four contracts would read as 'the' contract"
    assert parent.option_type is None, "an iron condor is neither a call nor a put"
    assert parent.strike is None, "an iron condor has four strikes; none of them is 'the' strike"
    # The legs are where the contracts live, and each one is complete.
    children = [o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]
    assert len(children) == 4
    assert all(c.contract_symbol and c.strike is not None and c.expiry == AUG for c in children)


def test_a_single_leg_order_records_its_full_contract(store, account):
    """One leg IS one contract, so the parent can and must name it. Regression pin."""
    parent = account.submit_option_order([_leg(strike=130.0)], quantity=2, order_type="limit",
                                         limit_price=5.2, option_strategy="long_call")

    assert parent.contract_symbol == "ACN260821C00130000"
    assert parent.option_type is OptionRight.CALL
    assert parent.strike == 130.0
    assert parent.expiry == AUG


def test_the_parent_symbol_stays_the_underlying(store, account):
    """JobManager selects distinct symbols and submits a market analysis per value; an OCC
    string there would be analysed as a ticker."""
    parent = _bull_call_spread(account)

    assert parent.symbol == "ACN"
    assert parent.underlying_symbol == "ACN"


# --- the transaction --------------------------------------------------------

def test_the_transaction_records_the_intent(store, account):
    """asset_class OPTION, the strategy, the expiry — and the symbol still the UNDERLYING."""
    parent = _bull_call_spread(account)

    txn = get_instance(Transaction, parent.transaction_id)
    assert txn.asset_class is AssetClass.OPTION
    assert txn.option_strategy == "bull_call_spread"
    assert txn.expiry == AUG
    assert txn.symbol == "ACN", "symbol must stay the UNDERLYING, never an OCC string"


def test_a_pre_created_transaction_is_stamped_too(store, account):
    """The PremiumSeller open path supplies its own transaction (expert attribution).

    ``OptionPortfolioManager.rebalance`` writes the Transaction itself and passes
    ``transaction_id=`` so the structure is attributed to the expert. That is the path every
    ``short_strangle`` and ``put_credit_spread`` in the GA took, so an intent stamp that only
    fired for transactions created inside ``submit_option_order`` would miss precisely the
    families the roll gene was dead for.
    """
    txn_id = add_instance(Transaction(symbol="ACN", quantity=1, side=OrderDirection.SELL,
                                      multiplier=100, expert_id=None))
    parent = _iron_condor(account, transaction_id=txn_id)

    assert parent.transaction_id == txn_id
    txn = get_instance(Transaction, txn_id)
    assert txn.asset_class is AssetClass.OPTION
    assert txn.option_strategy == "iron_condor"
    assert txn.expiry == AUG


def test_an_equity_transaction_is_untouched_by_all_this(store, account):
    """Nothing here may reach a share position: only orders that go through
    ``submit_option_order`` stamp anything."""
    equity_id = add_instance(Transaction(symbol="ACN", quantity=100, side=OrderDirection.BUY))
    _bull_call_spread(account)

    equity = get_instance(Transaction, equity_id)
    assert equity.asset_class is AssetClass.EQUITY
    assert equity.option_strategy is None and equity.expiry is None


# --- closing a structure ----------------------------------------------------

def test_a_close_does_not_rewrite_the_opening_intent(store, account):
    """"close" is what is being DONE, not what the position WAS.

    Both close paths (``TradeActions`` and ``OptionPortfolioManager._close_structure``) submit
    offsetting legs with ``option_strategy="close"`` on the SAME transaction. If that were
    allowed to overwrite the intent, every flattened structure in the book would report itself
    as a "close" and the strategy family would be unrecoverable from the ledger.
    """
    parent = _bull_call_spread(account)
    txn_id = parent.transaction_id

    account.submit_option_order(
        [_leg(strike=130.0, side=OrderDirection.SELL, intent="sell_to_close"),
         _leg(strike=140.0, intent="buy_to_close")],
        quantity=1, order_type="market", option_strategy="close", transaction_id=txn_id)

    txn = get_instance(Transaction, txn_id)
    assert txn.option_strategy == "bull_call_spread", "the close overwrote the opening intent"
    assert txn.expiry == AUG
    assert txn.asset_class is AssetClass.OPTION


def test_a_close_whose_legs_carry_no_expiry_still_writes(store, account):
    """The ``PremiumSeller`` flatten shape: every rebuilt leg has ``expiry=None``.

    ``_close_structure`` reads ``getattr(o, "expiry", None)`` off stored order rows, so a row
    written before the option columns existed yields a leg with no expiry. Task 2 deliberately
    treats that as UNKNOWN rather than as a second expiry, because refusing it would strand an
    open position that could no longer be flattened. The parent must therefore be written with
    ``expiry`` left NULL — not crash, and not invent a date.
    """
    legs = [_leg(strike=130.0, side=OrderDirection.SELL, intent="sell_to_close", expiry=None),
            _leg(strike=140.0, intent="buy_to_close", expiry=None)]

    parent = account.submit_option_order(legs, quantity=1, order_type="market",
                                         option_strategy="close")

    assert parent is not None, "an unflattenable position is far worse than an unknown expiry"
    assert parent.status == OrderStatus.FILLED
    assert parent.expiry is None, "a date was invented for legs that carry none"
    children = [o for o in store.all(TradingOrder) if o.parent_order_id == parent.id]
    assert len(children) == 2 and all(c.expiry is None for c in children)
    # The transaction is still recognisably an option position; the expiry stays unknown.
    txn = get_instance(Transaction, parent.transaction_id)
    assert txn.asset_class is AssetClass.OPTION
    assert txn.expiry is None


def test_a_partly_unrecorded_expiry_stamps_the_one_date_that_is_known(store, account):
    """One leg dated, one not. The known date is a fact; the unknown one is not a second date."""
    legs = [_leg(strike=130.0, expiry=AUG),
            _leg(strike=140.0, side=OrderDirection.SELL, intent="sell_to_close", expiry=None)]

    parent = account.submit_option_order(legs, quantity=1, order_type="market",
                                         option_strategy="close")

    assert parent.expiry == AUG


def test_a_stale_transaction_id_costs_the_intent_but_never_the_order(store, account, monkeypatch):
    """A reporting gap is survivable; an order that never reached the broker is not.

    The parent and its legs are already written by the time the intent is stamped and the
    caller is about to submit, so raising here would leave a structure the platform believes
    it holds with no broker order behind it. NOT ``caplog``: the package logger sets
    ``propagate = False`` (``packages/common/ba2_common/logger.py``), so caplog's root handler
    never sees the record. The patch goes on the shared logger OBJECT because the module under
    test imports it inside the function body, so there is no module attribute to patch.
    """
    from ba2_common.logger import logger
    errors: list = []
    monkeypatch.setattr(logger, "error", lambda msg, *a, **k: errors.append(str(msg)))

    parent = _bull_call_spread(account, transaction_id=4242)   # no such transaction

    assert parent is not None and parent.status == OrderStatus.FILLED
    assert len(account.submitted) == 1, "the broker must still have been reached"
    assert any("4242" in message and "NOT recorded" in message for message in errors), errors


# --- the consequence --------------------------------------------------------

def test_the_roll_at_dte_window_can_now_be_computed_from_the_parent(store, account):
    """``OptionPortfolioManager._should_close`` runs exactly this arithmetic::

           expiry = getattr(parent, "expiry", None)
           if expiry is not None and (expiry - as_of.date()).days <= int(self._s("roll_dte")):
               return True

    With ``parent.expiry`` NULL the branch is unreachable — which is why the 21-DTE roll never
    fired for a multi-leg. Reproduced here on the real parent row rather than imported, so
    ``ba2_common`` keeps no test-time dependency on ``ba2_experts``.
    """
    parent = _iron_condor(account)
    as_of = datetime(2026, 8, 5, 15, 30, tzinfo=timezone.utc)   # 16 days to AUG
    roll_dte = 21

    expiry = getattr(parent, "expiry", None)
    assert expiry is not None, "the roll branch is unreachable while this is None"
    assert (expiry - as_of.date()).days == 16
    assert (expiry - as_of.date()).days <= roll_dte, "the 21-DTE roll must now fire"


def test_the_roll_does_not_fire_outside_the_window(store, account):
    """A stamped expiry must not turn the gene into "always roll"."""
    parent = _iron_condor(account)
    as_of = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)   # 51 days to AUG

    assert (parent.expiry - as_of.date()).days == 51
    assert not (parent.expiry - as_of.date()).days <= 21
