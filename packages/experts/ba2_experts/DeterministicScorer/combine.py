"""DeterministicScorer - score combination & decision, pure logic.

Weighted linear core -> tanh compression -> veto gates -> regime exposure
multiplier -> Schmitt-trigger decision + ATR-based profit target (memo §4).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np

DEF_W_TECHNICAL = 0.50
DEF_W_FUNDAMENTAL = 0.30
DEF_W_ANALYST = 0.00
DEF_W_MACRO_AS_INPUT = 0.20   # only when macro_mode == "input"
DEF_K_COMPRESS = 0.6
DEF_THETA_BUY = 0.5
DEF_THETA_SELL = 0.2
DEF_VETO_CAP = -0.5
DEF_K_STOP = 2.5
DEF_K_TARGET = 4.5


def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    """Renormalize positive weights to sum to 1 (zero-sum -> equal weights)."""
    pos = {k: float(v) for k, v in weights.items() if v is not None and float(v) > 0}
    total = sum(pos.values())
    if total <= 0:
        return {k: 1.0 / len(weights) for k in weights}
    return {k: v / total for k, v in pos.items()}


def combine_section_scores(section_scores: Dict[str, Optional[float]],
                           weights: Dict[str, float]) -> Dict[str, Any]:
    """Weighted average over the AVAILABLE sections (None sections renormalize).

    `section_scores`: {"technical": T, "fundamental": F, "analyst": A, "macro": R}
    (macro included only in 'input' mode; in 'multiply' mode it acts later as
    the exposure multiplier instead).
    """
    w = normalize_weights(weights)
    used = {k: (w.get(k, 0.0), v) for k, v in section_scores.items()
            if v is not None and w.get(k, 0.0) > 0}
    total_w = sum(wt for wt, _ in used.values())
    if total_w <= 0:
        return {"raw": 0.0, "components": {}, "n_sections": 0}
    raw = sum(wt * v for wt, v in used.values()) / total_w
    return {
        "raw": float(raw),
        "components": {k: {"weight": wt, "score": v} for k, (wt, v) in used.items()},
        "n_sections": len(used),
    }


def compress(raw: float, k: float = DEF_K_COMPRESS) -> float:
    """tanh compression keeps the score differentiable and bounds extremes."""
    if k <= 0:
        return float(np.clip(raw, -1.0, 1.0))
    return float(math.tanh(raw / k))


def apply_vetoes(score: float, veto: bool, veto_cap: float = DEF_VETO_CAP) -> float:
    """Hard veto (Altman distress, Piotroski disqualify) caps the score so a
    strong momentum cannot 'buy' a distressed name."""
    return min(score, veto_cap) if veto else score


def final_score(technical: Optional[float], fundamental: Optional[float],
                analyst: Optional[float], regime: Optional[float],
                s: Dict[str, Any], veto: bool = False) -> Dict[str, Any]:
    """Full deterministic pipeline: combine -> compress -> veto -> regime.

    macro_mode: 'multiply' (default, regime scales exposure), 'gate' (regime
    must be > macro_gate_min or signal is flattened), 'input' (regime is just a
    4th weighted section), 'off'.
    """
    mode = str(s.get("macro_mode", "multiply")).lower()
    weights = {
        "technical": float(s.get("w_technical", DEF_W_TECHNICAL)),
        "fundamental": float(s.get("w_fundamental", DEF_W_FUNDAMENTAL)),
        "analyst": float(s.get("w_analyst", DEF_W_ANALYST)),
    }
    sections = {"technical": technical, "fundamental": fundamental, "analyst": analyst}
    if mode == "input":
        weights["macro"] = float(s.get("w_macro", DEF_W_MACRO_AS_INPUT))
        sections["macro"] = regime

    comb = combine_section_scores(sections, weights)
    score = compress(comb["raw"], float(s.get("k_compress", DEF_K_COMPRESS)))
    score = apply_vetoes(score, veto, float(s.get("veto_cap", DEF_VETO_CAP)))

    m = 1.0
    if regime is not None and mode == "multiply":
        from .macro import exposure_multiplier, DEF_M_FLOOR, DEF_HARD_RISKOFF
        m = exposure_multiplier(regime, float(s.get("m_floor", DEF_M_FLOOR)),
                                float(s.get("hard_riskoff", DEF_HARD_RISKOFF)))
        score = score * m
    elif regime is not None and mode == "gate":
        gate_min = float(s.get("macro_gate_min", -0.5))
        if regime < gate_min:
            score = min(score, 0.0)

    return {
        "final": float(np.clip(score, -1.0, 1.0)),
        "raw": comb["raw"],
        "components": comb["components"],
        "regime": regime,
        "exposure_multiplier": m,
        "veto": veto,
        "macro_mode": mode,
        "n_sections": comb["n_sections"],
    }


def schmitt_trigger(final: float, s: Dict[str, Any],
                    prev_signal: Optional[str] = None) -> str:
    """Asymmetric thresholds with hysteresis: BUY above +theta_buy, SELL below
    -theta_sell, HOLD in the band. `min_score_delta` (optional) requires the
    score to move further past the threshold when already positioned, reducing
    churn around the boundary."""
    theta_buy = float(s.get("theta_buy", DEF_THETA_BUY))
    theta_sell = float(s.get("theta_sell", DEF_THETA_SELL))
    delta = float(s.get("min_score_delta", 0.0) or 0.0)

    if prev_signal == "BUY":
        if final < -theta_sell:
            return "SELL"
        return "BUY" if final > theta_buy else "HOLD"
    if final > theta_buy + (delta if prev_signal == "SELL" else 0.0):
        return "BUY"
    if final < -(theta_sell + (delta if prev_signal == "BUY" else 0.0)):
        return "SELL"
    return "HOLD"


def atr_target_price(price: float, atr: Optional[float], direction: str,
                     s: Dict[str, Any], final: Optional[float] = None) -> Optional[float]:
    """Profit target = price ± k_target * ATR. With target_from_score the
    multiple stretches up to ±30% with |final| (high conviction -> wider target)."""
    if price is None or atr is None or atr <= 0 or not math.isfinite(atr):
        return None
    k = float(s.get("k_target", DEF_K_TARGET))
    if s.get("target_from_score", False) and final is not None:
        k = k * (1.0 + 0.3 * min(1.0, abs(final)))
    if direction == "BUY":
        return float(price + k * atr)
    return float(price - k * atr)


def atr_stop_price(price: float, atr: Optional[float], direction: str,
                   s: Dict[str, Any]) -> Optional[float]:
    """Stop hint = price ∓ k_stop * ATR (passed to the risk manager via
    raw_outputs; the platform RM keeps final authority)."""
    if price is None or atr is None or atr <= 0 or not math.isfinite(atr):
        return None
    k = float(s.get("k_stop", DEF_K_STOP))
    if direction == "BUY":
        return float(price - k * atr)
    return float(price + k * atr)


def confidence_from_score(final: float) -> float:
    """Map |final| to the platform's 1-100 confidence scale."""
    return float(max(5.0, 100.0 * min(1.0, abs(final))))
