"""DeterministicScorer - MACRO/regime section, pure calculators.

Deterministic regime composite (memo §3): index trend dominates, plus VIX,
credit spreads, yield curve, ISM, Sahm, breadth. Output R in [-1, 1] is used
as an EXPOSURE MULTIPLIER by combine.py (never a point forecast).

All series inputs must be pre-filtered to observation date <= as_of.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Default regime weights (sum 1.0; renormalized when inputs are missing)
DEF_MW = {
    "trend_index": 0.30,
    "breadth": 0.20,
    "vix": 0.15,
    "credit": 0.10,
    "yield_curve": 0.10,
    "pmi": 0.10,
    "sahm": 0.05,
}
DEF_VIX_CALM = 15.0
DEF_VIX_STRESS = 30.0
DEF_YC_SCALE = 0.005      # 50bp spread saturates the tanh
DEF_M_FLOOR = 0.25        # minimum exposure multiplier
DEF_HARD_RISKOFF = -0.75  # regime below this -> exposure forced to 0


def trend_score(index_closes: pd.Series, sma_period: int = 200) -> Optional[float]:
    """Index vs its SMA: +1 above, -1 below (Faber-style trend gate)."""
    if index_closes is None or len(index_closes) < sma_period:
        return None
    sma = float(index_closes.iloc[-sma_period:].mean())
    last = float(index_closes.iloc[-1])
    if sma <= 0 or not math.isfinite(sma):
        return None
    return 1.0 if last > sma else -1.0


def breadth_score(closes_matrix: Optional[pd.DataFrame], sma_period: int = 200) -> Optional[float]:
    """% of universe above SMA(period), mapped to [-1, 1] (50% -> 0)."""
    if closes_matrix is None or closes_matrix.empty or len(closes_matrix) < sma_period:
        return None
    last = closes_matrix.iloc[-1]
    smas = closes_matrix.iloc[-sma_period:].mean()
    mask = last.notna() & smas.notna() & (smas > 0)
    if mask.sum() < 5:
        return None
    pct = float((last[mask] > smas[mask]).mean())
    return 2.0 * pct - 1.0


def vix_score(vix: Optional[float], calm: float = DEF_VIX_CALM,
              stress: float = DEF_VIX_STRESS) -> Optional[float]:
    """VIX level -> [-1, 1]: calm -> +1, stressed -> -1."""
    if vix is None or not math.isfinite(vix):
        return None
    x = (stress - vix) / max(1e-9, (stress - calm))
    return float(np.clip(2.0 * x - 1.0, -1.0, 1.0))


def credit_score(oas_history: Optional[pd.Series], lookback: int = 756) -> Optional[float]:
    """HY OAS z-score vs trailing ~3y, inverted (widening = risk-off)."""
    if oas_history is None or len(oas_history) < 60:
        return None
    h = oas_history.iloc[-lookback:]
    mu, sigma = float(h.mean()), float(h.std(ddof=1))
    if sigma <= 0 or not math.isfinite(sigma):
        return None
    z = (float(h.iloc[-1]) - mu) / sigma
    return float(np.clip(-math.tanh(z), -1.0, 1.0))


def yield_curve_score(spread_history: Optional[pd.Series], avg_days: int = 63,
                      scale: float = DEF_YC_SCALE) -> Optional[float]:
    """10y-3m spread, 3-month trailing average (Engle-Ng), tanh-scaled."""
    if spread_history is None or len(spread_history) < 5:
        return None
    avg = float(spread_history.iloc[-avg_days:].mean())
    return float(math.tanh(avg / scale))


def pmi_score(pmi: Optional[float]) -> Optional[float]:
    """ISM/NAPM: 50 boundary, ±5 points saturates."""
    if pmi is None or not math.isfinite(pmi):
        return None
    return float(np.clip((pmi - 50.0) / 5.0, -1.0, 1.0))


def sahm_score(unrate_history: Optional[pd.Series]) -> Optional[float]:
    """Sahm rule signal: MA3(U3) - min(U3, prior 12m); >= 0.5pp = recession onset."""
    if unrate_history is None or len(unrate_history) < 13:
        return None
    u = unrate_history.astype(float)
    ma3_now = float(u.iloc[-3:].mean())
    prior_min = float(u.iloc[-15:-3].min()) if len(u) >= 15 else float(u.iloc[:-3].min())
    sig = ma3_now - prior_min
    return float(np.clip(-sig / 0.8, -1.0, 0.0))  # only penalizes, never rewards


def regime_composite(inputs: Dict[str, Optional[float]],
                     weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Weighted composite over the NON-None inputs (weights renormalized)."""
    w = dict(weights or DEF_MW)
    used = {k: (w.get(k, 0.0), v) for k, v in inputs.items()
            if v is not None and w.get(k, 0.0) > 0}
    total_w = sum(wt for wt, _ in used.values())
    if total_w <= 0:
        return {"score": 0.0, "components": {}, "n_inputs": 0}
    score = sum(wt * v for wt, v in used.values()) / total_w
    return {
        "score": float(np.clip(score, -1.0, 1.0)),
        "components": {k: {"weight": wt, "value": v} for k, (wt, v) in used.items()},
        "n_inputs": len(used),
    }


def exposure_multiplier(regime: float, m_floor: float = DEF_M_FLOOR,
                        hard_riskoff: float = DEF_HARD_RISKOFF) -> float:
    """Regime -> exposure multiplier in [0, 1]. Floor applies except in hard
    risk-off, where exposure goes to zero."""
    if regime < hard_riskoff:
        return 0.0
    m = m_floor + (1.0 - m_floor) * (regime + 1.0) / 2.0
    return float(np.clip(m, 0.0, 1.0))
