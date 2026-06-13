"""
Weinstein Stage Analysis (Stan Weinstein, *Secrets for Profiting in Bull and
Bear Markets*).

Classifies a stock into one of four stages from its long-term trend, using the
30-week SMA as the trend "thermometer". We approximate the weekly 30-SMA with a
150-trading-day SMA on daily closes (30 weeks × 5 sessions) and read its slope.

    Stage 1 — Base / accumulation : price ≈ flat SMA, SMA not trending.
    Stage 2 — Advancing           : price ABOVE a RISING SMA  ← the only buy zone.
    Stage 3 — Top / distribution  : price stalls, SMA flattens after an advance.
    Stage 4 — Declining           : price BELOW a FALLING SMA.

Pure functions (no IO) so they are unit-testable and reusable (screener filter
today, possibly a ruleset condition later).
"""

from typing import List, Optional


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def classify_weinstein_stage(
    closes: List[float],
    sma_period: int = 150,
    slope_lookback: int = 20,
    flat_threshold_pct: float = 0.5,
) -> dict:
    """Classify the latest bar into a Weinstein stage from daily closes.

    Args:
        closes: daily closing prices, oldest-first.
        sma_period: SMA length in trading days (150 ≈ 30 weeks).
        slope_lookback: how many bars back to measure the SMA slope (20 ≈ 4 weeks).
        flat_threshold_pct: |slope| below this (%) counts as flat, not trending.

    Returns dict: stage (1-4 or None), sma, slope_pct, price, above_sma, reason.
    """
    out = {"stage": None, "sma": None, "slope_pct": None, "price": None,
           "above_sma": None, "reason": ""}
    closes = [float(c) for c in closes if c is not None]
    if len(closes) < sma_period + slope_lookback:
        out["reason"] = (f"insufficient history ({len(closes)} bars, need "
                         f"{sma_period + slope_lookback})")
        return out

    sma_now = _sma(closes, sma_period)
    sma_prior = _sma(closes[:-slope_lookback], sma_period)
    price = closes[-1]
    if not sma_now or not sma_prior or sma_prior <= 0:
        out["reason"] = "could not compute SMA"
        return out

    slope_pct = (sma_now - sma_prior) / sma_prior * 100.0
    above = price > sma_now
    rising = slope_pct > flat_threshold_pct
    falling = slope_pct < -flat_threshold_pct

    if above and rising:
        stage = 2            # advancing — buy zone
    elif (not above) and falling:
        stage = 4            # declining
    elif above and not rising:
        stage = 3            # topping (above SMA but momentum stalled)
    else:
        stage = 1            # basing (at/below SMA, not yet trending up)

    out.update({"stage": stage, "sma": round(sma_now, 4),
                "slope_pct": round(slope_pct, 3), "price": price, "above_sma": above})
    return out


def is_stage2(closes: List[float], **kwargs) -> bool:
    """True when the latest bar is in Weinstein Stage 2 (advancing)."""
    return classify_weinstein_stage(closes, **kwargs).get("stage") == 2
