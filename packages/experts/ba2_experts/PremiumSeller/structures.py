"""Structure construction for PremiumSeller (spec §4 step 7).

Builders select expiry/strikes from a point-in-time chain and size the position
from a risk budget. They return None whenever a strike, quote or viable qty is
missing — the caller skips the underlying (no fabricated trades).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from ba2_common.core.option_types import OptionContract, OptionLeg
from ba2_common.core.types import OptionRight, OrderDirection

MULTIPLIER = 100


@dataclass
class StructureSpec:
    underlying: str
    strategy: str                     # put_credit_spread | short_put | short_strangle
    legs: List[OptionLeg]
    net_credit: float                 # per share, positive = credit received
    qty: int
    max_loss: float                   # total $ (defined-risk exact; naked = stress estimate)
    notional: float                   # total $ = short strike x 100 x qty
    expiry: date


def pick_expiry(chain: List[OptionContract], as_of: date, target_dte: int) -> Optional[date]:
    """Expiry nearest to target DTE (ties -> the earlier expiry, deterministic)."""
    exps = sorted({c.expiry for c in chain})
    if not exps:
        return None
    return min(exps, key=lambda e: (abs((e - as_of).days - target_dte), e))


def closest_to_delta(chain: List[OptionContract], expiry: date, target_delta: float) -> Optional[OptionContract]:
    """Contract at `expiry` with delta closest to target; ties -> smaller |delta|
    (further OTM). Contracts with delta None are ignored (never fabricate)."""
    cands = [c for c in chain if c.expiry == expiry and c.delta is not None]
    if not cands:
        return None
    return min(cands, key=lambda c: (abs(c.delta - target_delta), abs(c.delta)))


def _mid_credit(short: OptionContract, long: Optional[OptionContract]) -> Optional[float]:
    """Conservative fill assumption: sell at the short leg's BID, buy at the long
    leg's ASK. Any missing quote -> None (decline)."""
    if short.bid is None:
        return None
    credit = short.bid
    if long is not None:
        if long.ask is None:
            return None
        credit -= long.ask
    return credit


def _leg(c: OptionContract, side: OrderDirection) -> OptionLeg:
    return OptionLeg(contract_symbol=c.symbol, side=side, ratio_qty=1,
                     position_intent=("sell_to_open" if side == OrderDirection.SELL else "buy_to_open"),
                     option_type=c.option_type, strike=c.strike, expiry=c.expiry,
                     underlying=c.underlying)


def build_put_credit_spread(underlying, chain, as_of, target_dte, target_delta,
                            width, min_credit_ratio, risk_budget) -> Optional[StructureSpec]:
    puts = [c for c in chain if c.option_type == OptionRight.PUT]
    expiry = pick_expiry(puts, as_of, target_dte)
    if expiry is None:
        return None
    short = closest_to_delta(puts, expiry, target_delta)
    if short is None:
        return None
    long_strike = short.strike - width
    longs = [c for c in puts if c.expiry == expiry and abs(c.strike - long_strike) < 1e-9]
    if not longs:
        return None
    credit = _mid_credit(short, longs[0])
    # epsilon: the boundary is inclusive per spec (0.50/5.0 = 0.10 >= 0.10 OK); without
    # it, float artifacts (1.40 - 0.90 = 0.4999...9) would block exact-boundary credits.
    if credit is None or credit <= 0 or credit < min_credit_ratio * width - 1e-9:
        return None
    per_loss = (width - credit) * MULTIPLIER
    qty = math.floor(risk_budget / per_loss)
    if qty < 1:
        return None
    return StructureSpec(underlying, "put_credit_spread",
                         [_leg(short, OrderDirection.SELL), _leg(longs[0], OrderDirection.BUY)],
                         credit, qty, per_loss * qty, short.strike * MULTIPLIER * qty, expiry)


def build_short_put(underlying, chain, as_of, target_dte, target_delta,
                    risk_budget, max_notional) -> Optional[StructureSpec]:
    puts = [c for c in chain if c.option_type == OptionRight.PUT]
    expiry = pick_expiry(puts, as_of, target_dte)
    if expiry is None:
        return None
    short = closest_to_delta(puts, expiry, target_delta)
    if short is None:
        return None
    credit = _mid_credit(short, None)
    if credit is None or credit <= 0:
        return None
    per_risk = short.strike * MULTIPLIER          # cash-secured basis (stress estimate)
    qty = min(math.floor(risk_budget / per_risk), math.floor(max_notional / per_risk))
    if qty < 1:
        return None
    return StructureSpec(underlying, "short_put", [_leg(short, OrderDirection.SELL)],
                         credit, qty, (per_risk - credit * MULTIPLIER) * qty,
                         per_risk * qty, expiry)


def build_short_strangle(underlying, chain, as_of, target_dte, target_delta,
                         risk_budget, max_notional) -> Optional[StructureSpec]:
    expiry = pick_expiry(chain, as_of, target_dte)
    if expiry is None:
        return None
    put = closest_to_delta([c for c in chain if c.option_type == OptionRight.PUT],
                           expiry, -abs(target_delta))
    call = closest_to_delta([c for c in chain if c.option_type == OptionRight.CALL],
                            expiry, abs(target_delta))
    if put is None or call is None:
        return None
    if put.bid is None or call.bid is None:
        return None
    credit = put.bid + call.bid
    if credit <= 0:
        return None
    per_risk = max(put.strike, call.strike) * MULTIPLIER
    qty = min(math.floor(risk_budget / per_risk), math.floor(max_notional / per_risk))
    if qty < 1:
        return None
    return StructureSpec(underlying, "short_strangle",
                         [_leg(put, OrderDirection.SELL), _leg(call, OrderDirection.SELL)],
                         credit, qty, (per_risk - credit * MULTIPLIER) * qty,
                         per_risk * qty, expiry)
