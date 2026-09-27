"""The share-quantity GRID: which quantities a symbol can actually be traded in. Pure.

ONE definition for every sizer in the platform. The portfolio allocator had this logic
to itself (``portfolio_allocation._round_shares`` / ``tradeable_unit``); the classic risk
manager, ``compute_risk_based_quantity`` and FactorRanker each floored to a whole share
with their own ``int(...)``. Extracting it means the four cannot disagree about what
"2.5 shares of AAPL" rounds to, and a fractional grid added to one reaches all of them.

WHOLE SHARES UNLESS BOTH SAY YES
--------------------------------
A symbol trades in fractions only when the CALLER allows it (the allocator's account
config, or an expert's ``allow_fractional_shares`` setting) AND the broker says the
symbol is fractionable. Tri-state, and only ``True`` counts: ``None`` -- "the broker did
not say" -- sizes as whole shares. That is the conservative direction (under-fill rather
than send a fraction the broker then refuses) and it is the rule the allocator has always
applied; see ``MarginInfo.fractionable`` in ``account_types``.

ALWAYS ROUNDS DOWN
------------------
Every function here floors onto the grid, so a sizer can never spend more than the
notional it was given. Sell-side rounding (which the allocator does half-up, see
``portfolio_allocation._round_delta_shares``) is a policy of that caller, not of the grid.

THE DEFAULT GRID IS SAFE ON EVERY BROKER
----------------------------------------
With no published ``min_trade_increment``, the fractional step is
``10 ** -DEFAULT_FRACTIONAL_DECIMALS`` = 0.0001. That is a whole multiple of every
increment a supported broker uses (Alpaca accepts 9 decimals, TastyTrade 5), so a
quantity on the 4-decimal grid is always also on the broker's own -- which is what lets a
sizer that has only the fractionable FLAG (not the increment) use it without asking.
"""
from __future__ import annotations

import math
from typing import Optional

#: Decimal places used for a fractional quantity when the broker publishes no
#: ``min_trade_increment``. See the module docstring for why 4 is safe everywhere.
DEFAULT_FRACTIONAL_DECIMALS = 4

#: SHARE quantities closer to zero than this are exactly zero (float noise guard).
#: Shares only -- money has its own tolerance, because the two are different units and
#: tightening one must never silently move the other.
QUANTITY_EPSILON = 1e-9

#: The whole-share grid step.
WHOLE_SHARE = 1.0

#: The fractional grid step when the broker published none.
DEFAULT_FRACTIONAL_UNIT = 10.0 ** -DEFAULT_FRACTIONAL_DECIMALS


def fractional_unit(fractionable: Optional[bool], *, allow_fractional: bool,
                    min_trade_increment: Optional[float] = None) -> float:
    """The grid STEP for a symbol, from the bare facts. ``1.0`` means whole shares.

    For callers that know only the fractionable FLAG -- the risk manager and FactorRanker
    ask the account ``is_fractionable`` rather than fetching a full ``MarginInfo``. Only
    ``fractionable is True`` selects the fractional grid; ``False`` and ``None`` both size
    as whole shares.

    ``min_trade_increment`` is honoured when positive, and otherwise the safe 4-decimal
    default applies. It is ignored entirely on the whole-share grid: an increment of 0.01
    on a symbol the broker will not fractionalise does not make 0.01 shares tradeable.
    """
    if not allow_fractional or fractionable is not True:
        return WHOLE_SHARE
    if min_trade_increment and min_trade_increment > 0:
        return float(min_trade_increment)
    return DEFAULT_FRACTIONAL_UNIT


def tradeable_unit(margin, *, allow_fractional: bool) -> float:
    """The SMALLEST quantity this symbol can trade -- one step of the grid.

    1.0 on the whole-share grid; the broker's published ``min_trade_increment`` on the
    fractional one, or ``DEFAULT_FRACTIONAL_UNIT`` when fractional trading is allowed but
    no step was published (``fractionable=True, min_trade_increment=None`` is a legal
    pair).

    ``margin`` is a ``MarginInfo`` or ``None``. Untyped here only to keep this module free
    of an import on ``account_types``; any object with ``fractionable`` and
    ``min_trade_increment`` attributes will do.

    This is the QUANTITY grid only. It says nothing about whether an order of that size
    is ACCEPTABLE: ``min_order_size`` (shares) and ``min_fractional_notional`` (dollars)
    are separate thresholds, weighed in ``portfolio_allocation.size_sub_unit_target``.
    """
    if margin is None:
        return WHOLE_SHARE
    return fractional_unit(getattr(margin, 'fractionable', None),
                           allow_fractional=allow_fractional,
                           min_trade_increment=getattr(margin, 'min_trade_increment', None))


def floor_to_unit(raw: Optional[float], unit: float) -> float:
    """Round a POSITIVE share count DOWN onto the grid of step ``unit``. Never negative.

    THE rounding. Every sizer's final quantity passes through here, so a target and a
    delta -- or the allocator and the risk manager -- can never land on two different
    grids.

    The fractional branch divides by the step and floors with a tolerance of 1e-9 steps,
    because ``3 * 0.0001 / 0.0001`` is not exactly 3.0 in binary and a bare ``floor``
    would then drop a whole step: 0.0003 shares becoming 0.0002. The result is rounded to
    10 decimals to strip the multiplication's own noise, so what reaches the broker is the
    decimal the grid names.

    Returns a float on both grids. A caller that needs an ``int`` on the whole-share grid
    (a legacy return type) converts explicitly; doing it here would make the type depend
    on the grid, which is the kind of thing that breaks a JSON column silently.
    """
    if raw is None or raw <= 0:
        return 0.0
    if unit is None or unit >= WHOLE_SHARE:
        qty = float(math.floor(raw))
    else:
        qty = round(math.floor(round(raw / unit, 9)) * unit, 10)
    return qty if qty > 0 else 0.0


def round_shares(raw: Optional[float], margin, *, allow_fractional: bool) -> float:
    """Round a POSITIVE share count DOWN onto this symbol's grid (``MarginInfo`` form)."""
    return floor_to_unit(raw, tradeable_unit(margin, allow_fractional=allow_fractional))


def is_fractional_quantity(quantity: float) -> bool:
    """True when this share count is NOT a whole number of shares.

    Tested against ``QUANTITY_EPSILON`` from BOTH sides, because a quantity that came off
    a fractional grid can land at 2.9999999999 as easily as at 3.0000000001, and calling
    either of those "fractional" would apply a fractional-only broker rule to what is
    really a 3-share order.
    """
    part = abs(float(quantity)) % 1.0
    return min(part, 1.0 - part) > QUANTITY_EPSILON


def is_whole_grid(unit: float) -> bool:
    """True when ``unit`` is the whole-share step -- the legacy int-returning path."""
    return unit is None or unit >= WHOLE_SHARE
