"""The option ENTRY-QUOTE concession: how much of the modelled spread an entry gives up (F3).

THE DEFECT THIS EXISTS FOR
--------------------------
An option entry is quoted from the ANALYSIS bar (the builders take ``contract.ask`` for a buy
and ``contract.bid`` for a sell), but the backtest's default ``next_bar_open`` fill model makes
the NEXT day's open cross that quote before anything fills. In the historical option store the
chain carries NO usable quote -- every cached row has ``bid == ask`` (measured: 701,849 of
958,024 chain rows equal, the other 256,175 both NULL; the parquet store has no bid/ask column
at all) -- so ``contract.ask`` and ``contract.bid`` are BOTH just the analysis day's close, i.e.
the MID. The tradeable spread is then modelled at fill time by ``option_spread_pct`` /
``option_spread_min_tick`` (``BacktestAccount._option_half_spread`` / ``_option_cross``).

The two halves do not meet. A seller quoting the analysis mid ``c`` fills only if the next
bar's crossed bid clears it -- ``o - half >= c`` -- i.e. only if the premium RISES by the whole
modelled half-spread overnight. For decaying OTM premium it does the opposite, the DAY order
expires unfilled, and premium sellers structurally almost never fill. Head-to-head on INTC
Feb-Dec 2024: O_CSP got 6 trades under ``next_bar_open`` and 9 under ``same_bar_close``; an
earlier AAPL probe got 0 against 17.

WHY THE QUOTE AND NOT THE FILL MODEL
------------------------------------
``next_bar_open`` stays the default: it carries no look-ahead and it is what every existing
equity grid used, so numbers stay comparable. Flipping the default would retroactively move
every historical option result AND reintroduce the look-ahead the hermetic contract exists to
prevent. So the QUOTE side is what gets fixed: an entry may choose to quote away from the mid,
towards the touch it will have to trade at anyway.

WHY A FRACTION AND NOT AN ABSOLUTE OFFSET
-----------------------------------------
An absolute $0.05 means something completely different on a $0.40 put and on a $12 call,
whereas a fraction of that contract's OWN modelled spread is scale-free across symbols and
premium levels -- the same reasoning that made the selection-policy features chain-relative.

  0.0  quote at the mid. The pre-F3 behaviour, EXACTLY (see ``ENTRY_CROSS_NEUTRAL``).
  1.0  quote at the far touch -- exactly the price ``_option_cross`` models the fill at, so
       the entry gives up the whole modelled spread up front.

At 1.0 the fill test stops asking the premium to move in the entry's favour by a whole
half-spread and reduces to a straight comparison of the two bars' mids: a buy fills when
``o + half <= c + half`` (o <= c), a sell when ``o - half >= c - half`` (o >= c). It is NOT a
guaranteed fill -- nothing removes the overnight drift -- it removes only the part of the
barrier the spread model itself put there.

THE ONE SPREAD, NOT A SECOND ONE
--------------------------------
``half_spreads`` must come from the simulator's own model
(``BacktestAccount.option_modelled_half_spread``, which is ``_option_half_spread`` on the
contract's as-of bar). This module deliberately computes no spread of its own: a concession
measured against a spread the fill engine does not charge would be a second, silently divergent
cost model.

LIVE IS UNAFFECTED, AND THAT IS NOT AN OVERSIGHT. A live account has real quotes and the
builders already quote at the real touch (buy@ask / sell@bid), which IS a full concession.
There is no modelled half-spread to ask a live account for, the caller therefore applies none,
and live behaviour is byte-identical. The gene exists to let a BACKTEST reproduce the quoting
behaviour live already has, on a data source whose quote column is degenerate.
"""
from __future__ import annotations

from typing import Sequence

from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import OrderDirection

#: The concession that reproduces the pre-F3 quote EXACTLY: none. Every entry keeps quoting the
#: builder's own ``contract.ask`` / ``contract.bid`` / net, so no existing result moves. This is
#: the authored default of the ``option_entry_cross`` gene and the value the launcher stamps.
ENTRY_CROSS_NEUTRAL = 0.0

#: The full cross: give up the whole modelled spread, landing on the touch ``_option_cross``
#: already models the fill at.
ENTRY_CROSS_FULL = 1.0


def validated_entry_cross(fraction) -> float:
    """``fraction`` as a float in [0, 1], or a loud ``ValueError``.

    NOT clamped. A value outside the band is a configuration error, and silently clamping it
    would run a whole campaign at a concession nobody chose -- the same class of defect as a
    liquidity gate that is quietly ignored. The GA can only ever emit values inside the
    declared range, so a reject here means a hand-written config, which should be seen.
    """
    if isinstance(fraction, (bool, str, bytes)):
        raise ValueError(f"entry_cross {fraction!r} is not a number")
    value = float(fraction)
    if not (0.0 <= value <= 1.0):
        raise ValueError(
            f"entry_cross must be a fraction of the modelled spread in [0, 1], got {value}. "
            f"0 quotes at the mid (pre-F3 behaviour); 1 quotes at the far touch.")
    return value


def quote_concession(legs: Sequence[OptionLeg], half_spreads: Sequence[float],
                     fraction: float) -> float:
    """Premium per share, PER STRUCTURE, that a ``fraction`` concession gives up (>= 0).

    Weighted by each leg's ``ratio_qty`` because that is exactly how the parent's net is
    formed (``_fill_multi_leg_parent``: ``net = Σ ±premium x ratio``), so a 1-2-1 butterfly or
    a 1-2 put ratio concedes on its doubled leg twice.
    """
    if len(legs) != len(half_spreads):
        raise ValueError(
            f"quote_concession needs one half-spread per leg, got {len(half_spreads)} for "
            f"{len(legs)} legs")
    total = 0.0
    for leg, half in zip(legs, half_spreads):
        total += abs(float(half)) * abs(int(leg.ratio_qty or 1))
    return validated_entry_cross(fraction) * total


def entry_limit_with_concession(limit_price: float, legs: Sequence[OptionLeg],
                                half_spreads: Sequence[float], fraction) -> float:
    """The entry's limit after giving up ``fraction`` of the modelled spread.

    THE DIRECTION IS THE WHOLE POINT, and it differs by shape because the two shapes use two
    different sign conventions -- both of them the simulator's, not this module's:

    * SINGLE LEG -- ``limit_price`` is a POSITIVE premium and the side comes from the leg
      (``OptionsAccountInterface.submit_option_order`` types the order BUY_LIMIT/SELL_LIMIT off
      ``legs[0].side``). A BUYER gives up by quoting HIGHER (``+``), a SELLER by quoting LOWER
      (``-``). Backwards, this would make fills strictly rarer instead of commoner, which is
      why it is pinned by its own tests.
    * MULTI-LEG -- ``limit_price`` is the parent's NET in the ``+debit / -credit`` convention
      the whole option stack uses. Giving up means paying more debit OR taking less credit, and
      in that one convention both are the SAME direction: the net goes UP.

    FLOORS, mirroring ``_option_cross``'s own ``max(0.0, px - half)``:

    * a single-leg SELL limit never goes below zero (a modelled spread wider than the premium
      must not ask the account to pay to sell);
    * a multi-leg CREDIT never becomes a DEBIT. The clamp only engages when the whole modelled
      spread exceeds the structure's own credit -- a structure not worth opening -- and it
      keeps the parent's recorded side (derived from the sign of this number) stable. Such an
      order is left unfillable rather than mis-signed: the achieved net at fill pays that same
      spread, so it cannot come in at or below a zero net credit.
    """
    if not legs:
        return limit_price
    if validated_entry_cross(fraction) == 0.0:
        return limit_price                    # exact no-op: not even a float round trip
    give = quote_concession(legs, half_spreads, fraction)
    if give <= 0.0:
        return limit_price
    # A CONCESSION THAT WOULD WIPE OUT THE PREMIUM IS DECLINED, NOT CLAMPED.
    #
    # `max(0.0, limit - give)` looks like the safe floor -- "never ask the account to pay to
    # sell" -- and it is the opposite. A SELL_LIMIT of 0.0 ALWAYS clears (`_option_cross` floors
    # the sell at `max(0.0, px - half)`, and the arb guard does not fire on an OTM contract whose
    # intrinsic is 0), so the clamp converts an order that would honestly have expired unfilled
    # into a short option written for ZERO premium while carrying the full assignment liability.
    # Fabricated risk with no compensation. Reachable with the grid's own
    # --option-spread-min-tick 0.02, doubled on a thin contract, against anything priced at or
    # below $0.02.
    #
    # Declining leaves the order exactly where it was: unfillable, which is the honest outcome
    # when the whole premium is narrower than the spread being crossed.
    if len(legs) == 1:
        if legs[0].side == OrderDirection.SELL:
            conceded = limit_price - give
            return conceded if conceded > 0.0 else limit_price
        return limit_price + give
    if limit_price < 0.0:
        conceded = limit_price + give          # a credit is negative; concession shrinks it
        return conceded if conceded < 0.0 else limit_price
    return limit_price + give
