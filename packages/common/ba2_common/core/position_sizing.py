"""
Risk-based (ATR) position sizing.

Implements the institutional sizing rule: never risk more than a fixed fraction
of equity per trade, and let the *distance to the stop* (not a fixed notional)
determine the number of shares. A volatile name (wide stop) automatically gets a
smaller lot; the dollar risk stays constant.

    risk_dollars   = equity * risk_per_trade_pct / 100
    risk_per_share = |entry - stop|              (when a stop price is known)
                   = atr_multiplier * ATR        (otherwise — the doc's default)
    quantity       = floor(risk_dollars / risk_per_share)

The result is still clamped by the per-instrument notional ceiling and the
available balance, so risk sizing never *increases* exposure beyond the existing
caps — it only makes the lot react to volatility.

Used by BOTH the classic risk manager (TradeRiskManagement) and the smart risk
manager (SmartRiskManagerToolkit) via ``compute_risk_based_quantity``. The pure
function has no DB/IO so it is unit-testable; ``get_latest_atr`` is the thin
data-fetch wrapper.
"""

from datetime import datetime, timezone
from typing import Optional

from ba2_common.logger import logger


def compute_risk_based_quantity(
    equity: float,
    current_price: float,
    risk_per_trade_pct: float,
    *,
    stop_price: Optional[float] = None,
    atr: Optional[float] = None,
    atr_multiplier: float = 2.0,
    min_stop_pct: Optional[float] = None,
    max_position_value: Optional[float] = None,
    available_balance: Optional[float] = None,
    lot_size: Optional[int] = None,
) -> dict:
    """Compute a risk-based share quantity. Pure function (no IO).

    Args:
        equity: account/expert equity the risk percentage applies to.
        current_price: entry price reference (> 0).
        risk_per_trade_pct: max % of equity to lose if the stop triggers (e.g. 1.0).
        stop_price: concrete stop-loss price; when given (and on the correct side
            of price) the risk-per-share is |current_price - stop_price|.
        atr: latest ATR value; used for the stop distance when ``stop_price`` is
            absent (risk-per-share = atr_multiplier * atr).
        atr_multiplier: ATR multiple for the implied stop (default 2.0).
        min_stop_pct: minimum stop distance as a %% of price. The risk-per-share is
            floored at ``current_price * min_stop_pct/100`` so a very tight stop or
            low ATR cannot oversize the position (a 1%% implied stop would otherwise
            buy 7x the shares of a 7%% one). Typical floor ~7%%.
        max_position_value: per-instrument notional ceiling ($) — the lot is
            trimmed so quantity*price never exceeds it.
        available_balance: cash available — the lot is trimmed to what's affordable.
        lot_size: round-lot constraint (e.g. 100); quantity is floored to a multiple.

    Returns:
        dict with: quantity (int), risk_per_share, risk_dollars, reason (str when
        quantity is 0 explaining why), capped_by (None | 'notional' | 'balance').
    """
    out = {"quantity": 0, "risk_per_share": None, "risk_dollars": None,
           "reason": "", "capped_by": None}

    if not equity or equity <= 0:
        out["reason"] = "no equity"
        return out
    if not current_price or current_price <= 0:
        out["reason"] = "no current price"
        return out
    if not risk_per_trade_pct or risk_per_trade_pct <= 0:
        out["reason"] = "risk_per_trade_pct not set"
        return out

    risk_dollars = equity * (risk_per_trade_pct / 100.0)
    out["risk_dollars"] = risk_dollars

    # Determine risk-per-share: prefer an explicit, correctly-placed stop.
    risk_per_share = None
    if stop_price and stop_price > 0:
        dist = abs(current_price - stop_price)
        if dist > 0:
            risk_per_share = dist
    if risk_per_share is None:
        if atr and atr > 0 and atr_multiplier > 0:
            risk_per_share = atr_multiplier * atr
        else:
            out["reason"] = "no stop price and no usable ATR — cannot size by risk"
            return out

    # Floor the stop distance so an unrealistically tight stop (or tiny ATR) can't
    # oversize the position. Caps the implied stop at min_stop_pct of price.
    if min_stop_pct and min_stop_pct > 0:
        floor = current_price * (min_stop_pct / 100.0)
        if risk_per_share < floor:
            out["stop_floored"] = True
            risk_per_share = floor
    out["risk_per_share"] = risk_per_share

    qty = int(risk_dollars // risk_per_share)
    if qty < 1:
        out["reason"] = (f"risk budget ${risk_dollars:.2f} too small for risk/share "
                         f"${risk_per_share:.2f} (need a wider risk % or tighter stop)")
        return out

    # Clamp by the per-instrument notional ceiling.
    if max_position_value and max_position_value > 0:
        max_by_notional = int(max_position_value // current_price)
        if qty > max_by_notional:
            qty = max_by_notional
            out["capped_by"] = "notional"

    # Clamp by available cash.
    if available_balance is not None and available_balance >= 0:
        max_by_cash = int(available_balance // current_price)
        if qty > max_by_cash:
            qty = max_by_cash
            out["capped_by"] = "balance"

    # Round DOWN to whole lots when a lot constraint applies.
    if lot_size and lot_size > 1:
        qty = (qty // lot_size) * lot_size

    if qty < 1:
        out["reason"] = "after notional/balance/lot caps the affordable quantity is 0"
        out["quantity"] = 0
        return out

    out["quantity"] = qty
    return out


def derive_stop_for_quantity(
    equity: float,
    entry_price: float,
    quantity: int,
    risk_per_trade_pct: float,
    is_long: bool,
    *,
    min_stop_pct: float = 7.0,
) -> dict:
    """Synthesize a stop-loss for an order that has an explicit quantity but no SL.

    The stop is placed at the price where the loss equals risk_per_trade_pct of
    equity for the given quantity:

        stop_distance = (equity * risk%/100) / quantity
        sl_price      = entry - stop_distance   (long)  /  entry + stop_distance (short)

    If that stop would be TIGHTER than ``min_stop_pct`` of price (i.e. the quantity
    is so large that a 2%-account stop sits inside the noise), the quantity is
    reduced — a wider stop at the same dollar risk needs fewer shares — down to the
    largest size whose stop is at least ``min_stop_pct``. When even 1 share cannot
    keep the stop ≥ min_stop_pct within the risk budget, the order is REJECTED.

    Returns dict: sl_price, quantity (possibly reduced), rejected (bool), reason,
    stop_pct.
    """
    out = {"sl_price": None, "quantity": int(quantity or 0), "rejected": False,
           "reason": "", "stop_pct": None}
    if not equity or equity <= 0 or not entry_price or entry_price <= 0:
        out["rejected"] = True
        out["reason"] = "invalid equity/price"
        return out
    if not quantity or quantity < 1:
        out["rejected"] = True
        out["reason"] = "no quantity"
        return out
    if not risk_per_trade_pct or risk_per_trade_pct <= 0:
        out["rejected"] = True
        out["reason"] = "risk_per_trade_pct not set"
        return out

    risk_dollars = equity * (risk_per_trade_pct / 100.0)
    min_stop_dist = entry_price * (min_stop_pct / 100.0) if min_stop_pct and min_stop_pct > 0 else 0.0

    qty = int(quantity)
    if min_stop_dist > 0:
        # Largest qty whose 2%-account stop is still ≥ the min stop distance.
        max_qty = int(risk_dollars // min_stop_dist)
        if max_qty < 1:
            out["rejected"] = True
            out["reason"] = (f"even 1 share risks more than {risk_per_trade_pct:g}% of equity "
                             f"at a {min_stop_pct:g}% stop — order rejected")
            return out
        if qty > max_qty:
            qty = max_qty  # reduce so the stop widens to >= min_stop_pct

    stop_dist = risk_dollars / qty                 # loss = exactly risk_dollars here
    sl = entry_price - stop_dist if is_long else entry_price + stop_dist
    if sl <= 0:
        out["rejected"] = True
        out["reason"] = "computed stop price <= 0"
        return out
    out["quantity"] = qty
    out["sl_price"] = round(sl, 2)
    out["stop_pct"] = round(stop_dist / entry_price * 100.0, 2)
    return out


def get_latest_atr(symbol: str, indicator_provider, period: int = 14, interval: str = "1d") -> Optional[float]:
    """Fetch the latest ATR via an injected MarketIndicatorsInterface provider.

    indicator_provider: a ba2_common.core.interfaces.MarketIndicatorsInterface impl
        (the live host / backtest passes a ba2_providers PandasIndicatorCalc, or a
        pre-fetched cache adapter). Returns None on failure. ba2_common never imports
        ba2_providers, so the data source is injected rather than imported here.

    Kept separate from the pure sizing math so the calculation stays testable and
    callers can supply a cached/pre-fetched ATR instead.
    """
    if indicator_provider is None:
        logger.warning(f"get_latest_atr: no indicator_provider injected for {symbol}")
        return None
    try:
        # Pull a window comfortably longer than the ATR period for a stable value.
        lookback = max(period * 4, 60)
        result = indicator_provider.get_indicator(
            symbol, "atr", end_date=datetime.now(timezone.utc),
            lookback_days=lookback, interval=interval, format_type="dict",
        )
        # The indicator dict format exposes a flat float list under "values".
        values = (result or {}).get("values") or []
        for v in reversed(values):
            if v is not None:
                return float(v)
        logger.warning(f"get_latest_atr: no ATR value returned for {symbol}")
        return None
    except Exception as e:
        logger.warning(f"get_latest_atr failed for {symbol}: {e}")
        return None
