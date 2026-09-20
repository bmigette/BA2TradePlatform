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

logger = logging.getLogger(__name__)

#: Why a P&L could not be produced, in words the UI can show.
UNAVAILABLE_NOT_AN_OPTION = "not an option transaction"
UNAVAILABLE_NO_QUOTE = "no current option quote or contract multiplier"
UNAVAILABLE_NO_FILLS = "no executed option legs recorded"
UNAVAILABLE_FLAT = "structure already flat"
UNAVAILABLE_NO_MULTIPLIER = "contract multiplier not recorded"
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
