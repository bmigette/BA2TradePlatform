"""Choosing ONE contract from the candidates that fall inside a rule's box. Pure.

THE DIVISION OF LABOUR. A rule states a BOX — "a put, delta 0.10 to 0.25, one month". This module
chooses inside it. The rule owns the strategy's shape; the policy owns which contract best
expresses it today, and the policy's weights are GENES, so the GA searches the choosing rather
than inheriting somebody's guess at it.

WHY EVERY FEATURE IS NORMALISED WITHIN THE CANDIDATE SET. Option prices, spreads and volumes have
no standard range across symbols: "$2.00 of premium" is rich on a $15 stock and negligible on a
$900 one, so an absolute threshold optimised on one universe is meaningless on another. A
contract's RANK among the peers on its own chain is scale-free, and that is what these features
measure.

THE DEFAULT IS A PROVABLE NO-OP. With only ``w_box_center`` at its pinned 1.0, ``pick`` selects
exactly the contract ``option_selector._pick_by`` selects, tie-breaks included. That is what lets
this ship without moving a single existing backtest — proven in
``tests/test_option_selection_policy_noop.py`` (added by the next task).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

from ba2_common.core.option_selector import target_strike
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

#: The score a candidate gets on a feature it cannot answer. Features are MAXIMISED, so 0.0 is
#: the worst possible value: unknown never beats known. Same direction as
#: ``option_selector.passes_liquidity``, which fails closed on a missing liquidity field.
_WORST = 0.0

#: Calendar days per year, matching ``option_economics.DAYS_PER_YEAR``.
_DAYS_PER_YEAR = 365.0

FEATURE_NAMES = ("box_center", "premium", "iv", "rvol", "spread")


@dataclass(frozen=True)
class SelectionPolicy:
    """The weights that decide which contract in the box wins. Each non-pinned weight is a gene.

    ``w_box_center`` IS PINNED AT 1.0 AND IS NOT A GENE. Scaling all five weights by the same
    factor changes no ranking, so leaving it free would hand the GA a degenerate direction to
    wander in — budget spent exploring a difference that is not one.

    ``w_iv`` IS THE ONE SIGNED WEIGHT. Premium richness, relative volume and quote tightness have
    an unambiguous good direction. Implied volatility does not: premium SELLERS want rich vol and
    BUYERS want cheap vol, and which is right for a given strategy is exactly the sort of
    question the search should settle rather than inherit.
    """

    w_box_center: float = 1.0
    w_premium: float = 0.0
    w_iv: float = 0.0
    w_rvol: float = 0.0
    w_spread: float = 0.0

    @property
    def is_default(self) -> bool:
        """True when this policy reproduces the pre-policy selector exactly."""
        return (self.w_box_center == 1.0 and self.w_premium == 0.0 and self.w_iv == 0.0
                and self.w_rvol == 0.0 and self.w_spread == 0.0)


@dataclass(frozen=True)
class PolicyContext:
    """Everything about the request that is not the candidate list.

    ``target`` is the box CENTRE in the strike method's own units — a delta for ``delta``, a
    percentage for ``percent_otm``, unused for ``consensus_target``.

    THE BOX FILTER APPLIES ONLY WHEN ``box_min < box_max``. A degenerate or absent box means
    "aim at ``target``, filter nothing", which is what preserves compatibility with the existing
    single-``strike_param`` rules: filtering a chain down to contracts whose delta is exactly
    0.30 would leave nothing at all.
    """

    strike_method: str                      # "delta" | "percent_otm" | "consensus_target"
    target: Optional[float] = None
    box_min: Optional[float] = None
    box_max: Optional[float] = None
    spot: Optional[float] = None
    target_price: Optional[float] = None    # for consensus_target
    option_type: Optional[OptionRight] = None
    today: Optional[date] = None            # for the premium feature's annualisation


def _mark(c: OptionContract) -> Optional[float]:
    """The contract's price: mid when both sides quote, else last. None when neither exists."""
    return c.mid if c.mid is not None else c.last


def distance_from_target(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """How far this contract is from the box centre, in the strike method's own units.

    None when the contract cannot be measured at all (a delta method against a contract with no
    delta). ``pick`` excludes those candidates outright rather than scoring them worst — see its
    docstring in the next task for why that exactness matters.
    """
    if ctx.strike_method == "delta":
        if c.delta is None or ctx.target is None:
            return None
        return abs(abs(c.delta) - abs(ctx.target))
    ts = target_strike(ctx.strike_method, ctx.target, ctx.spot, ctx.target_price,
                       ctx.option_type)
    if ts is None:
        return None
    return abs(c.strike - ts)


def box_value(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """The quantity the box's bounds are expressed in, for this contract.

    ``delta`` -> absolute delta. ``percent_otm`` -> how far out of the money, in percent.
    ``consensus_target`` has no parameter, so it has no box and this is never consulted.
    """
    if ctx.strike_method == "delta":
        return None if c.delta is None else abs(c.delta)
    if ctx.strike_method == "percent_otm":
        if not ctx.spot:
            return None
        if ctx.option_type == OptionRight.PUT:
            return (1.0 - c.strike / ctx.spot) * 100.0
        return (c.strike / ctx.spot - 1.0) * 100.0
    return None


def _premium_richness(c: OptionContract, ctx: PolicyContext) -> Optional[float]:
    """Annualised premium as a fraction of the strike: ``(mark / strike) * 365/dte``.

    A per-contract ratio, so it is comparable between a $15 and a $900 underlying before
    normalisation even begins. None when it cannot be computed — notably a same-day expiry,
    which is not a division opportunity (``365/0`` is infinite and infinity beats every peer).
    """
    mark = _mark(c)
    if mark is None or not c.strike or ctx.today is None:
        return None
    dte = (c.expiry - ctx.today).days
    if dte <= 0:
        return None
    return (mark / c.strike) * (_DAYS_PER_YEAR / dte)


def _normalise(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Min-max each value onto [0, 1]. ``None`` stays ``None`` for the caller to fail closed.

    A DEGENERATE RANGE (every present value equal, including the single-candidate case) maps
    everything to 0.0 rather than dividing by zero. That is not a cop-out: a feature that cannot
    distinguish the candidates must not contribute to the ranking, and an equal contribution to
    all of them is exactly no contribution.
    """
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    if hi - lo <= 0:
        return [None if v is None else 0.0 for v in values]
    return [None if v is None else (v - lo) / (hi - lo) for v in values]


def _maximise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a higher-is-better raw feature, failing closed on missing values."""
    return [_WORST if v is None else v for v in _normalise(values)]


def _minimise(values: Sequence[Optional[float]]) -> List[float]:
    """Normalise a lower-is-better raw feature (distance, spread) and invert it.

    A missing value lands on ``_WORST`` AFTER inversion, not before — otherwise "unknown" would
    invert into the best possible score, which is the fail-OPEN this codebase keeps having to
    remove.
    """
    return [_WORST if v is None else 1.0 - v for v in _normalise(values)]


def feature_matrix(candidates: Sequence[OptionContract],
                   ctx: PolicyContext) -> Dict[str, List[float]]:
    """Every feature for every candidate, each normalised to [0, 1] and oriented so that
    HIGHER IS BETTER. Keys are ``FEATURE_NAMES``; each value is a list parallel to
    ``candidates``."""
    return {
        "box_center": _minimise([distance_from_target(c, ctx) for c in candidates]),
        "premium": _maximise([_premium_richness(c, ctx) for c in candidates]),
        "iv": _maximise([c.implied_volatility for c in candidates]),
        "rvol": _maximise([None if c.volume is None else float(c.volume)
                           for c in candidates]),
        "spread": _minimise([c.spread_pct for c in candidates]),
    }
