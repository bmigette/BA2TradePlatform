"""The protective-level adjustment that runs when an ENTRY order FILLS -- ONE pure function, TWO callers.

A market entry's take-profit and stop-loss are computed before the order fills, from a reference
price (the recommendation's ``price_at_date``, a limit price, or the then-current quote).  The fill
then lands somewhere else.  This module owns the rule for what happens to the levels at that moment:

  1. STOP: scaled to keep its proportional distance, now measured from the fill:
     ``round(fill * stop / reference, 4)``.  Long and short alike (sign-agnostic).
  2. TAKE-PROFIT: left alone, unless it is closer to the REAL fill than the minimum take-profit %,
     in which case it moves out to ``fill * (1 +/- min/100)`` (up for a long, down for a short);
     never the other way.
  3. LEGACY percent recalculation (orders carrying ``TP_SL.tp_percent`` / ``sl_percent``): the TP
     (else the SL, never both) is recomputed from the fill: ``round(fill * (1 + pct/100), 4)``.
     It runs last and overrides 1 and 2.

Callers:
  * LIVE   -- ``ba2_trade_platform.core.TradeManager._check_all_waiting_trigger_orders`` (the
              parent order reached FILLED; the exit order rows are WAITING_TRIGGER).
  * BACKTEST -- ``BacktestAccount._apply_fill`` for an entry order (the transaction's levels).

Backtest and live must run the same decision code; an asymmetry is a bug.  Everything here is pure:
no I/O, no DB, no broker types.  The callers resolve their own inputs (the reference price through
``resolve_tpsl_reference_price``, the minimum % through ``TradeActions.resolve_min_take_profit_pct``).

Refusals are loud: a stop that is to be re-based with no usable reference raises
``FillRebaseRefused``.  Skipping is the CALLER's explicit, logged decision (``rebase_stop=False``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


#: A re-base that moves a level by no more than this is rounding noise: not a change, level not rewritten.
REBASE_TOLERANCE = 1e-4


class FillRebaseRefused(ValueError):
    """The inputs cannot be re-based honestly (no fill, no reference for a stop that must move, ...)."""


def rebase_price_to_fill(target_price, reference_price, fill_price):
    """Re-scale a TP/SL target that was computed off a pre-fill reference so it keeps
    the same proportional distance from the order's ACTUAL fill.

    new = fill * (target / reference)

    Sign-agnostic (works for stops below and targets above) and a no-op when the
    reference already equals the fill. Returns target_price unchanged if any input
    is missing or the reference is non-positive.

    LENIENT by design (kept for existing callers and tests); the strict entry point is
    ``rebase_levels_at_fill``.
    """
    if not target_price or not reference_price or not fill_price or reference_price <= 0:
        return target_price
    return round(fill_price * (target_price / reference_price), 4)


def compute_tp_floor_price(
    target_price: float, entry_price: float, min_pct: float, is_long: bool
) -> Optional[float]:
    """If `target_price` is closer to `entry_price` than `min_pct`% allows, return the
    floor-enforced price; otherwise None (no adjustment needed). Pure, no I/O -- shared by the
    pre-fill Phase-2 enforcement (AdjustTakeProfitAction._enforce_minimum_distance / compute_price)
    and the post-fill re-check of the same floor against the REAL fill price."""
    if not entry_price:
        return None
    if is_long:
        actual_pct = ((target_price - entry_price) / entry_price) * 100
        if actual_pct < min_pct:
            return entry_price * (1 + min_pct / 100)
    else:
        actual_pct = ((entry_price - target_price) / entry_price) * 100
        if actual_pct < min_pct:
            return entry_price * (1 - min_pct / 100)
    return None


#: ``Transaction.meta_data`` key holding the price each protective level was COMPUTED FROM, stamped
#: at the moment the level is set: ``{"stop": <price>, "tp": <price>}`` (a missing key = unknown).
#: The fill-time re-base reads THIS (live and backtest alike); the preference chain below is only
#: the fallback for levels set before the stamp existed or from a literal price.
ANCHOR_KEY = "tpsl_anchor"
_KEEP = object()


def stamp_anchor(meta, *, stop=_KEEP, tp=_KEEP) -> dict:
    """A NEW ``meta_data`` dict with the level anchors stamped (pure).

    ``stop`` / ``tp``: the price that level was computed from; ``None`` CLEARS that level's anchor
    (the level was set from a price of unknown origin, so an older stamp no longer describes it);
    omitted = untouched. Anything else on ``meta`` is carried forward."""
    out = dict(meta) if isinstance(meta, dict) else {}
    anchors = dict(out.get(ANCHOR_KEY) or {})
    for key, val in (("stop", stop), ("tp", tp)):
        if val is _KEEP:
            continue
        if val is None:
            anchors.pop(key, None)
        else:
            anchors[key] = _positive(val, f"{key} anchor price")
    if anchors:
        out[ANCHOR_KEY] = anchors
    else:
        out.pop(ANCHOR_KEY, None)
    return out


def read_anchor(meta, level: str) -> Optional[float]:
    """The stamped anchor of ``level`` ("stop" or "tp") or None when it was never stamped."""
    if not isinstance(meta, dict):
        return None
    val = (meta.get(ANCHOR_KEY) or {}).get(level)
    return float(val) if val else None


def resolve_tpsl_reference_price(
    entry_open_price: Optional[float],
    entry_limit_price: Optional[float],
    recommendation_price: Callable[[], Optional[float]],
    current_price: Callable[[], Optional[float]],
    stamped_stop_anchor: Optional[float] = None,
) -> Optional[float]:
    """The pre-fill anchor a protective level was computed against (so it can be re-based later).

    Preference order: the realised fill (if the entry already has one -- then re-basing is a
    no-op), else the STAMPED anchor (the price the stop was actually computed from, recorded when
    the stop was set), else -- the fallback for levels that carry no stamp -- the entry's limit
    price, then the originating recommendation's ``price_at_date``, then the current quote.  The
    last two are callables so they are only evaluated when reached (each may do I/O in its
    caller).  Returns None when nothing resolves; the caller decides.
    """
    if entry_open_price:
        return entry_open_price
    if stamped_stop_anchor:
        return stamped_stop_anchor
    if entry_limit_price:
        return entry_limit_price
    rec_px = recommendation_price()
    if rec_px:
        return rec_px
    return current_price()


@dataclass(frozen=True)
class FillRebase:
    """The levels after the fill adjustment, and what was done to get them."""

    take_profit: Optional[float]
    stop_loss: Optional[float]
    stop_rebased: bool = False          # rule 1 moved the stop
    tp_floored: bool = False            # rule 2 moved the take-profit
    legacy_tp_applied: bool = False     # rule 3 (TP branch)
    legacy_sl_applied: bool = False     # rule 3 (SL branch)
    reasons: Tuple[str, ...] = ()       # human-readable, for logs

    @property
    def changed(self) -> bool:
        return self.stop_rebased or self.tp_floored or self.legacy_tp_applied or self.legacy_sl_applied


def _positive(x, what: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise FillRebaseRefused(f"{what} is not a number: {x!r}")
    if not math.isfinite(v) or v <= 0:
        raise FillRebaseRefused(f"{what} must be a positive finite price, got {x!r}")
    return v


def rebase_levels_at_fill(
    *,
    is_long: bool,
    fill_price: float,
    reference_price: Optional[float],
    take_profit: Optional[float],
    stop_loss: Optional[float],
    rebase_stop: bool = True,
    apply_tp_floor: bool = True,
    min_take_profit_pct: Optional[float] = None,
    legacy_tp_percent: Optional[float] = None,
    legacy_sl_percent: Optional[float] = None,
) -> FillRebase:
    """Apply the fill adjustment (rules 1-3 in the module docstring) to a TP/SL pair.

    ``take_profit`` / ``stop_loss``: the current levels; None (or 0) means "no such level".
    ``rebase_stop=False`` is the caller's explicit decision NOT to re-base the stop (live: the
    order carries no reference anchor); with True and a stop present, a missing/non-positive
    ``reference_price`` raises ``FillRebaseRefused``.
    ``apply_tp_floor`` + ``min_take_profit_pct``: rule 2 (raises if the floor applies to a TP
    and the minimum % is None).
    ``legacy_*_percent``: rule 3; ``legacy_tp_percent`` wins over ``legacy_sl_percent``.
    """
    fill = _positive(fill_price, "fill price")
    tp = take_profit if take_profit else None
    sl = stop_loss if stop_loss else None
    if tp is not None:
        _positive(tp, "take-profit")
    if sl is not None:
        _positive(sl, "stop-loss")
    reasons = []
    stop_rebased = tp_floored = legacy_tp = legacy_sl = False

    # Rule 1: the stop keeps its proportional distance, measured from the fill.
    if rebase_stop and sl is not None:
        ref = _positive(reference_price, "reference price (the price the stop was built from)")
        new_sl = round(fill * (sl / ref), 4)
        if abs(new_sl - sl) > REBASE_TOLERANCE:
            reasons.append(f"stop re-based {sl:.4f} -> {new_sl:.4f} (ref {ref:.4f}, fill {fill:.4f})")
            sl = new_sl
            stop_rebased = True

    # Rule 2: the take-profit floor, measured from the REAL fill (never lowers a long's TP).
    if apply_tp_floor and tp is not None:
        if min_take_profit_pct is None:
            raise FillRebaseRefused("minimum take-profit % is required to check the TP floor")
        floor = compute_tp_floor_price(tp, fill, min_take_profit_pct, is_long)
        if floor is not None:
            reasons.append(f"take-profit floored {tp:.4f} -> {floor:.4f} "
                           f"(min {min_take_profit_pct}% from fill {fill:.4f})")
            tp = floor
            tp_floored = True

    # Rule 3: legacy percent recalculation (TP branch wins; the SL branch only when no TP percent).
    if legacy_tp_percent is not None:
        new_tp = round(fill * (1 + float(legacy_tp_percent) / 100), 4)
        reasons.append(f"take-profit recalculated {tp if tp is None else format(tp, '.4f')} -> "
                       f"{new_tp:.4f} ({legacy_tp_percent}% from fill {fill:.4f})")
        tp = new_tp
        legacy_tp = True
    elif legacy_sl_percent is not None:
        new_sl = round(fill * (1 + float(legacy_sl_percent) / 100), 4)
        reasons.append(f"stop recalculated {sl if sl is None else format(sl, '.4f')} -> "
                       f"{new_sl:.4f} ({legacy_sl_percent}% from fill {fill:.4f})")
        sl = new_sl
        legacy_sl = True

    return FillRebase(
        take_profit=tp, stop_loss=sl, stop_rebased=stop_rebased, tp_floored=tp_floored,
        legacy_tp_applied=legacy_tp, legacy_sl_applied=legacy_sl, reasons=tuple(reasons))
