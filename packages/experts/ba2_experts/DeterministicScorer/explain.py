"""DeterministicScorer — turn recorded evidence into readable explanations.

Pure formatting: every function takes the evidence the calculators recorded and
returns ``(text, table)`` for one card row. ``table`` is a list of
(label, value) pairs a UI draws as a small two-column table; ``text`` is the
step-by-step derivation, one step per line.

The structure deliberately mirrors FMPRating's ``details`` block, which is the
house style for this: RAW INPUTS -> the COMPARATOR -> the ARITHMETIC with real
numbers -> the RESULT. Consistency across cards beats local elegance.

The one rule that outranks readability: an input that was never recorded is
reported as ``NOT_RECORDED``. Back-solving a plausible ROE out of a tanh output
would produce a number the platform never measured, which is worse than
saying nothing.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .fundamental import (
    DEF_QUALITY_K, DEF_QUALITY_NEUTRAL_ROE, DEF_VALUE_K, DEF_VALUE_NEUTRAL_YIELD,
    DEF_Z_GREY,
)
from .technical import (
    DEF_SCALE_D200, DEF_SCALE_MOMVOL, DEF_SCALE_RSI, DEF_ADX_GATE,
)

NOT_RECORDED = "not recorded for this analysis"

Explanation = Tuple[str, List[Tuple[str, str]]]


# ------------------------------------------------------------------ helpers
def _pct(v: Optional[float], nd: int = 2) -> str:
    return f"{v * 100:.{nd}f}%" if isinstance(v, (int, float)) else NOT_RECORDED


def _spct(v: Optional[float], nd: int = 2) -> str:
    """Signed percent — growth rates read wrong without the sign."""
    return f"{v * 100:+.{nd}f}%" if isinstance(v, (int, float)) else NOT_RECORDED


def _kfmt(k: Optional[float]) -> str:
    """A tanh scale expressed in the unit it divides. Showing only '10.00%'
    hides the divisor actually written in the formula (0.10)."""
    if not isinstance(k, (int, float)):
        return NOT_RECORDED
    return f"{k:.2f} ({k * 100:.2f} percentage points)"


def _num(v: Optional[float], nd: int = 4) -> str:
    return f"{v:,.{nd}f}" if isinstance(v, (int, float)) else NOT_RECORDED


def _money(v: Optional[float]) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else NOT_RECORDED


def _signed(v: Optional[float], nd: int = 4) -> str:
    return f"{v:+,.{nd}f}" if isinstance(v, (int, float)) else NOT_RECORDED


# ------------------------------------------------------------------ quality
def explain_quality(ev: Optional[Dict[str, Any]]) -> Explanation:
    """'Quality (ROE) -0.91' — the ROE, the fixed threshold it was measured
    against, and the tanh that squashed the difference."""
    if not ev:
        return (f"Quality (ROE): raw inputs {NOT_RECORDED}. This analysis kept "
                f"only the normalized score.", [])
    roe = ev.get("roe")
    neutral = ev.get("neutral", DEF_QUALITY_NEUTRAL_ROE)
    k = ev.get("k", DEF_QUALITY_K)
    norm = ev.get("normalized")
    lines = ["Quality leg — return on equity."]
    if roe is None:
        lines.append(f"Raw ROE: {NOT_RECORDED} (only the normalized score "
                     f"{_signed(norm, 2)} survived).")
    else:
        lines.append(f"Raw measurement: ROE = net income / shareholder equity = "
                     f"{_money(ev.get('net_income'))} / {_money(ev.get('equity'))} "
                     f"= {_pct(roe)}.")
    lines.append(f"Compared against: a FIXED {_pct(neutral)} ROE threshold — this is "
                 f"an absolute quality bar, NOT a sector median and not a peer "
                 f"cross-section.")
    lines.append(f"Transformation: tanh((ROE − {_pct(neutral)}) / k) with "
                 f"k = {_kfmt(k)}, clipped to [−1, +1]; roughly ±k of ROE either "
                 f"side of the threshold spans most of the range.")
    if roe is not None:
        lines.append(f"  tanh(({roe:.4f} − {neutral:.2f}) / {k:.2f}) "
                     f"= tanh({(roe - neutral) / k:+.4f}) = {_signed(norm, 4)}")
    table: List[Tuple[str, str]] = []
    if ev.get("net_income") is not None or ev.get("equity") is not None:
        table.append(("Net income", _money(ev.get("net_income"))))
        table.append(("Shareholder equity", _money(ev.get("equity"))))
    table.append(("ROE (raw)", _pct(roe)))
    table.append(("Neutral threshold (fixed)", _pct(neutral)))
    table.append(("tanh scale k", _kfmt(k)))
    table.append(("Quality score", _signed(norm, 4)))
    return "\n".join(lines), table


# -------------------------------------------------------------------- value
def explain_value(ev: Optional[Dict[str, Any]]) -> Explanation:
    """'Value (earnings yield) -0.79' — which yield, on what enterprise value."""
    if not ev:
        return (f"Value (earnings yield): raw inputs {NOT_RECORDED}. This analysis "
                f"kept only the normalized score.", [])
    ey = ev.get("earnings_yield")
    neutral = ev.get("neutral", DEF_VALUE_NEUTRAL_YIELD)
    k = ev.get("k", DEF_VALUE_K)
    norm = ev.get("normalized")
    mc, tl, cash = ev.get("market_cap"), ev.get("total_liabilities"), ev.get("cash")
    ev_total = ev.get("enterprise_value")
    lines = ["Value leg — OPERATING earnings yield on enterprise value "
             "(EBIT / EV), not earnings-per-share over price."]
    lines.append(f"Enterprise value = market cap + total liabilities − cash = "
                 f"{_money(mc)} + {_money(tl)} − {_money(cash)} = {_money(ev_total)}.")
    if ey is None:
        lines.append(f"Raw yield: {NOT_RECORDED}.")
    else:
        lines.append(f"Raw measurement: operating income / EV = "
                     f"{_money(ev.get('operating_income'))} / {_money(ev_total)} "
                     f"= {_pct(ey)}.")
    lines.append(f"Compared against: a FIXED {_pct(neutral)} neutral yield (cheap = "
                 f"high yield = positive) — not a sector median, not a peer "
                 f"cross-section.")
    lines.append(f"Transformation: tanh((yield − {_pct(neutral)}) / k) with "
                 f"k = {_kfmt(k)}, clipped to [−1, +1].")
    if ey is not None:
        lines.append(f"  tanh(({ey:.4f} − {neutral:.2f}) / {k:.2f}) "
                     f"= tanh({(ey - neutral) / k:+.4f}) = {_signed(norm, 4)}")
    table = [
        ("Operating income (EBIT)", _money(ev.get("operating_income"))),
        ("Market cap", _money(mc)),
        ("Total liabilities", _money(tl)),
        ("Cash", _money(cash)),
        ("Enterprise value", f"{_money(ev_total)}  (market cap + liabilities - cash)"),
        ("Earnings yield (raw)", _pct(ey)),
        ("Neutral yield (fixed)", _pct(neutral)),
        ("tanh scale k", _kfmt(k)),
        ("Value score", _signed(norm, 4)),
    ]
    return "\n".join(lines), table


# ------------------------------------------------------------------- growth
def explain_growth(kind: str, d: Optional[Dict[str, Any]]) -> Explanation:
    """'Revenue acceleration +0.81' — latest growth minus trailing average."""
    if not d:
        return (f"{kind} acceleration: the underlying growth rates are "
                f"{NOT_RECORDED}.", [])
    accel = d.get("acceleration")
    if accel is None:
        return (f"{kind} acceleration: not computable — only {d.get('n_values')} "
                f"statement periods were available and at least "
                f"{d.get('min_points')} are required.", [])
    latest, trail = d.get("latest_growth"), d.get("trailing_mean")
    n_trail = d.get("n_trailing")
    lines = [
        f"{kind} acceleration — is growth speeding up or slowing down?",
        f"Raw measurement: latest period went {_money(d.get('prior_value'))} → "
        f"{_money(d.get('latest_value'))}, i.e. {_spct(latest)} "
        f"(change / |prior|, so a negative base keeps its sign).",
        f"Compared against: this symbol's OWN trailing {n_trail}-period average "
        f"growth of {_spct(trail)} — a self-comparison over time, not a peer "
        f"comparison.",
        f"Transformation: none at this row — acceleration = latest − trailing = "
        f"{_spct(latest)} − {_spct(trail)} = {_signed(accel, 4)} "
        f"({_spct(accel)} of growth).",
        f"(The FUNDAMENTAL section then tanh-scales the mean of the revenue and "
        f"EPS accelerations; see that row for the scale used.)",
    ]
    table = [
        ("Latest period", f"{_money(d.get('prior_value'))} → {_money(d.get('latest_value'))}"),
        ("Latest period growth", _spct(latest)),
        (f"Trailing average growth ({n_trail} periods)", _spct(trail)),
        ("Trailing growths", ", ".join(_spct(g) for g in (d.get("trailing_growths") or []))
         or NOT_RECORDED),
        ("Acceleration", _signed(accel, 4)),
    ]
    return "\n".join(lines), table


# ---------------------------------------------------------------- piotroski
def explain_piotroski(d: Optional[Dict[str, Any]]) -> Explanation:
    """'Piotroski F-Score 4 / 9' — which five tests it lost."""
    if not d or not d.get("components"):
        return (f"Piotroski F-Score: the nine individual tests are {NOT_RECORDED} "
                f"for this analysis.", [])
    passed = [c for c in d["components"] if c["passed"] is True]
    failed = [c for c in d["components"] if c["passed"] is False]
    skipped = [c for c in d["components"] if c["passed"] is None]
    lines = [
        "Piotroski F-Score — nine accounting tests, one point each.",
        f"Result: {len(passed)} passed, {len(failed)} failed, "
        f"{len(skipped)} not computable (a missing input is NOT counted as a "
        f"failure; the score is None below 6 computable tests).",
        "Each test compares this year's statement figure against the stated "
        "comparator — the prior fiscal year, or a fixed zero.",
    ]
    if failed:
        lines.append("Points lost: " + ", ".join(c["rule"] for c in failed) + ".")
    table = [(f"{c['name'].replace('_', ' ')} ({c['rule']})",
              ("n/a — input missing" if c["passed"] is None else
               f"{'PASS' if c['passed'] else 'FAIL'}: "
               f"{_num(c['current'])} vs {_num(c['comparator'])}"))
             for c in d["components"]]
    return "\n".join(lines), table


# ------------------------------------------------------------------- altman
def explain_altman(fund: Optional[Dict[str, Any]]) -> Explanation:
    """'Altman Z 9.25' — the variant, its cutoff, and the weighted terms."""
    fund = fund or {}
    z = fund.get("z")
    variant = fund.get("z_variant") or "original"
    cutoff = fund.get("z_veto_used")
    terms = ((fund.get("evidence") or {}).get("altman") or {}).get("terms") or []
    lines = [
        f"Altman Z-Score ({variant} variant) — bankruptcy-distress score.",
        f"Raw measurement: Z = {_num(z, 2)}.",
        f"Compared against: the distress cutoff for THIS variant, "
        f"{_num(cutoff, 2)} (the cutoffs are not interchangeable: 1.8 for the "
        f"original Z, 1.1 for the adjusted Z''); the grey zone runs to "
        f"{DEF_Z_GREY:.2f}.",
        ("A distress VETO was applied: the composite score is capped."
         if fund.get("veto") else "No distress veto: Z is above the cutoff."),
    ]
    if terms:
        lines.append("Transformation: a weighted sum of five balance-sheet ratios; "
                     "the contributions below add up to Z.")
    else:
        lines.append(f"Per-term breakdown: {NOT_RECORDED}.")
    table = [(f"{t['name']} — {t['label']}",
              f"{_num(t['ratio'])} x {t['coefficient']:g} = {_num(t['contribution'])}")
             for t in terms]
    table.append(("Z-Score", _num(z, 4)))
    table.append((f"Distress cutoff ({variant})", _num(cutoff, 2)))
    return "\n".join(lines), table


# -------------------------------------------------------- FUNDAMENTAL section
_FUND_ALL = ("piotroski", "quality", "value", "growth")


def explain_fundamental_section(fund: Optional[Dict[str, Any]]) -> Explanation:
    """The section score as the visible sum of its weighted parts."""
    fund = fund or {}
    comps: Dict[str, Any] = fund.get("components") or {}
    score = fund.get("score")
    if not comps:
        return ("FUNDAMENTAL section: no fundamental input was computable for this "
                "symbol (no statements, or none recent enough), so the section "
                "carries no score and is dropped from the weighted average "
                "rather than counted as a neutral 0.", [])
    total_w = fund.get("weight_total") or sum(c["weight"] for c in comps.values())
    missing = [n for n in _FUND_ALL if n not in comps]
    lines = [
        "FUNDAMENTAL section — weighted average of the legs that were computable.",
        f"Weights are RENORMALIZED over those legs (divisor = "
        f"{total_w:.2f}), so a missing leg redistributes its weight instead of "
        f"dragging the score toward zero.",
    ]
    if missing:
        lines.append(f"Not computable, therefore excluded: {', '.join(missing)}.")
    lines.append("Contributions (weight / renormalized weight x normalized score):")
    table: List[Tuple[str, str]] = []
    for name, c in comps.items():
        w, norm = c["weight"], c["normalized"]
        contrib = w * norm / total_w if total_w else 0.0
        raw, transform, scale = c.get("raw"), c.get("transform"), c.get("scale")
        lines.append(
            f"  {name}: raw {_signed(raw, 4) if raw is not None else NOT_RECORDED}"
            f" -> {transform or 'n/a'}"
            + (f" (scale {scale:g})" if scale else "")
            + f" -> normalized {_signed(norm, 4)}"
            f"; weight {w:.2f}/{total_w:.2f} x {_signed(norm, 4)} = {contrib:+.4f}")
        table.append((f"Component {name}",
                      f"{w:.2f}/{total_w:.2f} x {norm:+.4f} = {contrib:+.4f}"))
    lines.append(f"Section score = sum of the contributions = "
                 f"{score:+.4f}" if score is not None else "Section score = n/a")
    table.append(("Section score", f"{score:+.4f}" if score is not None else "n/a"))
    if fund.get("veto"):
        lines.append(f"VETO applied ({fund.get('veto_reason')}): the FINAL composite "
                     f"score is capped, regardless of this section's value.")
    return "\n".join(lines), table


# ---------------------------------------------------------- TECHNICAL section
_TECH_LEGS = {
    "momentum_vol_adj": ("12-1 momentum / horizon-scaled realized vol",
                         "scale_momvol", DEF_SCALE_MOMVOL, "tanh(raw / k)"),
    "dist_sma_trend": ("price / SMA(trend) - 1", "scale_d200", DEF_SCALE_D200,
                       "tanh(raw / k)"),
    "rsi_meanrev": ("RSI(14), Wilder", "scale_rsi", DEF_SCALE_RSI,
                    "tanh((50 - RSI) / k)  [inverted: high RSI = negative]"),
    "donchian_breakout": ("position in the trailing Donchian channel", None, None,
                          "used as-is, already in [-1, +1]"),
}


def explain_technical_section(tech: Optional[Dict[str, Any]],
                              settings: Optional[Dict[str, Any]] = None) -> Explanation:
    """The four technical legs with their raw readings and tanh scales."""
    tech = tech or {}
    settings = settings or {}
    comps: Dict[str, Any] = tech.get("components") or {}
    score = tech.get("score")
    if not comps:
        return ("TECHNICAL section: no leg was computable (insufficient price "
                "history).", [])
    adx, trending = tech.get("adx"), tech.get("trending")
    used_w = sum(c["weight"] for c in comps.values() if c.get("raw") is not None)
    lines = [
        "TECHNICAL section — weighted average of four legs, each squashed to "
        "[-1, +1].",
        f"ADX({_num(adx, 1)}) vs the gate {float(settings.get('adx_gate', DEF_ADX_GATE)):.1f}: "
        f"{'TRENDING' if trending else 'NOT trending'}"
        + ("" if trending else
           f" — the RSI mean-reversion leg's weight is multiplied by "
           f"{float(settings.get('adx_rsi_boost', 2.0)):g} and the weights renormalized."),
        f"Weights renormalized over the computable legs (divisor = {used_w:.2f}).",
    ]
    table: List[Tuple[str, str]] = []
    for name, c in comps.items():
        label, skey, sdef, transform = _TECH_LEGS.get(
            name, (name, None, None, "tanh(raw / k)"))
        w, raw, norm = c["weight"], c.get("raw"), c.get("normalized")
        k = float(settings.get(skey, sdef)) if skey else None
        if raw is None:
            lines.append(f"  {name}: not computable — EXCLUDED (its weight is "
                         f"redistributed, it does not score 0).")
            table.append((f"{name} ({label})", "excluded — not computable"))
            continue
        contrib = w * norm / used_w if used_w else 0.0
        lines.append(
            f"  {name}: raw {_signed(raw, 4)} ({label})"
            + (f" -> {transform} with k={k:g}" if k is not None else f" -> {transform}")
            + f" -> {_signed(norm, 4)}; weight {w:.2f}/{used_w:.2f} = {contrib:+.4f}")
        table.append((f"{name} ({label})",
                      f"raw {raw:+.4f}"
                      + (f", k={k:g}" if k is not None else "")
                      + f" -> {norm:+.4f}, weight {w:.2f} = {contrib:+.4f}"))
    lines.append(f"Section score = {score:+.4f}" if score is not None
                 else "Section score = n/a")
    table.append(("Section score", f"{score:+.4f}" if score is not None else "n/a"))
    if tech.get("atr") is not None:
        table.append(("ATR (used for target/stop, not scored)", _num(tech["atr"])))
    return "\n".join(lines), table


# -------------------------------------------------------------- MACRO regime
def explain_macro_section(regime: Optional[Dict[str, Any]]) -> Explanation:
    if not regime:
        return ("MACRO regime: turned off (macro_mode = off), so it neither scores "
                "nor scales exposure.", [])
    comps: Dict[str, Any] = regime.get("components") or {}
    score = regime.get("score")
    if not comps:
        return ("MACRO regime: no macro input resolved, so the composite carries "
                "no weight.", [])
    total_w = sum(c["weight"] for c in comps.values())
    lines = [
        f"MACRO regime — weighted composite of {regime.get('n_inputs')} macro "
        f"inputs, each already normalized to [-1, +1].",
        f"Weights renormalized over the inputs that resolved (divisor = {total_w:.2f}).",
    ]
    table: List[Tuple[str, str]] = []
    for name, c in comps.items():
        w, v = c["weight"], c["value"]
        contrib = w * v / total_w if total_w else 0.0
        lines.append(f"  {name}: {_signed(v, 4)} x weight {w:.2f}/{total_w:.2f} "
                     f"= {contrib:+.4f}")
        table.append((name, f"{v:+.4f} x {w:.2f}/{total_w:.2f} = {contrib:+.4f}"))
    lines.append(f"Regime score = {score:+.4f}" if score is not None
                 else "Regime score = n/a")
    table.append(("Regime score", f"{score:+.4f}" if score is not None else "n/a"))
    return "\n".join(lines), table
