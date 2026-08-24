"""DeterministicScorer - FUNDAMENTAL section, pure calculators.

Point-in-time rule: every statement input must be filtered by FILING date
(`fillingDate`/`acceptedDate`) <= as_of BEFORE these functions are called.
These calculators only do math on what they're given.

Evidence base (memo §2): Piotroski F-Score (2000), Altman Z (1968/2000),
Novy-Marx gross profitability (2013), EV/EBIT + FCF yield value metrics,
growth acceleration / SUE-style earnings momentum.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

# --- Defaults ----------------------------------------------------------------
DEF_FW_PIOTROSKI = 0.25
DEF_FW_QUALITY = 0.30
DEF_FW_VALUE = 0.25
DEF_FW_GROWTH = 0.20

# Altman distress cutoffs are NOT interchangeable between variants: the original
# Z (manufacturers, market-cap X4) breaks at 1.81, while Z'' (adjusted, book
# equity, no X5) breaks at 1.1. Applying 1.8 to a Z'' score vetoes most healthy
# non-manufacturers, so the threshold follows the variant that was actually used.
DEF_Z_VETO = 1.8          # original Z distress zone
DEF_Z_VETO_ADJUSTED = 1.1  # Z'' distress zone
DEF_Z_GREY = 2.99
DEF_SCALE_ACCEL = 0.05    # 5pp growth acceleration saturates the tanh

# Quality/value normalization: tanh((x - neutral) / k). These were inline
# literals in the expert's _build_fundamental; they are named here so the
# EXPLANATION of a displayed score quotes the same neutral point and scale the
# maths used, instead of a re-typed copy that can silently drift out of step.
DEF_QUALITY_NEUTRAL_ROE = 0.10    # 10% ROE scores 0.0
DEF_QUALITY_K = 0.10              # +-10pp of ROE spans most of (-1, 1)
DEF_VALUE_NEUTRAL_YIELD = 0.10    # 10% EBIT/EV yield scores 0.0
DEF_VALUE_K = 0.10


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b == 0 or not math.isfinite(a) or not math.isfinite(b):
        return None
    return a / b


def _fnum(d: Dict[str, Any], *keys: str) -> Optional[float]:
    """First non-None numeric among candidate key names (FMP field aliases)."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def piotroski_f_score_detail(cur: Dict[str, Any], prior: Dict[str, Any]) -> Dict[str, Any]:
    """Piotroski F-Score PLUS the nine tests that produced it.

    Same arithmetic as ``piotroski_f_score`` (which delegates here) -- this
    variant additionally reports, per test, the measured value, the value it
    was compared against, and whether it passed, failed, or was NOT COMPUTABLE.
    The three are genuinely different: Piotroski does not count a missing input
    against the score, so rendering an uncomputable test as a failure would
    misstate why a 4/9 is a 4/9.

    Returns ``{"score", "computed", "components": [...]}``; ``score`` is None
    when fewer than 6 tests were computable (the documented contract), but the
    components are still reported so a caller can say WHAT was missing.
    """
    def check(name: str, rule: str, current, comparator, passed) -> Dict[str, Any]:
        return {"name": name, "rule": rule, "current": current,
                "comparator": comparator, "passed": passed}

    if not cur or not prior:
        return {"score": None, "computed": 0, "components": []}

    components: List[Dict[str, Any]] = []

    # 1. ROA > 0
    roa = _safe_div(_fnum(cur, "netIncome", "net_income"), _fnum(cur, "totalAssets", "total_assets"))
    components.append(check("roa_positive", "ROA > 0", roa, 0.0,
                            None if roa is None else roa > 0))

    # 2. CFO > 0
    cfo = _fnum(cur, "operatingCashFlow", "operating_cash_flow",
                "netCashProvidedByOperatingActivities")
    components.append(check("cfo_positive", "operating cash flow > 0", cfo, 0.0,
                            None if cfo is None else cfo > 0))

    # 3. ROA increasing YoY
    roa_prior = _safe_div(_fnum(prior, "netIncome", "net_income"),
                          _fnum(prior, "totalAssets", "total_assets"))
    components.append(check(
        "roa_increased", "ROA > prior-year ROA", roa, roa_prior,
        None if (roa is None or roa_prior is None) else roa > roa_prior))

    # 4. Accruals: CFO/Assets > ROA
    cfo_a = _safe_div(cfo, _fnum(cur, "totalAssets", "total_assets"))
    components.append(check(
        "accruals_clean", "CFO/assets > ROA", cfo_a, roa,
        None if (cfo_a is None or roa is None) else cfo_a > roa))

    # 5. Leverage: long-term debt/Assets decreased (strict: Piotroski awards the
    # point for a DECREASE, and `<=` handed a free point to every debt-free name)
    lev = _safe_div(_fnum(cur, "longTermDebt", "long_term_debt"),
                    _fnum(cur, "totalAssets", "total_assets"))
    lev_prior = _safe_div(_fnum(prior, "longTermDebt", "long_term_debt"),
                          _fnum(prior, "totalAssets", "total_assets"))
    components.append(check(
        "leverage_decreased", "LT debt/assets < prior year", lev, lev_prior,
        None if (lev is None or lev_prior is None) else lev < lev_prior))

    # 6. Liquidity: current ratio increased. netReceivables is only ONE component
    # of current assets, so it was never a valid alias -- it produced a bogus ratio.
    cr = _safe_div(_fnum(cur, "totalCurrentAssets", "total_current_assets", "currentAssets"),
                   _fnum(cur, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities"))
    cr_prior = _safe_div(_fnum(prior, "totalCurrentAssets", "total_current_assets", "currentAssets"),
                         _fnum(prior, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities"))
    components.append(check(
        "current_ratio_increased", "current ratio > prior year", cr, cr_prior,
        None if (cr is None or cr_prior is None) else cr > cr_prior))

    # 7. No dilution: share count flat or down (weighted avg shares from the
    # income statement; the provider's balance 'common_stock' is a dollar amount)
    sh = _fnum(cur, "weightedAverageShsOut", "weighted_average_shares_outstanding")
    sh_prior = _fnum(prior, "weightedAverageShsOut", "weighted_average_shares_outstanding")
    sh_ok = (sh is not None and sh_prior is not None and sh_prior > 0)
    components.append(check(
        "no_dilution", "shares out <= prior year x 1.001", sh,
        (sh_prior * 1.001) if sh_ok else sh_prior,
        (sh <= sh_prior * 1.001) if sh_ok else None))

    # 8. Gross margin increased
    gm = _safe_div(_fnum(cur, "grossProfit", "gross_profit"), _fnum(cur, "revenue", "total_revenue"))
    gm_prior = _safe_div(_fnum(prior, "grossProfit", "gross_profit"), _fnum(prior, "revenue", "total_revenue"))
    components.append(check(
        "gross_margin_increased", "gross margin > prior year", gm, gm_prior,
        None if (gm is None or gm_prior is None) else gm > gm_prior))

    # 9. Asset turnover increased
    ato = _safe_div(_fnum(cur, "revenue", "total_revenue"), _fnum(cur, "totalAssets", "total_assets"))
    ato_prior = _safe_div(_fnum(prior, "revenue", "total_revenue"), _fnum(prior, "totalAssets", "total_assets"))
    components.append(check(
        "asset_turnover_increased", "revenue/assets > prior year", ato, ato_prior,
        None if (ato is None or ato_prior is None) else ato > ato_prior))

    computed = sum(1 for c in components if c["passed"] is not None)
    points = sum(1 for c in components if c["passed"])
    return {"score": points if computed >= 6 else None,
            "computed": computed, "components": components}


def piotroski_f_score(cur: Dict[str, Any], prior: Dict[str, Any]) -> Optional[int]:
    """Piotroski F-Score (0-9) from two statement snapshots (current vs prior FY).

    Expected numeric fields (FMP-style): netIncome, totalAssets,
    totalAssetsPrior (or caller passes prior snapshot separately), operatingCashFlow,
    longTermDebt, currentAssets, currentLiabilities, commonStock (shares),
    grossProfit, revenue, totalAssetsPrevYear etc. Missing components are NOT
    counted against the score; returns None when fewer than 6 components computable.

    Use ``piotroski_f_score_detail`` when you also need WHICH tests passed.
    """
    return piotroski_f_score_detail(cur, prior)["score"]


#: (name, description, numerator label) per Altman term, keyed by variant.
_Z_TERM_LABELS = {
    "X1": "working capital / total assets",
    "X2": "retained earnings / total assets",
    "X3": "EBIT / total assets",
    "X4": "market cap / total liabilities",
    "X4b": "book equity / total liabilities",
    "X5": "revenue / total assets",
}


def altman_z_detail(cur: Dict[str, Any], market_cap: Optional[float],
                    variant: str = "auto") -> Dict[str, Any]:
    """Altman Z PLUS the weighted terms that sum to it.

    ``altman_z_and_variant`` delegates here, so the terms reported are by
    construction the ones the score was built from. Each term carries its own
    ratio, the coefficient applied, and the resulting contribution -- the
    contributions sum to ``z`` exactly.

    Returns ``{"z", "variant", "terms": [...]}``; ``z``/``variant`` are None
    (and ``terms`` empty) whenever the score is not computable, rather than a
    partial sum that would look like a real reading.
    """
    empty = {"z": None, "variant": None, "terms": []}
    ta = _fnum(cur, "totalAssets", "total_assets")
    if ta is None or ta <= 0:
        return empty
    x1 = _safe_div((_fnum(cur, "totalCurrentAssets", "total_current_assets", "currentAssets") or 0.0)
                   - (_fnum(cur, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities") or 0.0), ta)
    x2 = _safe_div(_fnum(cur, "retainedEarnings", "retained_earnings", "totalRetainedEarnings"), ta)
    x3 = _safe_div(_fnum(cur, "ebit", "operatingIncome", "operating_income"), ta)
    x5 = _safe_div(_fnum(cur, "revenue", "total_revenue"), ta)
    if x1 is None or x2 is None or x3 is None or x5 is None:
        return empty

    def term(name: str, ratio: float, coeff: float) -> Dict[str, Any]:
        return {"name": name.rstrip("b") if name == "X4b" else name,
                "label": _Z_TERM_LABELS[name], "ratio": float(ratio),
                "coefficient": float(coeff), "contribution": float(coeff * ratio)}

    # totalDebt is NOT a stand-in for totalLiabilities (debt is a subset): using
    # it shrinks the denominator and inflates Z, i.e. it hides distress.
    tl = _fnum(cur, "totalLiabilities", "total_liabilities")
    use_original = variant == "original" or (variant == "auto" and market_cap is not None)
    if use_original and market_cap is not None:
        x4 = _safe_div(market_cap, tl) if tl else None
        if x4 is None:
            return empty
        terms = [term("X1", x1, 1.2), term("X2", x2, 1.4), term("X3", x3, 3.3),
                 term("X4", x4, 0.6), term("X5", x5, 1.0)]
        return {"z": 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5,
                "variant": "original", "terms": terms}
    # Z'' adjusted (book equity for X4; +3.25 constant for emerging omitted by default)
    be = _fnum(cur, "bookValue", "totalStockholdersEquity", "total_shareholder_equity",
               "totalEquity")
    x4b = _safe_div(be, tl)
    if x4b is None:
        return empty
    terms = [term("X1", x1, 6.56), term("X2", x2, 3.26), term("X3", x3, 6.72),
             term("X4b", x4b, 1.05)]
    return {"z": 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4b,
            "variant": "adjusted", "terms": terms}


def altman_z_and_variant(cur: Dict[str, Any], market_cap: Optional[float],
                         variant: str = "auto") -> tuple:
    """Altman Z-Score AND the variant it was actually computed with.

    The variant matters downstream: the distress cutoff is 1.81 for the original
    Z but 1.1 for Z'', so a caller that only gets the number cannot threshold it
    correctly. `variant`: original (manufacturers), adjusted (Z'' for
    non-manufacturers/emerging), auto (= original when market cap present).
    Financials should be skipped by the caller (opaque balance sheets).
    Returns (z, "original"|"adjusted") or (None, None).

    Use ``altman_z_detail`` when you also need the weighted terms.
    """
    d = altman_z_detail(cur, market_cap, variant)
    return d["z"], d["variant"]


def altman_z(cur: Dict[str, Any], market_cap: Optional[float],
             variant: str = "auto") -> Optional[float]:
    """Altman Z-Score only. Prefer `altman_z_and_variant` when you need to
    threshold the result (the cutoff differs per variant)."""
    return altman_z_and_variant(cur, market_cap, variant)[0]


def _tanh(x: Optional[float], k: float) -> float:
    if x is None or k <= 0 or not math.isfinite(x):
        return 0.0
    return math.tanh(x / k)


def _period_growth(cur: float, prior: float) -> Optional[float]:
    """Signed growth that stays meaningful when the base is negative.

    A plain cur/prior-1 inverts on negative bases: EPS -1 -> -2 is a
    DETERIORATION but scores +100%. Dividing by |prior| keeps the sign of the
    actual change, which is what an acceleration signal needs.
    """
    if prior is None or cur is None or prior == 0:
        return None
    if not (math.isfinite(cur) and math.isfinite(prior)):
        return None
    return (cur - prior) / abs(prior)


#: Trailing growth periods averaged as the comparator for the latest period.
DEF_ACCEL_TRAILING_PERIODS = 4


def growth_acceleration_detail(history: List[Optional[float]],
                               min_points: int = 6) -> Dict[str, Any]:
    """Growth acceleration PLUS both sides of the subtraction that made it.

    ``growth_acceleration`` delegates here. The acceleration alone
    ("+0.81") is unreadable: it is the LATEST period's growth minus the
    average of the trailing periods' growth, and neither survives the
    subtraction. This variant reports both, the period values behind the
    latest growth, and how many periods each side used -- plus, when the
    result is None, how short the history was against ``min_points``.
    """
    vals = [v for v in history if v is not None and math.isfinite(v)]
    out: Dict[str, Any] = {
        "acceleration": None, "latest_growth": None, "trailing_mean": None,
        "trailing_growths": [], "n_trailing": 0, "n_values": len(vals),
        "min_points": int(min_points), "latest_value": None, "prior_value": None,
    }
    if len(vals) < min_points:
        return out
    growth = [g for g in (_period_growth(vals[i], vals[i - 1])
                          for i in range(1, len(vals))) if g is not None]
    if len(growth) < 2:
        return out
    latest = growth[-1]
    trailing = growth[:-1][-DEF_ACCEL_TRAILING_PERIODS:]
    trail = float(np.mean(trailing))
    out.update({
        "acceleration": latest - trail, "latest_growth": latest,
        "trailing_mean": trail, "trailing_growths": [float(g) for g in trailing],
        "n_trailing": len(trailing), "latest_value": vals[-1], "prior_value": vals[-2],
    })
    return out


def growth_acceleration(history: List[Optional[float]], min_points: int = 6) -> Optional[float]:
    """Growth of the latest period vs the trailing average growth.

    `history` = ascending values (revenue or EPS). Returns the raw acceleration
    (the caller tanh-scales it). Negative bases are handled by `_period_growth`.

    Use ``growth_acceleration_detail`` when you also need the two growth rates
    the subtraction consumed.
    """
    return growth_acceleration_detail(history, min_points)["acceleration"]


def fundamental_score(snapshot: Dict[str, Any], s: Dict[str, Any]) -> Dict[str, Any]:
    """Combine F-Score, quality, value, growth into the section score [-1, 1].

    `snapshot` keys (all optional; missing components renormalize weights):
      fscore (int 0-9), z (float|None), roe, gross_profitability (GP/A),
      ev_ebit, fcf_yield, rev_accel, eps_accel (raw accelerations),
      quality_hist/value_hist scales handled by caller-provided normalized
      values when available (quality_norm, value_norm in [-1,1]).
    Returns score + veto flag + components.
    """
    fw_p = float(s.get("fw_piotroski", DEF_FW_PIOTROSKI))
    fw_q = float(s.get("fw_quality", DEF_FW_QUALITY))
    fw_v = float(s.get("fw_value", DEF_FW_VALUE))
    fw_g = float(s.get("fw_growth", DEF_FW_GROWTH))
    z_veto = float(s.get("z_veto", DEF_Z_VETO))
    scale_accel = float(s.get("scale_accel", DEF_SCALE_ACCEL))
    fscore_disqualify = int(s.get("fscore_disqualify", 0) or 0)

    # (weight, normalized, raw input, transformation label, tanh scale or None).
    # The raw input and the transformation are carried so a UI can EXPLAIN the
    # normalized number instead of just printing it: without them "quality
    # -0.91" cannot be traced back to anything, and a renderer would have to
    # back-solve a plausible ROE out of the output (i.e. invent it).
    components: Dict[str, tuple] = {}
    fscore = snapshot.get("fscore")
    if fscore is not None:
        components["piotroski"] = (fw_p, (fscore - 4.5) / 4.5, fscore,
                                   "(F-Score − 4.5) / 4.5", None)
    if snapshot.get("quality_norm") is not None:
        components["quality"] = (fw_q, float(snapshot["quality_norm"]),
                                 float(snapshot["quality_norm"]),
                                 "already normalized upstream (see quality evidence)", None)
    if snapshot.get("value_norm") is not None:
        components["value"] = (fw_v, float(snapshot["value_norm"]),
                               float(snapshot["value_norm"]),
                               "already normalized upstream (see value evidence)", None)
    accels = [a for a in (snapshot.get("rev_accel"), snapshot.get("eps_accel")) if a is not None]
    if accels:
        mean_accel = float(np.mean(accels))
        components["growth"] = (fw_g, _tanh(mean_accel, scale_accel), mean_accel,
                                "tanh(mean(revenue, EPS acceleration) / scale_accel)",
                                scale_accel)

    total_w = sum(c[0] for c in components.values())
    # None (not 0.0) when nothing was computable: 0.0 is a real "neutral"
    # reading and would keep the section's full weight, quietly shrinking every
    # score of every symbol that simply has no fundamentals. The caller decides
    # what missing means via skip_on_missing_section.
    score = float(np.clip(sum(w * v for w, v, _r, _t, _k in components.values()) / total_w,
                          -1.0, 1.0)) if total_w > 0 else None

    z = snapshot.get("z")
    # Threshold follows the variant actually used (see DEF_Z_VETO_ADJUSTED).
    variant = str(snapshot.get("z_variant") or "original").lower()
    if variant == "adjusted":
        z_veto = float(s.get("z_veto_adjusted", DEF_Z_VETO_ADJUSTED))
    veto = bool(z is not None and z < z_veto)
    disq = bool(fscore is not None and fscore_disqualify > 0 and fscore <= fscore_disqualify)

    return {
        "score": score,
        "components": {k: {"weight": w, "normalized": v, "raw": r,
                           "transform": t, "scale": s}
                       for k, (w, v, r, t, s) in components.items()},
        "weight_total": total_w,
        "fscore": fscore, "z": z, "z_variant": variant, "z_veto_used": z_veto,
        "veto": veto or disq,
        "veto_reason": ("altman_z_distress" if veto else
                        ("piotroski_disqualify" if disq else None)),
        "n_signals": len(components),
    }
