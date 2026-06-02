"""Rebalance math and execution for FactorRanker.

``rebalance_deltas`` is pure (target weights + holdings + prices -> signed share
deltas) and unit tested directly. ``FactorPortfolioManager`` (added later) wraps it
with DB/account access to actually submit the orders.
"""

import math
from typing import Dict


def rebalance_deltas(target_weights: Dict[str, float], held_shares: Dict[str, float],
                     prices: Dict[str, float], equity: float) -> Dict[str, float]:
    """Signed whole-share deltas to move from current holdings to target weights.

    target_shares = floor(weight * equity / price); delta = target - held. Names
    held but absent from the target weight 0 (sold down). A held name we must exit
    but cannot price is still fully sold using its held quantity. Zero deltas are
    omitted from the result.
    """
    deltas: Dict[str, float] = {}
    symbols = set(target_weights) | set(held_shares)
    for s in symbols:
        price = prices.get(s)
        if price is None or price <= 0:
            # Can't price a held name we must exit -> still allow full sell using held qty
            if s in held_shares and target_weights.get(s, 0.0) == 0.0:
                deltas[s] = -float(held_shares[s])
            continue
        target_shares = math.floor((target_weights.get(s, 0.0) * equity) / price)
        delta = target_shares - float(held_shares.get(s, 0.0))
        if delta != 0.0:
            deltas[s] = float(delta)
    return deltas
