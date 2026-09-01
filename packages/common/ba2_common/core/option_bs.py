"""option_bs — Black-Scholes MARK FALLBACK, not a new pricer.

WHY THIS FILE: the backtest's daily options cache is sparse — a held contract routinely
lacks a bar for the current simulated tick. The mark-fallback chain (see
``testplatform...backtest_account.py``'s per-lot marking / margin-liquidation buyback)
needs a stage between "real bar" and "intrinsic/entry": price the contract off its own
LAST KNOWN implied vol via Black-Scholes, so a temporarily-missing bar doesn't collapse
straight to the cruder, less informative intrinsic/entry floor.

NOT a second Black-Scholes implementation. ``ba2_common.core.finance_calc.derivatives
.black_scholes`` is THE pinned shared BSM pricer — the platform's computed Greeks come
from it and nothing else (``test_option_cache_viewer.py::
test_computed_greeks_are_the_shared_black_scholes_not_a_second_one`` guards this). This
module is a thin, fallback-grade WRAPPER over it: input gating (every input present,
finite, and positive — else ``None``, so the caller falls through to the next mark
stage) plus one unit conversion (DTE in days -> years), and nothing else. It never reads
a bar, a lot, an order, or the account — it is pure arithmetic over the numbers its
caller already resolved, and it never appears in any reserve/margin/max-loss call graph
(see ``bs_price``'s own docstring for how that separation is kept, and
``testplatform/backend/tests/backtest/test_bs_mark_fallback.py``'s structural pin for
where it is proved).

CLOSED FORM (European, continuous-compounding — the same one ``black_scholes``
implements, dividend yield 0 here since option marks are not dividend-adjusted
elsewhere in this codebase)::

    d1 = (ln(S/K) + (r + iv^2/2) * T) / (iv * sqrt(T))
    d2 = d1 - iv * sqrt(T)
    call = S * N(d1) - K * exp(-r*T) * N(d2)
    put  = K * exp(-r*T) * N(-d2) - S * N(-d1)

DTE=0 CONVENTION: ``dte_days`` must be STRICTLY POSITIVE. At ``dte_days == 0`` the
option expires on the pricing bar itself, where time value is definitionally zero and
the BSM closed form is undefined at T=0 (division by zero in d1/d2 — ``sqrt(T)`` in the
denominator). There is nothing for a volatility model to add over pricing at INTRINSIC
there, which is exactly what the mark chain's next fallback stage already does — and
exactly what the platform's own expiry-settlement path uses (``settle_option_expiry``
et al. resolve ``close_premium`` from intrinsic, never from BS). So
``bs_price(dte_days=0, ...)`` returns ``None`` BY DESIGN, not by omission: the caller
falls through to intrinsic/entry, unchanged from today.
"""
from __future__ import annotations

import math
from typing import Optional

from ba2_common.core.finance_calc.derivatives import black_scholes
from ba2_common.core.types import OptionRight


def bs_price(spot: float, strike: float, dte_days: float, iv: float,
             right: "OptionRight", r: float = 0.0) -> Optional[float]:
    """Black-Scholes MARK-FALLBACK price for one contract, per share — or ``None``.

    Requires ``spot``/``strike``/``dte_days``/``iv`` to each be present, finite, and
    STRICTLY POSITIVE (``dte_days > 0`` — see the module docstring's DTE=0 convention)
    and ``right`` to be a real ``OptionRight``. Any other input — ``None``, NaN, inf,
    zero, negative, an unresolvable enum — returns ``None`` rather than raising: this is
    a FALLBACK stage, so a bad input here must fall through to the mark chain's next
    stage (intrinsic/entry), never abort the mark or crash the run.

    ``r`` defaults to 0.0. The mark fallback's job is to recover a defensible TIME VALUE
    from the contract's own last-known IV, not to model a rates curve; a non-finite or
    absent ``r`` is treated as 0.0 rather than invalidating the whole price (unlike
    spot/strike/dte/iv, ``r`` is not one of the "requires" inputs the caller must supply).

    Delegates the actual pricing to the ONE shared BSM implementation
    (``ba2_common.core.finance_calc.derivatives.black_scholes``) so there is exactly one
    Black-Scholes formula in the codebase; this function is gating + unit conversion
    only — see the module docstring.
    """
    if right not in (OptionRight.CALL, OptionRight.PUT):
        return None
    checked = []
    for value in (spot, strike, dte_days, iv):
        if value is None:
            return None
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(fvalue) or fvalue <= 0:
            return None
        checked.append(fvalue)
    spot, strike, dte_days, iv = checked
    try:
        rate = float(r)
        if not math.isfinite(rate):
            rate = 0.0
    except (TypeError, ValueError):
        rate = 0.0
    years = dte_days / 365.0
    option_type = "call" if right == OptionRight.CALL else "put"
    try:
        result = black_scholes(spot, strike, years, rate, iv, option_type=option_type)
    except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError):
        return None
    price = result.get("price")
    if price is None or not math.isfinite(price) or price < 0:
        return None
    return float(price)
