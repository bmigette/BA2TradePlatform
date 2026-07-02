"""
Ratchet-only trailing stop for PennyMomentumTrader positions past max_holding_days.

June-2026 post-mortem: the max_holding_days time stop market-closed SKYQ flat
right before a +56% run. Instead of flattening profitable positions, the expert
now switches them to a trailing stop at max_holding: the stop is tightened to
max(current stop, high-watermark × (1 - trailing_stop_pct/100)) and re-tightened
on every monitoring tick. The ratchet is monotonic — the stop NEVER loosens,
even if the exit-update LLM later rewrites the stop_loss structure (the ratchet
re-applies ``trail_stop_price`` as a floor each tick).

Note: ``account.adjust_sl`` via ruleset actions is already ratcheted
platform-wide, but this expert manages its own structured-conditions exits
(``exit_conditions.stop_loss`` evaluated by ConditionEvaluator each tick), so
the ratchet is implemented here in that mechanism.

These are pure helpers with no platform dependencies so they can be unit-tested
in isolation (like tier_tracking.py).
"""
from typing import Any, Dict, List, Optional


def update_high_watermark(
    info: Dict[str, Any], current_price: Optional[float]
) -> Optional[float]:
    """Raise ``info["high_watermark"]`` to ``current_price`` if higher.

    Never lowers the watermark. Returns the (possibly updated) watermark,
    or None if no price has ever been observed.
    """
    if current_price is not None and current_price > 0:
        hwm = info.get("high_watermark")
        if hwm is None or current_price > hwm:
            info["high_watermark"] = current_price
    return info.get("high_watermark")


def hard_stop_price_from_conditions(
    stop_loss: Any, entry_price: Optional[float]
) -> Optional[float]:
    """Extract the tightest hard stop PRICE implied by a stop_loss structure.

    Walks the composite ("all"/"any") condition tree for:
      - ``percent_below_entry`` → entry_price × (1 - percent/100)
        (requires ``entry_price``; skipped when unavailable)
      - ``price_below``         → its ``value``

    Returns the HIGHEST implied stop price found (the tightest stop), or None
    when the structure carries no derivable price stop. Signal-only stops
    (VWAP, time, ...) contribute nothing.
    """
    prices: List[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if "all" in node:
                for child in node["all"]:
                    _walk(child)
            elif "any" in node:
                for child in node["any"]:
                    _walk(child)
            else:
                ctype = node.get("type")
                if ctype == "percent_below_entry" and entry_price:
                    pct = node.get("percent")
                    if pct is not None:
                        prices.append(entry_price * (1 - float(pct) / 100.0))
                elif ctype == "price_below":
                    value = node.get("value")
                    if value is not None and float(value) > 0:
                        prices.append(float(value))
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(stop_loss)
    return max(prices) if prices else None


def apply_trailing_ratchet(
    info: Dict[str, Any],
    trailing_pct: float,
    entry_price: Optional[float] = None,
    current_price: Optional[float] = None,
) -> Optional[float]:
    """Tighten the position's stop from the high-watermark, ratchet-only.

    1. Updates the high-watermark with ``current_price`` (never lowers).
    2. Computes candidate stop = high_watermark × (1 - trailing_pct/100).
    3. New stop = max(candidate, previous ``trail_stop_price``, hard stop
       implied by the existing ``exit_conditions.stop_loss``) — the stop can
       only move UP, never down.
    4. Rewrites ``info["exit_conditions"]["stop_loss"]`` as a hard
       ``price_below`` at the new stop (safe to replace: the new stop is at
       least as tight as any hard stop it supersedes) and records the level in
       ``info["trail_stop_price"]``.

    Returns the effective stop price, or None when no stop could be derived
    (no watermark yet and no pre-existing stop).
    """
    hwm = update_high_watermark(info, current_price)

    floors: List[float] = []
    prev_trail = info.get("trail_stop_price")
    if prev_trail is not None:
        floors.append(float(prev_trail))
    exit_conds = info.get("exit_conditions") or {}
    hard = hard_stop_price_from_conditions(exit_conds.get("stop_loss"), entry_price)
    if hard is not None:
        floors.append(hard)
    if hwm is not None and trailing_pct > 0:
        floors.append(hwm * (1 - trailing_pct / 100.0))

    if not floors:
        return None

    new_stop = round(max(floors), 4)
    info["trail_stop_price"] = new_stop
    info.setdefault("exit_conditions", {})
    info["exit_conditions"]["stop_loss"] = {
        "any": [{"type": "price_below", "value": new_stop}]
    }
    return new_stop
