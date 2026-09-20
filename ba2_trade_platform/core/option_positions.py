"""The executed OPENING legs of a live option transaction (spec 2026-09-20, review R1/R2).

A transaction's order history is not a position. It contains the entry fills, any adds, the
closing fills and whatever was cancelled, and the UI was feeding ALL of it to two consumers
that needed only the entry structure:

* the payoff builder, which then drew the net cash of closed fills as if it were the
  structure's expiration outcomes (a spread's -600/+400 curve flattened to +370 everywhere);
* the leg count, which counted exits as extra legs.

So the opening set is derived here ONCE, by the rules the review asked for, and both
consumers read it:

1. **A leg needs a contract.** The structure parent carries the net premium and has no
   contract symbol; it is not a leg.
2. **Only executed orders count.** A cancelled or rejected order is not a fill, and a pending
   one is not a position.
3. **Opening, not closing.** ``position_intent`` is authoritative when it names one
   (``buy_to_open`` / ``sell_to_open`` vs the ``*_to_close`` pair). When it is absent, an
   executed order for the SAME contract whose OPPOSITE side was already executed earlier is a
   close of that earlier fill -- which is what makes a long spread's entry legs and its exit
   fills distinguishable without the intent field.
4. **Filled size, not requested size.** ``filled_qty`` when it is recorded. A partially filled
   order that does not record its filled quantity is EXCLUDED rather than sized at its
   request: the position it represents is unknown.
5. **One leg per contract, weighted.** Repeated fills of the same contract on the same side
   are one leg whose entry premium is the size-weighted average and whose contracts are the
   sum. Scaling in is not two positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.types import OrderStatus

#: Order statuses that mean "this order actually traded".
_EXECUTED = frozenset(OrderStatus.get_executed_statuses())

_OPEN_INTENTS = frozenset({'buy_to_open', 'sell_to_open'})
_CLOSE_INTENTS = frozenset({'buy_to_close', 'sell_to_close'})

EXCLUDED_NOT_A_LEG = 'structure parent (no contract symbol)'
EXCLUDED_NOT_EXECUTED = 'not executed'
EXCLUDED_CLOSING = 'closing fill'
EXCLUDED_SIZE_UNKNOWN = 'partially filled without a recorded filled quantity'
EXCLUDED_NO_SIZE = 'no quantity recorded'
EXCLUDED_NO_PREMIUM = 'no fill premium recorded'


@dataclass(frozen=True)
class OpeningLeg:
    """One contract of the ENTRY structure, at its executed size and weighted premium."""

    contract_symbol: str
    side: Any
    option_type: Any
    strike: Optional[float]
    expiry: Any
    underlying: Optional[str]
    multiplier: Optional[float]
    multiplier_recorded: bool
    contracts: float
    entry_premium: float
    fills: int
    size_source: str
    order_ids: Tuple[int, ...] = ()
    #: The first executed OPENING order of this leg. The pricing seam takes an ORDER (it
    #: resolves the transaction from it), so the representative must be this, never the
    #: normalised dataclass.
    order: Any = None

    def to_chart_row(self) -> Dict[str, Any]:
        """The shape ``option_payoff_chart.chart_leg_from_row`` consumes."""
        expiry = self.expiry
        return {
            'side': getattr(self.side, 'value', self.side),
            'option_type': getattr(self.option_type, 'value', self.option_type),
            'strike': self.strike,
            'entry_price': self.entry_premium,
            'size': self.contracts,
            'multiplier': self.multiplier,
            'multiplier_recorded': self.multiplier_recorded,
            'expiry': expiry.isoformat() if hasattr(expiry, 'isoformat') else expiry,
            'underlying_symbol': self.underlying,
        }


@dataclass(frozen=True)
class OpeningLegSet:
    """The entry structure of one transaction, plus what was left out and why."""

    legs: Tuple[OpeningLeg, ...] = ()
    parent: Any = None
    excluded: Tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.legs)

    @property
    def is_multi_leg(self) -> bool:
        """More than one contract: the structure must be priced as a STRUCTURE.

        This is the R1 rule. A spread has legs, so choosing a leg as the pricing
        representative sends it through the single-contract path, which marks that one leg
        against the parent's NET premium and quantity -- the +730 the review measured where
        the executable mark was +370.
        """
        return len(self.legs) > 1

    def representative_order(self) -> Any:
        """The ORDER to hand the pricing seam: the parent when recorded, else the first leg's."""
        if self.parent is not None:
            return self.parent
        return self.legs[0].order if self.legs else None

    def chart_rows(self) -> List[Dict[str, Any]]:
        return [leg.to_chart_row() for leg in self.legs]


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = getattr(value, 'value', value)
    return str(text).strip().lower() or None


def _size_of(order: Any) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """(contracts, size_source, exclusion_reason) for one executed order."""
    filled = getattr(order, 'filled_qty', None)
    requested = getattr(order, 'quantity', None)
    status = getattr(order, 'status', None)

    if filled is not None:
        try:
            size = abs(float(filled))
        except (TypeError, ValueError):
            return None, None, EXCLUDED_NO_SIZE
        if size <= 0:
            # An executed order with no filled size contributes no position.
            return None, None, EXCLUDED_NO_SIZE
        return size, 'filled_qty', None

    if status == OrderStatus.PARTIALLY_FILLED:
        return None, None, EXCLUDED_SIZE_UNKNOWN

    if requested is None:
        return None, None, EXCLUDED_NO_SIZE
    try:
        size = abs(float(requested))
    except (TypeError, ValueError):
        return None, None, EXCLUDED_NO_SIZE
    if size <= 0:
        return None, None, EXCLUDED_NO_SIZE
    return size, 'quantity', None


def _fill_premium(order: Any) -> Optional[float]:
    for attribute in ('filled_avg_price', 'open_price'):
        value = getattr(order, attribute, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def opening_legs(transaction: Any, orders: Any) -> OpeningLegSet:
    """Normalise a transaction's orders into its executed entry structure.

    ``transaction`` supplies the fallback multiplier and the underlying; it may be None when
    the caller only needs the leg set.
    """
    ordered = list(orders or ())
    excluded: List[str] = []
    parent = None

    # Earliest executed side per contract, which is what the no-intent close detection needs.
    first_side_by_contract: Dict[str, Any] = {}
    collected: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for order in ordered:
        contract = getattr(order, 'contract_symbol', None)
        if not contract:
            if parent is None and getattr(order, 'asset_class', None) is not None:
                parent = order
            excluded.append(EXCLUDED_NOT_A_LEG)
            continue

        if getattr(order, 'status', None) not in _EXECUTED:
            excluded.append(EXCLUDED_NOT_EXECUTED)
            continue

        side = _text(getattr(order, 'side', None))
        intent = _text(getattr(order, 'position_intent', None))
        if intent in _CLOSE_INTENTS:
            excluded.append(EXCLUDED_CLOSING)
            continue
        if intent is None:
            # No intent recorded: a fill that reverses an earlier fill of the same contract
            # closes it. The earlier side is remembered per contract, first-seen first.
            previous = first_side_by_contract.get(contract)
            if previous is not None and previous != side:
                excluded.append(EXCLUDED_CLOSING)
                continue
        if contract not in first_side_by_contract:
            first_side_by_contract[contract] = side

        size, size_source, size_problem = _size_of(order)
        if size is None:
            excluded.append(size_problem or EXCLUDED_NO_SIZE)
            continue
        premium = _fill_premium(order)
        if premium is None:
            excluded.append(EXCLUDED_NO_PREMIUM)
            continue

        key = (contract, str(side))
        entry = collected.get(key)
        if entry is None:
            collected[key] = {
                'contract_symbol': contract,
                'side': getattr(order, 'side', None),
                'option_type': getattr(order, 'option_type', None),
                'strike': getattr(order, 'strike', None),
                'expiry': getattr(order, 'expiry', None),
                'underlying': getattr(order, 'underlying_symbol', None)
                              or getattr(transaction, 'symbol', None),
                'multiplier': getattr(order, 'multiplier', None),
                'multiplier_recorded': getattr(order, 'multiplier', None) is not None,
                'contracts': 0.0,
                'premium_notional': 0.0,
                'fills': 0,
                'size_sources': set(),
                'order_ids': [],
                'order': order,
            }
            entry = collected[key]

        entry['contracts'] += size
        entry['premium_notional'] += size * premium
        entry['fills'] += 1
        entry['size_sources'].add(size_source)
        if getattr(order, 'id', None) is not None:
            entry['order_ids'].append(order.id)

    legs: List[OpeningLeg] = []
    for entry in collected.values():
        contracts = entry['contracts']
        multiplier = entry['multiplier']
        if multiplier is None and transaction is not None:
            # The structure's multiplier is the recorded contract term when a leg lacks it.
            multiplier = getattr(transaction, 'multiplier', None)
        legs.append(OpeningLeg(
            contract_symbol=entry['contract_symbol'],
            side=entry['side'],
            option_type=entry['option_type'],
            strike=entry['strike'],
            expiry=entry['expiry'],
            underlying=entry['underlying'],
            multiplier=multiplier,
            multiplier_recorded=multiplier is not None,
            contracts=contracts,
            entry_premium=(entry['premium_notional'] / contracts) if contracts else 0.0,
            fills=entry['fills'],
            size_source='filled_qty' if entry['size_sources'] == {'filled_qty'} else 'quantity',
            order_ids=tuple(entry['order_ids']),
            order=entry['order'],
        ))

    legs.sort(key=lambda leg: (leg.strike is None, leg.strike, leg.contract_symbol))
    return OpeningLegSet(legs=tuple(legs), parent=parent, excluded=tuple(excluded))
