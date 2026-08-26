"""The per-contract option balance, and the two readings of it.

``option_contract_net`` is the ONE piece of arithmetic that decides whether an
option structure is still holding something, and two doors ask it:

* ``ReadOnlyAccountInterface.refresh_transactions`` — both readings: the strict one
  for a multi-leg structure's ``position_balanced``, the plain one for the closing
  arm (OPT-S8);
* ``AlpacaAccount._apply_option_activity``, via
  :func:`every_option_contract_is_flat` — the LIVE settlement door (OPT-S3).

The two predicates DISAGREE on an empty net, deliberately, and that disagreement
is the whole reason there are two of them. It is pinned here because neither
engine's suite can see it: a live transaction with no executed contract row is
the case the live door must close (its activity is already marked processed and
never returns) and the refresh door must not (an option structure whose legs have
not filled yet has not even opened).
"""
from dataclasses import dataclass
from typing import Optional

from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus
from ba2_common.core.utils import (
    OPTION_CONTRACT_EPS, every_option_contract_is_flat, option_contract_net,
    option_structure_is_flat,
)

PUT = "AAPL260116P00150000"
CALL = "AAPL260116C00160000"


@dataclass
class Order:
    """The five fields the balance reads off a ``TradingOrder`` row."""
    side: OrderDirection = OrderDirection.SELL
    quantity: Optional[float] = 1.0
    filled_qty: Optional[float] = 1.0
    status: OrderStatus = OrderStatus.FILLED
    contract_symbol: Optional[str] = PUT
    asset_class: Optional[AssetClass] = AssetClass.OPTION


def test_a_short_leg_nets_negative_and_its_buy_back_flattens_it():
    assert option_contract_net([Order()]) == {PUT: -1.0}
    assert option_contract_net([Order(), Order(side=OrderDirection.BUY)]) == {PUT: 0.0}


def test_each_contract_is_netted_SEPARATELY():
    """The defect this exists to prevent: one sum over a structure's rows mixes
    STRUCTURES (the parent) with CONTRACTS (the legs) and reports the whole thing
    flat as soon as any one leg closes."""
    net = option_contract_net([
        Order(contract_symbol=PUT),
        Order(contract_symbol=CALL),
        Order(contract_symbol=PUT, side=OrderDirection.BUY),
    ])
    assert net == {PUT: 0.0, CALL: -1.0}
    assert not every_option_contract_is_flat(net)


def test_the_net_only_PARENT_of_a_structure_is_not_a_contract_position():
    """A multi-leg parent is an OPTION row with NO ``contract_symbol``; it counts
    structures, not contracts, and counting it would make the structure never flat."""
    net = option_contract_net([
        Order(contract_symbol=None),                       # the MLEG parent
        Order(contract_symbol=PUT),
        Order(contract_symbol=PUT, side=OrderDirection.BUY),
    ])
    assert net == {PUT: 0.0}
    assert every_option_contract_is_flat(net)


def test_equity_rows_are_not_option_contracts():
    net = option_contract_net([Order(asset_class=AssetClass.EQUITY,
                                     contract_symbol=None, quantity=100.0,
                                     filled_qty=100.0)])
    assert net == {}


def test_an_unexecuted_order_contributes_nothing():
    """PENDING/NEW rows have not traded. Only executed statuses — or a real partial
    fill — put contracts on the books."""
    assert option_contract_net([Order(status=OrderStatus.PENDING, filled_qty=None)]) == {}
    assert option_contract_net([Order(status=OrderStatus.CANCELED, filled_qty=None)]) == {}


def test_a_CANCELED_order_still_counts_the_part_that_really_traded():
    """A cancel-and-replace can race a live fill: the row ends CANCELED with a
    partial ``filled_qty``, and those contracts really changed hands."""
    assert option_contract_net([
        Order(status=OrderStatus.CANCELED, quantity=3.0, filled_qty=2.0)
    ]) == {PUT: -2.0}


def test_an_executed_order_with_no_filled_qty_falls_back_to_its_quantity():
    assert option_contract_net([
        Order(status=OrderStatus.FILLED, quantity=4.0, filled_qty=None)
    ]) == {PUT: -4.0}


def test_a_PARTIALLY_FILLED_leg_nets_only_what_filled():
    assert option_contract_net([
        Order(status=OrderStatus.PARTIALLY_FILLED, quantity=5.0, filled_qty=2.0)
    ]) == {PUT: -2.0}


# ---------------------------------------------------------------------------
# THE TWO READINGS, and the one input they answer differently
# ---------------------------------------------------------------------------
def test_the_two_predicates_disagree_on_an_EMPTY_net__and_that_is_the_point():
    """THE DISCRIMINATOR. Every other input makes them agree, so only this one can
    tell them apart — and collapsing them would break one door or the other:

    * ``every_option_contract_is_flat({})`` is True: the LIVE settlement door has no
      evidence of a surviving sibling, so it closes, which is the pre-existing
      behaviour. Holding the transaction open on a ledger that records nothing
      strands it forever — the broker activity is already marked processed.
    * ``option_structure_is_flat({})`` is False: ``refresh_transactions`` turns
      "balanced" into "CLOSE this transaction" and runs over every non-terminal
      transaction every pass. An empty net there means "nothing has executed", and
      reading that as balanced would close structures that have not even opened.
    """
    assert every_option_contract_is_flat({}) is True
    assert option_structure_is_flat({}) is False
    unfilled = option_contract_net([Order(status=OrderStatus.PENDING, filled_qty=None)])
    assert unfilled == {}
    assert option_structure_is_flat(unfilled) is False, (
        "an option structure whose legs have not filled is not a flat structure")


def test_on_a_NON_empty_net_the_two_readings_are_the_same():
    """The other half of the pin: they may differ ONLY on empty."""
    flat = option_contract_net([Order(), Order(side=OrderDirection.BUY)])
    live = option_contract_net([Order()])
    assert option_structure_is_flat(flat) is True
    assert every_option_contract_is_flat(flat) is True
    assert option_structure_is_flat(live) is False
    assert every_option_contract_is_flat(live) is False


def test_flat_is_a_TOLERANCE_not_an_exact_zero():
    """Contract counts are whole numbers summed as floats, so the residue of a
    fractional-looking net must not read as an open position."""
    dust = OPTION_CONTRACT_EPS / 10.0
    assert every_option_contract_is_flat({PUT: dust}) is True
    assert every_option_contract_is_flat({PUT: OPTION_CONTRACT_EPS * 10.0}) is False


def test_no_orders_at_all_is_handled_without_raising():
    assert option_contract_net([]) == {}
    assert option_contract_net(None) == {}
