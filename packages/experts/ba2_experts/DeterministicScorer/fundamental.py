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


def piotroski_f_score(cur: Dict[str, Any], prior: Dict[str, Any]) -> Optional[int]:
    """Piotroski F-Score (0-9) from two statement snapshots (current vs prior FY).

    Expected numeric fields (FMP-style): netIncome, totalAssets,
    totalAssetsPrior (or caller passes prior snapshot separately), operatingCashFlow,
    longTermDebt, currentAssets, currentLiabilities, commonStock (shares),
    grossProfit, revenue, totalAssetsPrevYear etc. Missing components are NOT
    counted against the score; returns None when fewer than 6 components computable.
    """
    if not cur or not prior:
        return None
    points = 0
    computed = 0

    # 1. ROA > 0
    roa = _safe_div(_fnum(cur, "netIncome", "net_income"), _fnum(cur, "totalAssets", "total_assets"))
    if roa is not None:
        computed += 1
        points += 1 if roa > 0 else 0

    # 2. CFO > 0
    cfo = _fnum(cur, "operatingCashFlow", "operating_cash_flow",
                "netCashProvidedByOperatingActivities")
    if cfo is not None:
        computed += 1
        points += 1 if cfo > 0 else 0

    # 3. ROA increasing YoY
    roa_prior = _safe_div(_fnum(prior, "netIncome", "net_income"),
                          _fnum(prior, "totalAssets", "total_assets"))
    if roa is not None and roa_prior is not None:
        computed += 1
        points += 1 if roa > roa_prior else 0

    # 4. Accruals: CFO/Assets > ROA
    cfo_a = _safe_div(cfo, _fnum(cur, "totalAssets", "total_assets"))
    if cfo_a is not None and roa is not None:
        computed += 1
        points += 1 if cfo_a > roa else 0

    # 5. Leverage: long-term debt/Assets decreased (strict: Piotroski awards the
    # point for a DECREASE, and `<=` handed a free point to every debt-free name)
    lev = _safe_div(_fnum(cur, "longTermDebt", "long_term_debt"),
                    _fnum(cur, "totalAssets", "total_assets"))
    lev_prior = _safe_div(_fnum(prior, "longTermDebt", "long_term_debt"),
                          _fnum(prior, "totalAssets", "total_assets"))
    if lev is not None and lev_prior is not None:
        computed += 1
        points += 1 if lev < lev_prior else 0

    # 6. Liquidity: current ratio increased. netReceivables is only ONE component
    # of current assets, so it was never a valid alias -- it produced a bogus ratio.
    cr = _safe_div(_fnum(cur, "totalCurrentAssets", "total_current_assets", "currentAssets"),
                   _fnum(cur, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities"))
    cr_prior = _safe_div(_fnum(prior, "totalCurrentAssets", "total_current_assets", "currentAssets"),
                         _fnum(prior, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities"))
    if cr is not None and cr_prior is not None:
        computed += 1
        points += 1 if cr > cr_prior else 0

    # 7. No dilution: share count flat or down (weighted avg shares from the
    # income statement; the provider's balance 'common_stock' is a dollar amount)
    sh = _fnum(cur, "weightedAverageShsOut", "weighted_average_shares_outstanding")
    sh_prior = _fnum(prior, "weightedAverageShsOut", "weighted_average_shares_outstanding")
    if sh is not None and sh_prior is not None and sh_prior > 0:
        computed += 1
        points += 1 if sh <= sh_prior * 1.001 else 0

    # 8. Gross margin increased
    gm = _safe_div(_fnum(cur, "grossProfit", "gross_profit"), _fnum(cur, "revenue", "total_revenue"))
    gm_prior = _safe_div(_fnum(prior, "grossProfit", "gross_profit"), _fnum(prior, "revenue", "total_revenue"))
    if gm is not None and gm_prior is not None:
        computed += 1
        points += 1 if gm > gm_prior else 0

    # 9. Asset turnover increased
    ato = _safe_div(_fnum(cur, "revenue", "total_revenue"), _fnum(cur, "totalAssets", "total_assets"))
    ato_prior = _safe_div(_fnum(prior, "revenue", "total_revenue"), _fnum(prior, "totalAssets", "total_assets"))
    if ato is not None and ato_prior is not None:
        computed += 1
        points += 1 if ato > ato_prior else 0

    if computed < 6:
        return None
    return points


def altman_z_and_variant(cur: Dict[str, Any], market_cap: Optional[float],
                         variant: str = "auto") -> tuple:
    """Altman Z-Score AND the variant it was actually computed with.

    The variant matters downstream: the distress cutoff is 1.81 for the original
    Z but 1.1 for Z'', so a caller that only gets the number cannot threshold it
    correctly. `variant`: original (manufacturers), adjusted (Z'' for
    non-manufacturers/emerging), auto (= original when market cap present).
    Financials should be skipped by the caller (opaque balance sheets).
    Returns (z, "original"|"adjusted") or (None, None).
    """
    ta = _fnum(cur, "totalAssets", "total_assets")
    if ta is None or ta <= 0:
        return None, None
    x1 = _safe_div((_fnum(cur, "totalCurrentAssets", "total_current_assets", "currentAssets") or 0.0)
                   - (_fnum(cur, "totalCurrentLiabilities", "total_current_liabilities", "currentLiabilities") or 0.0), ta)
    x2 = _safe_div(_fnum(cur, "retainedEarnings", "retained_earnings", "totalRetainedEarnings"), ta)
    x3 = _safe_div(_fnum(cur, "ebit", "operatingIncome", "operating_income"), ta)
    x5 = _safe_div(_fnum(cur, "revenue", "total_revenue"), ta)
    if x1 is None or x2 is None or x3 is None or x5 is None:
        return None, None

    # totalDebt is NOT a stand-in for totalLiabilities (debt is a subset): using
    # it shrinks the denominator and inflates Z, i.e. it hides distress.
    tl = _fnum(cur, "totalLiabilities", "total_liabilities")
    use_original = variant == "original" or (variant == "auto" and market_cap is not None)
    if use_original and market_cap is not None:
        x4 = _safe_div(market_cap, tl) if tl else None
        if x4 is None:
            return None, None
        return 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5, "original"
    # Z'' adjusted (book equity for X4; +3.25 constant for emerging omitted by default)
    be = _fnum(cur, "bookValue", "totalStockholdersEquity", "total_shareholder_equity",
               "totalEquity")
    x4b = _safe_div(be, tl)
    if x4b is None:
        return None, None
    return 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4b, "adjusted"


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


def growth_acceleration(history: List[Optional[float]], min_points: int = 6) -> Optional[float]:
    """Growth of the latest period vs the trailing average growth.

    `history` = ascending values (revenue or EPS). Returns the raw acceleration
    (the caller tanh-scales it). Negative bases are handled by `_period_growth`.
    """
    vals = [v for v in history if v is not None and math.isfinite(v)]
    if len(vals) < min_points:
        return None
    growth = [g for g in (_period_growth(vals[i], vals[i - 1])
                          for i in range(1, len(vals))) if g is not None]
    if len(growth) < 2:
        return None
    latest = growth[-1]
    trail = float(np.mean(growth[:-1][-4:]))
    return latest - trail


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

    components: Dict[str, tuple] = {}
    fscore = snapshot.get("fscore")
    if fscore is not None:
        components["piotroski"] = (fw_p, (fscore - 4.5) / 4.5)
    if snapshot.get("quality_norm") is not None:
        components["quality"] = (fw_q, float(snapshot["quality_norm"]))
    if snapshot.get("value_norm") is not None:
        components["value"] = (fw_v, float(snapshot["value_norm"]))
    accels = [a for a in (snapshot.get("rev_accel"), snapshot.get("eps_accel")) if a is not None]
    if accels:
        components["growth"] = (fw_g, _tanh(float(np.mean(accels)), scale_accel))

    total_w = sum(w for w, _ in components.values())
    # None (not 0.0) when nothing was computable: 0.0 is a real "neutral"
    # reading and would keep the section's full weight, quietly shrinking every
    # score of every symbol that simply has no fundamentals. The caller decides
    # what missing means via skip_on_missing_section.
    score = float(np.clip(sum(w * v for w, v in components.values()) / total_w,
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
        "components": {k: {"weight": w, "normalized": v} for k, (w, v) in components.items()},
        "fscore": fscore, "z": z, "z_variant": variant, "z_veto_used": z_veto,
        "veto": veto or disq,
        "veto_reason": ("altman_z_distress" if veto else
                        ("piotroski_disqualify" if disq else None)),
        "n_signals": len(components),
    }
