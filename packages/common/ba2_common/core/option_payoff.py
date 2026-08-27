"""Payoff at expiry for an arbitrary option/stock leg set. Pure: no DB, no network, no broker.

WHY THIS EXISTS RATHER THAN A PER-STRUCTURE MAX-LOSS TABLE. The platform already has one
per-structure risk table — ``OptionsAccountInterface.option_reserve_required`` — and it is BROKER
MARGIN, which is not maximum loss and diverges from it in both directions. A cash-secured put
reserves ``strike * 100`` but can only lose ``(strike - credit) * 100``. A jade lizard reserves
the put strike PLUS the call wing, though its loss is bounded by the put side alone. Both remain
correct as margin; neither is max loss.

A second hand-maintained table would be a second thing to keep correct against sixteen builders,
and it drifts easily: the intuitive max loss for a covered call is "basis minus strike minus
credit", which is WRONG — the strike caps the upside, not the downside, and the real answer is
"basis minus credit" (the stock going to zero). Derived from the legs, it cannot be got wrong
structure-by-structure.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ba2_common.core.types import OrderDirection

#: Leg kinds that carry a strike. ``stock`` is the third valid kind and carries none.
_OPTION_KINDS = ("call", "put")
_ALL_KINDS = ("call", "put", "stock")


@dataclass(frozen=True)
class PayoffLeg:
    """One leg of a structure, as the payoff evaluator sees it.

    ``premium`` is ALWAYS POSITIVE — what was paid for a BUY, what was received for a SELL. The
    direction lives in ``side`` alone, so a caller cannot express "a short leg with a negative
    credit" and silently get a sign flip. Every sign in this module derives from ``side``.

    A STOCK LEG uses ``kind="stock"``, ``strike=None``, ``premium=`` the per-share entry price,
    and the DEFAULT ``multiplier=100.0`` — i.e. one stock leg is the 100 shares that back one
    contract. That keeps a covered call's two legs on the same scale with no arithmetic at the
    call site.

    ``ratio`` is legs per ONE structure unit: a 1x2 put ratio spread has a short leg with
    ``ratio=2``.
    """

    kind: str                        # "call" | "put" | "stock"
    side: OrderDirection             # BUY = long, SELL = short
    premium: float                   # per share, always positive
    strike: Optional[float] = None   # required for call/put, None for stock
    ratio: int = 1
    multiplier: float = 100.0


def validate_legs(legs: Sequence[PayoffLeg]) -> Optional[str]:
    """``None`` when every leg can be priced; otherwise a human-readable reason it cannot.

    RETURNED, NOT RAISED. The caller turns this into a recorded refusal on ONE structure. A
    raise here would escape into the middle of a bar's evaluation and take every other
    structure's decision down with it — the same reasoning as
    ``option_economics.collateral_per_contract``.
    """
    if not legs:
        return "structure has no legs"
    for i, leg in enumerate(legs):
        where = f"leg {i} ({leg.kind})"
        if leg.kind not in _ALL_KINDS:
            return f"{where}: unknown leg kind {leg.kind!r}, expected one of {list(_ALL_KINDS)}"
        if leg.side not in (OrderDirection.BUY, OrderDirection.SELL):
            return f"{where}: side {leg.side!r} is neither BUY nor SELL"
        if (leg.premium is None or isinstance(leg.premium, bool)
                or not math.isfinite(leg.premium) or leg.premium < 0):
            return (f"{where}: premium {leg.premium!r} is not a usable price "
                    f"(must be a finite, non-negative number; the sign lives in `side`)")
        if leg.ratio is None or isinstance(leg.ratio, bool) or leg.ratio <= 0:
            return f"{where}: ratio {leg.ratio!r} must be positive"
        if (leg.multiplier is None or not math.isfinite(leg.multiplier)
                or leg.multiplier <= 0):
            return f"{where}: multiplier {leg.multiplier!r} must be positive"
        if leg.kind in _OPTION_KINDS:
            if (leg.strike is None or isinstance(leg.strike, bool)
                    or not math.isfinite(leg.strike) or leg.strike <= 0):
                return f"{where}: strike {leg.strike!r} is not a usable strike"
    return None


def _sign(side: OrderDirection) -> float:
    """+1 for a long leg, -1 for a short one. The ONLY place direction becomes arithmetic."""
    return 1.0 if side == OrderDirection.BUY else -1.0


def payoff_at(legs: Sequence[PayoffLeg], spot: float) -> float:
    """Total P&L in DOLLARS of ONE structure unit if the underlying expires at ``spot``.

    Assumes ``validate_legs(legs) is None`` — call it first. Passing unvalidated legs will
    raise a ``TypeError`` on the bad leg rather than returning a wrong number, which is the
    intended failure mode.
    """
    total = 0.0
    for leg in legs:
        if leg.kind == "call":
            intrinsic = max(spot - leg.strike, 0.0)
        elif leg.kind == "put":
            intrinsic = max(leg.strike - spot, 0.0)
        else:  # stock
            intrinsic = spot
        s = _sign(leg.side)
        total += (s * intrinsic - s * leg.premium) * leg.ratio * leg.multiplier
    return total
