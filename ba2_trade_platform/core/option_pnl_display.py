"""Display P&L for a live OPTION transaction (spec 2026-09-20, decision 5).

The Live Trades page prices every row off the UNDERLYING's current price and multiplies by
the raw quantity -- correct for equity, wrong twice for an option:

  * the underlying quote is not the option premium, and
  * the dollar amount is missing the contract multiplier (a $370 spread profit reads $3.70).

The rules engine already prices options correctly (``TradeConditions._get_pnl_for_condition``
dispatches single-leg to ``_get_option_pnl_via_transaction`` -- long marks at the bid, short
at the ask, last as fallback -- and multi-leg to ``_get_spread_pnl_via_transaction``, priced
off the structure's NET premium). This module is the DISPLAY-side entry to that same seam so
the Options tab cannot grow a second, different formula.

Nothing here invents a number. A missing quote, a missing multiplier, an unexecuted leg set
or a flat structure yields ``unavailable`` with the reason, and the caller must render that
as unknown rather than as zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

logger = logging.getLogger(__name__)

#: Why a P&L could not be produced, in words the UI can show.
UNAVAILABLE_NOT_AN_OPTION = "not an option transaction"
UNAVAILABLE_NO_QUOTE = "no current option quote or contract multiplier"
UNAVAILABLE_NO_FILLS = "no executed option legs recorded"
UNAVAILABLE_FLAT = "structure already flat"
UNAVAILABLE_NO_MULTIPLIER = "contract multiplier not recorded"
UNAVAILABLE_INCOMPLETE = "executed structure incompletely recorded"
UNAVAILABLE_NO_PRICES = "entry or exit premium not recorded"


@dataclass(frozen=True)
class OptionPnlDisplay:
    """What the Options tab can honestly say about a transaction's P&L."""

    amount: Optional[float]
    percent: Optional[float]
    source: str
    reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.amount is not None


def _unavailable(reason: str) -> OptionPnlDisplay:
    return OptionPnlDisplay(amount=None, percent=None, source="unavailable", reason=reason)


class QuoteCachingAccount(OptionsAccountInterface):
    """Wrap an options account so each contract is quoted ONCE per refresh.

    The pricing seam quotes a contract itself, and the Options tab also wants the current
    premium for its Current column. Same contract, same account, same refresh: one broker call
    (second review, N5).

    It must BE an ``OptionsAccountInterface`` -- that ``isinstance`` is how the seam decides it
    can price options at all -- so the abstract set is emptied here and every other call is
    delegated by ``__getattr__``. The cache key includes the ACCOUNT: two accounts can hold the
    same contract, and one account's quote is not the other's.
    """

    #: Attributes that stay LOCAL to the wrapper; everything else is forwarded to the wrapped
    #: account. `__getattr__` alone is NOT enough: a method the ABC itself defines (abstract or
    #: concrete) is found by ordinary lookup and would run the ABC's own body against the
    #: wrapper -- returning None for `get_option_positions` and silently skipping the real
    #: account. Forwarding has to happen at lookup time.
    _LOCAL = frozenset({
        'get_option_quote', '_account', '_cache', '_account_id', '_LOCAL',
        '__class__', '__dict__', '__getattribute__', '__setattr__', '__init__',
        '__abstractmethods__', '__weakref__', '__module__', '__doc__',
    })

    def __init__(self, account: Any, cache: Dict[Any, Any], account_id: Any = None):
        self._account = account
        self._cache = cache
        self._account_id = account_id

    def __getattribute__(self, name: str) -> Any:
        if name in QuoteCachingAccount._LOCAL:
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, '_account'), name)

    def get_option_quote(self, contract_symbol: str) -> Any:
        cache = object.__getattribute__(self, '_cache')
        key = (object.__getattribute__(self, '_account_id'), contract_symbol)
        if key not in cache:
            wrapped = object.__getattribute__(self, '_account')
            cache[key] = wrapped.get_option_quote(contract_symbol)
        return cache[key]


# ABCMeta computes __abstractmethods__ while the class is being created, so this has to happen
# AFTER the body. It is honest rather than a dodge: every capability really is available,
# forwarded to the wrapped account by __getattribute__. What matters is that the wrapper still
# IS an OptionsAccountInterface -- that isinstance is how the pricing seam decides it may price
# options at all, so a duck-typed proxy would not work.
QuoteCachingAccount.__abstractmethods__ = frozenset()


def quote_caching_account(account: Any, cache: Dict[Any, Any], account_id: Any = None) -> Any:
    """``account`` wrapped for per-refresh quote reuse (already-wrapped accounts pass through)."""
    if isinstance(account, QuoteCachingAccount):
        return account
    return QuoteCachingAccount(account, cache, account_id)


def unavailable_pnl(reason: str) -> OptionPnlDisplay:
    """An explicitly UNAVAILABLE P&L, for a caller that knows why it must not price.

    Public because the Options tab refuses to price an incompletely recorded structure and has
    to say so on the row, rather than showing a blank that reads as flat.
    """
    return _unavailable(reason)


def option_transaction_pnl(
    account: Any,
    order: Any,
    *,
    opening_legs: Optional[int] = None,
    single_leg: Optional[Callable[[Any, Any], Optional[Dict[str, float]]]] = None,
    multi_leg: Optional[Callable[[Any, Any], Optional[Dict[str, float]]]] = None,
) -> OptionPnlDisplay:
    """Unrealised P&L of an OPEN option transaction, from the shared pricing seam.

    ``order`` is the pricing representative and ``opening_legs`` is how many contracts the
    ENTRY structure actually holds (see ``core.option_positions``). Pass it: the dispatch
    must follow the STRUCTURE, because dispatching on the representative's own shape is the
    review's R1 defect. A spread has contract legs, so handing over ``legs[0]`` sent the whole
    structure down the single-contract path, which marks that ONE leg against the parent's NET
    premium and quantity -- +730 displayed where the executable mark was +370.

    When ``opening_legs`` is omitted the old shape-based dispatch is kept, so a caller that
    only has one order still works; production callers pass the count.

    The two seam functions are injectable so the dispatch itself is testable without a live
    broker account; production always uses the rules engine's own functions.
    """
    if getattr(order, "asset_class", None) is None:
        return _unavailable(UNAVAILABLE_NOT_AN_OPTION)

    if single_leg is None or multi_leg is None:
        from ba2_common.core.TradeConditions import (
            _get_option_pnl_via_transaction, _get_spread_pnl_via_transaction,
        )
        single_leg = single_leg or _get_option_pnl_via_transaction
        multi_leg = multi_leg or _get_spread_pnl_via_transaction

    if opening_legs is None:
        is_structure = not getattr(order, "contract_symbol", None)
    else:
        is_structure = opening_legs > 1

    try:
        if is_structure:
            pnl = multi_leg(account, order)
            source = "structure_net_premium"
        else:
            pnl = single_leg(account, order)
            source = "single_leg_premium"
    except Exception as exc:  # a pricing failure is UNKNOWN, never zero
        logger.warning(f"Option P&L unavailable for order {getattr(order, 'id', '?')}: {exc}")
        return _unavailable(UNAVAILABLE_NO_QUOTE)

    if not pnl or pnl.get("amount") is None:
        return _unavailable(UNAVAILABLE_NO_QUOTE)

    return OptionPnlDisplay(
        amount=float(pnl["amount"]),
        percent=None if pnl.get("percent") is None else float(pnl["percent"]),
        source=source,
    )


def open_structure_pnl(
    account: Any,
    transaction: Any,
    orders: Any,
    *,
    leg_set: Any = None,
    single_leg: Optional[Callable[[Any, Any], Optional[Dict[str, float]]]] = None,
    multi_leg: Optional[Callable[[Any, Any], Optional[Dict[str, float]]]] = None,
) -> OptionPnlDisplay:
    """Unrealised P&L of an OPEN option transaction, from its orders: THE display rule.

    The Options tab and the Floating P/L cards both call this, so they cannot disagree. (The
    cards used to price option transactions with the equity formula -- the broker position
    keyed by the transaction's UNDERLYING symbol, which an option book never holds, so every
    option structure read "no broker price" -- and would have missed the contract
    multiplier even if it had matched.)

    Refuses an incompletely recorded structure rather than pricing what is left of it (the
    seam would see a DIFFERENT structure, second review N4), and reports a transaction whose
    entry has not executed as ``UNAVAILABLE_NO_FILLS``: it holds nothing yet.
    ``leg_set`` may be passed when the caller already built it (``opening_legs``).
    """
    if leg_set is None:
        from .option_positions import opening_legs
        leg_set = opening_legs(transaction, orders)
    if leg_set.incomplete:
        return _unavailable(f"{UNAVAILABLE_INCOMPLETE}: " + "; ".join(leg_set.incomplete_reasons))
    if leg_set.count == 0:
        return _unavailable(UNAVAILABLE_NO_FILLS)
    # The representative: for a STRUCTURE it is the parent (the seam resolves the legs
    # itself), for a single contract it is that contract's own ORDER -- the seam resolves the
    # transaction from the order it is handed. The count picks the seam (review R1).
    if leg_set.is_multi_leg:
        representative = leg_set.representative_order()
    else:
        representative = leg_set.legs[0].order
    if representative is None:
        return _unavailable(UNAVAILABLE_INCOMPLETE)
    return option_transaction_pnl(account, representative, opening_legs=leg_set.count,
                                  single_leg=single_leg, multi_leg=multi_leg)


def option_closed_pnl(transaction: Any) -> OptionPnlDisplay:
    """Realised P&L of a CLOSED option transaction, MULTIPLIER-AWARE.

    ``(close - open) * |qty| * multiplier``, signed by side. The multiplier is never
    assumed: without a recorded one the dollar amount is not derivable and the row says so
    (the equity path's ``(close - open) * qty`` understates an option by 100x, which is the
    defect this function exists to not repeat).
    """
    multiplier = getattr(transaction, "multiplier", None)
    open_price = getattr(transaction, "open_price", None)
    close_price = getattr(transaction, "close_price", None)
    quantity = getattr(transaction, "quantity", None)

    if open_price is None or close_price is None or not quantity:
        return _unavailable(UNAVAILABLE_NO_PRICES)
    if not multiplier or multiplier <= 0:
        return _unavailable(UNAVAILABLE_NO_MULTIPLIER)

    from ba2_common.core.types import OrderDirection

    sign = 1.0 if getattr(transaction, "side", None) == OrderDirection.BUY else -1.0
    amount = sign * (float(close_price) - float(open_price)) * abs(float(quantity)) * float(multiplier)

    basis = float(open_price) * abs(float(quantity)) * float(multiplier)
    percent = (amount / basis * 100.0) if basis > 0 else None

    return OptionPnlDisplay(amount=amount, percent=percent, source="recorded_fills")
