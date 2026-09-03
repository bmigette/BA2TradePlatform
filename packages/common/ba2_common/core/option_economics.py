"""Premium richness: annualised return on collateral, PER CONTRACT (OPT-C1).

Credit structures are admitted on ``net_credit > 0`` alone (``TradeActions.py``). There is no
minimum credit, no credit-as-a-fraction-of-width, no return floor. Selling far-OTM options for
near-zero credit expires worthless roughly 97 % of the time, so on any win-rate- or
Sharpe-flavoured fitness the search is ACTIVELY REWARDED for doing exactly that. The
profitability criterion is not merely unpenalised; it does not exist.

The criterion
-------------
::

    arc = (premium_received_per_contract / collateral_per_contract) * (365 / days_to_expiry)

**Per contract, and that is what makes it usable.** At the BOOK level
``contracts x max_loss`` IS ``option_sizing`` % of equity by construction, so a book-level
return-on-collateral divides by a near-constant and degenerates into plain return. Per contract
the objection vanishes: premium and collateral both scale with contract count, so the ratio is
INVARIANT to sizing. Three contracts is three times both.

Collateral is not re-derived here. It comes from
``OptionsAccountInterface.option_reserve_required`` -- the one place that knows a
cash-secured put reserves ``strike x 100``, a credit vertical ``(width - credit) x 100``, and
the naked shapes their empirically-verified Reg-T / MLEG amounts. A second implementation would
be a second thing to keep correct.

Unknown is never a number
-------------------------
An unmeasurable ARC must not read as 0 % (refuse everything, which looks like a working gate)
nor as infinite (admit everything, which looks like a working gate too). Every unmeasurable
input yields ``None``, and ``admits_credit_structure`` turns ``None`` into a REFUSAL whenever a
floor is configured. In particular:

* ``days_to_expiry <= 0`` is not a division opportunity. A same-day expiry annualised by
  ``365/0`` is infinite, and infinity admits any credit at all -- including a 1-cent one.
* a ZERO collateral is not an infinite return. Long/debit structures reserve nothing by
  definition, so the ratio is undefined for them and this gate simply does not apply
  (``applies_to``).
* an unreadable contract multiplier is unmeasurable, and so is a multiplier that disagrees
  with the reserve table's own 100. Silently mixing a 100-share reserve with a 10-share
  contract's premium would misstate the return by an order of magnitude on exactly the
  adjusted contracts nobody checks.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from ba2_common.logger import logger

#: Calendar days per year for the annualisation. Calendar, not trading, days: the collateral
#: is tied up over the calendar life of the structure, including weekends.
DAYS_PER_YEAR = 365.0

#: Contract size ``option_reserve_required`` bakes into every branch (``* 100.0``). A contract
#: whose real multiplier differs is NOT priced by that helper, so its ARC is unmeasurable
#: rather than approximately right. See the module docstring.
RESERVE_TABLE_MULTIPLIER = 100.0

#: Stable phrase carried by every ARC refusal, so a caller (and a test) can tell this one apart
#: from the buying-power, assignment-capacity and share-cover refusals that decline the same
#: entry path with three different remedies. Mirrors ``ASSIGNMENT_CAPACITY_REFUSAL``.
ARC_FLOOR_REFUSAL = "return on collateral below the configured floor"


#: RESERVING strategies whose BUILDER deliberately never consults the ARC floor. Membership
#: here is not "the gate does not apply" -- ``applies_to`` below still answers True for these,
#: because they do post collateral -- it is "no builder asks the question, on purpose", and it
#: exists so the two drift guards that walk ``RESERVING_STRATEGIES`` looking for an ARC-gated
#: builder (``packages/common/tests/test_option_arc_gate_enforced.py`` and
#: ``testplatform/backend/tests/test_option_min_arc_gene.py``) name ONE exemption list instead
#: of two that can disagree. Two different reasons, both spelled out:
#:
#:  * the three PRICING ALIASES have no action class at all -- ``option_reserve_required``
#:    prices them for callers that name a structure generically, and nothing builds one;
#:  * the two 1x2 BACKSPREADS (design 2026-08-31 SS2) are the convexity-financed family,
#:    whose whole thesis is that the short leg finances the longs. Their net is near zero BY
#:    DESIGN and may be a DEBIT, so a minimum annualised-return-on-collateral floor would
#:    refuse exactly the structures the design is built to search -- and at a net debit the
#:    ratio is not merely thin but undefined (``annualized_return_on_collateral`` returns
#:    None, which a configured floor turns into a refusal). The premium-richness question is
#:    the right one for a structure OPENED for its credit; a backspread is opened for its
#:    convexity and its risk is gated by the measured max loss instead.
ARC_FLOOR_EXEMPT_STRATEGIES = frozenset({
    "credit_spread", "naked_put", "debit_spread",
    "call_backspread", "put_backspread",
})


def applies_to(strategy: str) -> bool:
    """True when ``strategy`` posts collateral, i.e. when an ARC floor is meaningful.

    Long/debit structures (``ZERO_RESERVE_STRATEGIES``) reserve nothing -- their maximum loss
    is the premium already paid -- so "return on collateral" has no denominator and the gate
    does not apply to them. An UNRECOGNISED strategy is not silently exempted: it answers
    False here, but ``annualized_return_on_collateral`` still returns None for it, so a
    configured floor refuses it rather than waving it through.
    """
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    return strategy in OptionsAccountInterface.RESERVING_STRATEGIES


def _readable_multiplier(multiplier: Any) -> Optional[float]:
    """``float(multiplier)`` when it can be a real contract size, else None.

    Never defaults to 100. The option-seams work established the discipline: read the
    multiplier per contract, and treat an unreadable one as unmeasurable. ``bool`` is excluded
    because it is an ``int`` subclass and ``True`` would become a 1-share contract; strings
    because ``float("100")`` succeeds and a stringly-typed field is a bug to surface.
    """
    if multiplier is None or isinstance(multiplier, (bool, str, bytes)):
        return None
    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def collateral_per_contract(strategy: str, **reserve_kwargs) -> Optional[float]:
    """The capital ONE contract of ``strategy`` must set aside, or None if unpriceable.

    A thin wrapper over ``OptionsAccountInterface.option_reserve_required(strategy, 1, ...)``
    that converts its (deliberate, loud) ``ValueError`` for an unknown strategy or a missing
    sizing input into the None this module's callers understand. The raise is right for a
    buying-power check -- it must never under-reserve -- but here it would escape into the
    middle of a rule evaluation and take every other gate on the bar down with it.

    A computed ZERO is returned as None, not as 0.0: a structure with no collateral has no
    return ON collateral, and 0.0 would make the caller divide by zero.
    """
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    try:
        reserve = OptionsAccountInterface.option_reserve_required(strategy, 1, **reserve_kwargs)
    except ValueError as e:
        logger.warning(
            f"Cannot price collateral for {strategy!r}, so its return on collateral is "
            f"UNKNOWN (not zero, not infinite): {e}")
        return None
    if reserve is None or not math.isfinite(reserve) or reserve <= 0:
        return None
    return float(reserve)


def annualized_return_on_collateral(
    *,
    strategy: str,
    net_credit: Optional[float],
    days_to_expiry: Optional[int],
    multiplier: Any = RESERVE_TABLE_MULTIPLIER,
    **reserve_kwargs,
) -> Optional[float]:
    """Annualised return on collateral for ONE contract, as a FRACTION (0.25 == 25 %/yr).

    ``net_credit`` is the per-share net premium RECEIVED (the sign convention every credit
    builder and ``option_reserve_required`` already use: positive == credit).
    ``reserve_kwargs`` are forwarded verbatim to ``option_reserve_required``
    (``strike`` / ``spread_width`` / ``net_credit`` / ``spot`` / ``option_type``) -- pass
    whatever that strategy's branch prices with.

    Returns None -- UNMEASURABLE, which the gate reads as "refuse" -- for every one of:
    an absent or negative credit (a negative credit is a DEBIT, i.e. not this structure at
    all), an absent or non-positive DTE, an unreadable multiplier, a multiplier that the
    reserve table does not price, an unknown or unpriceable strategy, and a zero collateral.

    A net credit of exactly ZERO is measurable and returns 0.0: the structure really does
    earn nothing, and any positive floor must reject it on its merits rather than because the
    number went missing.
    """
    if net_credit is None:
        logger.warning(f"{strategy}: no net credit, so return on collateral is unknown")
        return None
    try:
        credit = float(net_credit)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(credit) or credit < 0:
        logger.warning(
            f"{strategy}: net credit {net_credit!r} is not a credit; return on collateral "
            f"is unknown")
        return None

    if days_to_expiry is None:
        logger.warning(f"{strategy}: no days-to-expiry, so return on collateral is unknown")
        return None
    try:
        dte = float(days_to_expiry)
    except (TypeError, ValueError):
        return None
    # NOT a division opportunity. 365/0 is infinite and infinity admits ANY credit, including
    # a one-cent one, on exactly the expiry where the structure has the least time to work.
    if not math.isfinite(dte) or dte <= 0:
        logger.warning(
            f"{strategy}: {days_to_expiry} days to expiry cannot be annualised (a same-day "
            f"expiry is not an infinite return); return on collateral is unknown")
        return None

    mult = _readable_multiplier(multiplier)
    if mult is None:
        logger.warning(
            f"{strategy}: contract multiplier {multiplier!r} is unreadable, so the premium "
            f"cannot be converted to dollars; return on collateral is unknown")
        return None
    if mult != RESERVE_TABLE_MULTIPLIER:
        # option_reserve_required prices every branch at x100. Mixing a 100-share reserve with
        # a differently-sized contract's premium misstates the return by the ratio of the two,
        # on precisely the adjusted contracts nobody inspects.
        logger.warning(
            f"{strategy}: contract multiplier {mult} is not the {RESERVE_TABLE_MULTIPLIER} "
            f"the reserve table prices, so collateral and premium are on different scales; "
            f"return on collateral is unknown")
        return None

    # The credit the caller passed IS the credit the reserve branches price with (credit
    # verticals net it off the width; jade lizard and put ratio spread net it off the
    # notional), so forward it rather than making every call site pass the same number twice
    # -- passing it once and forgetting the other is how the two would drift.
    reserve_kwargs.setdefault("net_credit", credit)
    collateral = collateral_per_contract(strategy, **reserve_kwargs)
    if collateral is None:
        return None

    premium = credit * mult
    arc = (premium / collateral) * (DAYS_PER_YEAR / dte)
    if not math.isfinite(arc):
        return None
    return arc


def admits_credit_structure(arc: Optional[float], min_arc: Optional[float]) -> bool:
    """The gate decision: may a structure with this ARC be opened?

    * ``min_arc is None`` -- no floor configured. The gate is OFF and admits everything,
      including an unmeasurable ARC; this is the current behaviour and the opt-in default.
    * ``min_arc`` configured (including an explicit 0.0) and ``arc is None`` -- REFUSE. An
      unmeasurable entry criterion is not a satisfied one, and a floor of zero is a
      configured gate, not an absent one.
    * otherwise -- ``arc >= min_arc``.
    """
    if min_arc is None:
        return True
    # str/bytes/bool are excluded for the same reason ``plausible_atm_iv`` excludes them: a
    # stringly-typed threshold is a configuration bug to SURFACE, not to parse, and ``True``
    # would quietly become a 100 %/yr floor.
    if isinstance(min_arc, (bool, str, bytes)):
        logger.warning(
            f"ARC floor {min_arc!r} is not a number; refusing the entry rather than parsing "
            f"a misconfigured gate")
        return False
    try:
        floor = float(min_arc)
    except (TypeError, ValueError):
        # A floor we cannot read is a MISCONFIGURED gate. Refusing is the safe direction:
        # admitting would silently disable a criterion the operator asked for.
        logger.warning(f"Unreadable ARC floor {min_arc!r}; refusing the entry")
        return False
    if not math.isfinite(floor):
        logger.warning(f"Non-finite ARC floor {min_arc!r}; refusing the entry")
        return False
    if arc is None:
        logger.info(
            f"Return on collateral is unmeasurable and a floor of {floor} is configured; "
            f"refusing the entry (unknown is not a pass)")
        return False
    return float(arc) >= floor
