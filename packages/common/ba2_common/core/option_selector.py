"""Pure option-contract selection (no DB/network/broker). Operates on OptionContract lists.

Note: the `delta` and `percent_otm` methods require a non-None `strike_param`; callers must
validate (a None param raises, by design, to surface misconfigured rulesets).
"""
from datetime import date
from typing import List, Optional, Tuple

from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight


def passes_liquidity(c: OptionContract, min_open_interest: Optional[int],
                     max_spread_pct: Optional[float]) -> bool:
    if min_open_interest is not None:
        if c.open_interest is None or c.open_interest < min_open_interest:
            return False
    if max_spread_pct is not None:
        sp = c.spread_pct
        if sp is None or sp < 0 or sp > max_spread_pct:
            return False
    return True


def filter_dte(chain: List[OptionContract], today: date,
               dte_min: Optional[int], dte_max: Optional[int]) -> List[OptionContract]:
    out = []
    for c in chain:
        dte = (c.expiry - today).days
        if dte_min is not None and dte < dte_min:
            continue
        if dte_max is not None and dte > dte_max:
            continue
        out.append(c)
    return out


def _target_strike(method, strike_param, spot, target_price, option_type) -> Optional[float]:
    if method == "percent_otm":
        if option_type == OptionRight.CALL:
            return spot * (1 + strike_param / 100.0)
        return spot * (1 - strike_param / 100.0)
    if method == "consensus_target":
        # TODO(P2 Task 5): optionally prefer strike <= target for calls / >= target for puts (currently nearest-absolute).
        return target_price
    return None


def _candidates(chain, option_type, dte_min, dte_max, today, min_oi, max_spread):
    out = [c for c in chain if c.option_type == option_type]
    out = filter_dte(out, today, dte_min, dte_max)
    out = [c for c in out if passes_liquidity(c, min_oi, max_spread)]
    return out


def _pick_by(method, cands, strike_param, spot, target_price, option_type):
    if not cands:
        return None
    if method == "delta":
        usable = [c for c in cands if c.delta is not None]
        if not usable:
            return None
        return min(usable, key=lambda c: (abs(abs(c.delta) - abs(strike_param)), c.strike))
    ts = _target_strike(method, strike_param, spot, target_price, option_type)
    if ts is None:
        return None
    return min(cands, key=lambda c: (abs(c.strike - ts), c.strike))


def select_single(chain, *, method, strike_param, spot, option_type, dte_min, dte_max, today,
                  target_price=None, min_open_interest=None, max_spread_pct=None) -> Optional[OptionContract]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest, max_spread_pct)
    return _pick_by(method, cands, strike_param, spot, target_price, option_type)


def select_vertical_spread(chain, *, method, long_param, short_param, spot, option_type,
                           dte_min, dte_max, today, target_price=None,
                           min_open_interest=None, max_spread_pct=None
                           ) -> Optional[Tuple[OptionContract, OptionContract]]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest, max_spread_pct)
    if len(cands) < 2:
        return None
    # Work within a single expiry: the earliest expiry in the window that has >=2 strikes.
    by_expiry = {}
    for c in cands:
        by_expiry.setdefault(c.expiry, []).append(c)
    for expiry in sorted(by_expiry):
        legs = by_expiry[expiry]
        if len(legs) < 2:
            continue
        long_leg = _pick_by(method, legs, long_param, spot, target_price, option_type)
        short_leg = _pick_by(method, [c for c in legs if c is not long_leg],
                             short_param, spot, target_price, option_type)
        if not long_leg or not short_leg or long_leg.strike == short_leg.strike:
            continue
        # For a debit CALL spread, long is the lower strike. Order so long<short.
        lo, hi = sorted([long_leg, short_leg], key=lambda c: c.strike)
        if option_type == OptionRight.CALL:
            return (lo, hi)   # buy lower, sell higher (debit)
        return (hi, lo)       # put debit spread: buy higher strike, sell lower
    return None


def select_wing(chain, *, center_strike, width_pct, option_type,
                dte_min, dte_max, today, expiry=None,
                min_open_interest=None, max_spread_pct=None):
    """Pick the wing contract nearest ``center_strike`` moved ``width_pct`` percent
    farther OTM (calls: up; puts: down). When ``expiry`` is given, restrict to that
    expiry (wings must share the short leg's expiry)."""
    cands = _candidates(chain, option_type, dte_min, dte_max, today,
                        min_open_interest, max_spread_pct)
    if expiry is not None:
        cands = [c for c in cands if c.expiry == expiry]
    if not cands:
        return None
    if option_type == OptionRight.CALL:
        target = center_strike * (1 + width_pct / 100.0)
    else:
        target = center_strike * (1 - width_pct / 100.0)
    return min(cands, key=lambda c: (abs(c.strike - target), c.strike))
