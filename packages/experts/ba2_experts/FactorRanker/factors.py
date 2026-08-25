"""Pure factor calculators and combine/rank helpers for FactorRanker.

Every function here is pure (no DB, no network, no broker) so it can be unit
tested directly against known inputs. The expert orchestrates these; data
fetching lives in ``data.py`` and execution in ``portfolio.py``.


Unknown is never a value
------------------------
Every calculator here used to answer ``0.0`` when it could not compute anything --
fewer than ``lookback`` closes, no ROE, no earnings-estimate dispersion, a symbol
the fetcher dropped entirely. ``composite_score`` then z-scored that 0.0 across the
universe and averaged it in as though it had been measured.

That is not a conservative default, it is the *best* reading in a falling market:
measured on a 4-name universe of three falling stocks plus one 100-close IPO, the
IPO's fabricated 0.0 momentum z-scored to +1.58, ranked **first of four**, and
``long_only_top_n(top_n=2)`` gave it half the book. No failure was needed to reach
that -- a recent listing suffices. A single unmeasurable member was worse still:
``np.array([1.0, 3.0, None], dtype=float)`` is ``[1, 3, nan]``, so ``sd`` was NaN,
``sd > 0`` was False, and the "no dispersion" branch returned **0.0 for every symbol
in the universe**.

So, following ``ba2_common.core.option_lifecycle``:

* a calculator that cannot measure returns ``None`` -- never 0.0, never NaN;
* ``cross_sectional_zscore`` standardizes against the MEASURED members only and
  answers ``None`` for the rest;
* ``composite_detail`` renormalizes per symbol over the factors that WERE measured
  and records ``n_factors``; a symbol measured on fewer than ``min_factors`` is
  excluded from the ranking instead of being scored on a fraction of the model.

And the inverse error is guarded just as carefully: a factor genuinely measured AT
zero -- a flat 12-1 momentum, a break-even earnings yield, a PEAD reading outside
its drift window -- is a measurement and still scores 0.0.
"""

from typing import Any, Dict, List, Optional, Tuple
import math
import numpy as np
import pandas as pd


#: How many WEIGHTED factors a symbol must have actually been measured on before it
#: is allowed into the ranking (clamped down to the number of weighted factors, so a
#: deliberately single-factor model still works).
#:
#: K = 2, i.e. corroboration. A lone z-score has materially wider dispersion than a
#: blend of several -- averaging shrinks -- so admitting one-factor names
#: systematically stuffs BOTH tails of the ranking with the least-measured names, and
#: a long-only top-N buys the top tail. Renormalizing alone would therefore re-create
#: the very defect it fixes, one step further along. ``macro.DEF_MIN_INPUTS_FOR_RISKOFF``
#: made the same call for the same reason ("a single input can hit exactly -1.0 on its
#: own ... that is not a regime call").
DEF_MIN_MEASURED_FACTORS = 2


def _is_measured(v: Any) -> bool:
    """True for a real, finite number. ``None`` and NaN are both "not measured";
    a NaN that reaches a mean/std silently poisons the whole cross-section."""
    if v is None or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _positive(v: Any) -> bool:
    """True for a measured, strictly positive number (a valid denominator)."""
    return _is_measured(v) and float(v) > 0.0


def _mean_of_measured(terms: List[Optional[float]]) -> Optional[float]:
    """Equal-weight mean over the measured terms, or ``None`` if there are none.

    This is the per-factor equivalent of ``composite_detail``'s renormalization: the
    terms of ``value``/``quality`` are equally-weighted ratios on a comparable scale,
    so a name missing one is scored on the ones it has rather than penalised by a
    fabricated 0. Dividing by the term COUNT (rather than summing) is what makes the
    partially-covered name comparable with the fully-covered one; because a
    cross-sectional z-score is invariant to a positive affine rescale, a universe
    where every name has the same coverage ranks EXACTLY as the old sum did.
    """
    got = [float(t) for t in terms if _is_measured(t)]
    if not got:
        return None
    return sum(got) / len(got)


def momentum_12_1(prices: Dict[str, pd.Series], lookback: int = 252,
                  skip: int = 21) -> Dict[str, Optional[float]]:
    """12-1 month total return: P[-skip] / P[-lookback] - 1.

    Skips the most recent ``skip`` days to avoid short-term reversal. A symbol with
    fewer than ``lookback`` points, or a non-positive start price, is ``None``: the
    return is UNDEFINED there, and the old 0.0 read as "flat" -- the single best
    momentum reading in any falling market. A genuinely flat window still scores 0.0.
    """
    out: Dict[str, Optional[float]] = {}
    for sym, s in prices.items():
        s = s.dropna()
        if len(s) < lookback:
            out[sym] = None
            continue
        p_start = float(s.iloc[-lookback])
        p_end = float(s.iloc[-skip - 1])
        out[sym] = (p_end / p_start - 1.0) if p_start > 0 else None
    return out


def earnings_surprise(data: Dict[str, dict],
                      drift_window_days: int = 60) -> Dict[str, Optional[float]]:
    """Standardized unexpected earnings (SUE), zeroed outside the post-earnings drift window.

    ``data[sym]`` carries ``actual``, ``estimate``, ``estimate_std`` and ``days_since``
    (days since the earnings report). SUE = (actual - estimate) / estimate_std.

    Two different zeros live here and they are NOT the same fact:

    * **outside the drift window** the factor is DEFINED as 0 -- there is no drift
      left to capture. That is a statement about the world, and it stays 0.0.
    * **no report date, no dispersion, no actual/estimate** means SUE is not
      computable. That is a statement about our data, and it is ``None``. It used to
      read as "this company reported exactly in line".
    """
    out: Dict[str, Optional[float]] = {}
    for sym, d in data.items():
        days = d.get("days_since")
        if not _is_measured(days):
            out[sym] = None                      # we cannot place it in or out of the window
            continue
        if float(days) > drift_window_days:
            out[sym] = 0.0                       # measured: the drift window has closed
            continue
        std = d.get("estimate_std")
        if not _positive(std) or not _is_measured(d.get("actual")) \
                or not _is_measured(d.get("estimate")):
            out[sym] = None
            continue
        out[sym] = (float(d["actual"]) - float(d["estimate"])) / float(std)
    return out


def value_score(data: Dict[str, dict]) -> Dict[str, Optional[float]]:
    """Composite value: equal-weight of earnings yield (E/P) and FCF/EV yield.

    Higher = cheaper. A leg whose inputs are missing is DROPPED and the score
    renormalized over the leg(s) that remain (both legs are yields on the same
    scale); a symbol with neither leg is ``None``. The legs used to contribute 0,
    which reads as "this company earns nothing" rather than "we don't know".

    A denominator of 0 (or a non-positive enterprise value, i.e. net cash exceeding
    market cap plus debt) makes the yield uninterpretable, so it is unmeasurable --
    but a NUMERATOR of exactly 0 is a real break-even reading and stays 0.0.
    """
    out: Dict[str, Optional[float]] = {}
    for sym, d in data.items():
        ey = (float(d["eps_ttm"]) / float(d["price"])
              if _is_measured(d.get("eps_ttm")) and _positive(d.get("price")) else None)
        fcfy = (float(d["fcf_ttm"]) / float(d["enterprise_value"])
                if _is_measured(d.get("fcf_ttm")) and _positive(d.get("enterprise_value"))
                else None)
        out[sym] = _mean_of_measured([ey, fcfy])
    return out


def quality_score(data: Dict[str, dict]) -> Dict[str, Optional[float]]:
    """Quality = mean of (ROE, gross profitability, -accruals ratio).

    Higher = more profitable with cleaner (lower-accrual) earnings. The three terms
    are equally-weighted ratios on a comparable scale, so a missing one is DROPPED
    and the score renormalized over the rest; a symbol with no measurable term is
    ``None``. A missing term used to contribute 0, which is a real quality statement
    ("no return on equity") rather than an absence of data -- and ``fetch_quality_inputs``
    routinely leaves ``roe`` or ``accruals_ratio`` as None.
    """
    out: Dict[str, Optional[float]] = {}
    for sym, d in data.items():
        roe = float(d["roe"]) if _is_measured(d.get("roe")) else None
        gp = (float(d["gross_profit"]) / float(d["total_assets"])
              if _is_measured(d.get("gross_profit")) and _positive(d.get("total_assets"))
              else None)
        accr = -float(d["accruals_ratio"]) if _is_measured(d.get("accruals_ratio")) else None
        out[sym] = _mean_of_measured([roe, gp, accr])
    return out


def _winsorized_array(values: Dict[str, Optional[float]], winsorize_pct: float):
    """The array the z-score is ACTUALLY taken over (post-winsorize), plus the symbols
    it covers. Shared by cross_sectional_zscore and cross_sectional_stats so the
    reported comparator can never drift from the one used.

    Only MEASURED members are included. A single ``None`` used to become NaN in the
    float array, which made ``sd`` NaN, which made ``sd > 0`` False, which returned
    0.0 for every symbol in the universe -- one missing name silently flattened the
    whole factor.
    """
    syms = [s for s, v in values.items() if _is_measured(v)]
    arr = np.array([float(values[s]) for s in syms], dtype=float)
    if winsorize_pct > 0 and len(arr) > 2:
        lo, hi = np.quantile(arr, [winsorize_pct, 1 - winsorize_pct])
        arr = np.clip(arr, lo, hi)
    return syms, arr


def cross_sectional_zscore(values: Dict[str, Optional[float]],
                           winsorize_pct: float = 0.0) -> Dict[str, Optional[float]]:
    """Z-score raw factor values across the universe (mean 0, std 1).

    Optionally winsorize the tails at ``winsorize_pct`` before standardizing.
    If the cross-section has zero dispersion, all z-scores are 0 (see
    ``cross_sectional_stats``/``describe_composite_availability``: that 0 means "not
    computable here", and the presentation layer says so).

    A member that was never measured is ``None`` on the way out and is excluded from
    the mean/sd on the way in -- it is not part of the peer group it could not join.
    """
    syms, arr = _winsorized_array(values, winsorize_pct)
    out: Dict[str, Optional[float]] = {s: None for s in values}
    if arr.size == 0:
        return out
    mu, sd = arr.mean(), arr.std()
    z = (arr - mu) / sd if sd > 0 else np.zeros_like(arr)
    for i, s in enumerate(syms):
        out[s] = float(z[i])
    return out


def cross_sectional_stats(values: Dict[str, Optional[float]],
                          winsorize_pct: float = 0.0) -> Dict[str, Any]:
    """The comparator ``cross_sectional_zscore`` measured against, made visible.

    Returns ``n`` / ``mean`` / ``sd`` of the (post-winsorize) cross-section and
    ``degenerate``: True when ``sd == 0``, i.e. the branch where every z-score
    is forced to exactly 0 REGARDLESS of the underlying values. That happens
    for a one-symbol universe by definition, and for any universe whose members
    all carry the same value -- in both cases 0.0 means "not computable here",
    not "average".

    ``n`` counts the MEASURED members only; ``n_unmeasured`` counts the rest, so a
    consumer can tell a 30-name universe that measured 30 from one that measured 3.
    """
    _, arr = _winsorized_array(values, winsorize_pct)
    n_unmeasured = len(values) - int(arr.size)
    if arr.size == 0:
        return {"n": 0, "mean": None, "sd": 0.0, "degenerate": True,
                "n_unmeasured": n_unmeasured, "winsorize_pct": float(winsorize_pct)}
    sd = float(arr.std())
    return {"n": int(arr.size), "mean": float(arr.mean()), "sd": sd,
            "degenerate": not (sd > 0), "n_unmeasured": n_unmeasured,
            "winsorize_pct": float(winsorize_pct)}


def describe_composite_availability(universe_size: int,
                                    factor_stats: Dict[str, Dict[str, Any]],
                                    weights: Dict[str, float]) -> Tuple[bool, Optional[str]]:
    """Is the composite a real measurement here, and if not, why not?

    The composite is a weighted sum of cross-sectional z-scores. When every
    CONTRIBUTING factor's cross-section is degenerate the sum is arithmetically
    pinned to 0.0 and carries no information about the symbol -- reporting that
    as a number invites it to be read as "neutral", which is a different and
    false statement.

    ``factor_stats`` may be empty (a book stored before these stats existed);
    the universe size alone then decides, which is the only structurally
    certain case.
    """
    if universe_size <= 1:
        return False, (
            f"Not computable in this view. The composite is a weighted sum of "
            f"CROSS-SECTIONAL z-scores, which need a peer universe to rank against; "
            f"this card scores exactly 1 symbol, so the standard deviation of the "
            f"cross-section is 0 and every z-score — and therefore the composite — is "
            f"forced to +0.000 whatever the underlying factor values are. "
            f"See the raw per-factor values below for the numbers that were actually "
            f"measured.")
    contributing = [n for n, w in (weights or {}).items() if w] or list(factor_stats or {})
    stats = [factor_stats[n] for n in contributing if n in (factor_stats or {})]
    if stats and all(s.get("degenerate") for s in stats):
        names = ", ".join(n for n in contributing if n in factor_stats)
        return False, (
            f"Not computable in this view. Every weighted factor ({names}) has ZERO "
            f"dispersion across the {universe_size}-symbol universe, so its "
            f"cross-sectional z-score is forced to 0 for every name and the composite "
            f"with it. See the raw per-factor values below.")
    return True, None


def composite_detail(factor_values: Dict[str, Dict[str, Optional[float]]],
                     weights: Dict[str, float], winsorize_pct: float = 0.0,
                     min_factors: int = DEF_MIN_MEASURED_FACTORS) -> Dict[str, Dict[str, Any]]:
    """Per-symbol composite WITH the coverage evidence behind it.

    ``{symbol: {"score", "n_factors", "n_weighted", "min_factors",
    "weight_measured", "weight_total", "detail"}}``, in sorted symbol order.

    ``score`` is the weighted sum of the symbol's MEASURED per-factor z-scores,
    renormalized back up to the full weight total::

        score = Σ_measured(w·z) / Σ_measured(w) × Σ_all(w)

    so a symbol measured on everything scores exactly what the old plain
    ``Σ w·z`` gave it (the renormalization is a no-op), and a symbol measured on
    some of the model is scored on THAT, not on the model with holes filled by
    zeros. This is ``combine_section_scores``' documented "skip" behaviour, one
    expert over.

    ``score`` is ``None`` -- and the symbol therefore absent from ``composite_score``
    and from the ranking -- when fewer than ``min_factors`` weighted factors were
    measured for it. See ``DEF_MIN_MEASURED_FACTORS`` for why that bar exists at all.
    ``min_factors`` is clamped into ``[1, n_weighted]``: a symbol measured on nothing
    is never scored, and a single-factor model is never made impossible.
    """
    weighted = {n: float(w) for n, w in (weights or {}).items()
                if n in factor_values and float(w or 0.0) != 0.0}
    symbols = sorted(set().union(*[set(v) for v in factor_values.values()])) \
        if factor_values else []
    n_weighted = len(weighted)
    k = max(1, min(int(min_factors), n_weighted)) if n_weighted else 1
    weight_total = sum(weighted.values())

    z_by_factor = {n: cross_sectional_zscore(factor_values[n], winsorize_pct)
                   for n in weighted}

    out: Dict[str, Dict[str, Any]] = {}
    for s in symbols:
        contribs = [(w, z_by_factor[n].get(s)) for n, w in weighted.items()]
        measured = [(w, z) for w, z in contribs if z is not None]
        w_measured = sum(w for w, _ in measured)
        row: Dict[str, Any] = {
            "score": None,
            "n_factors": len(measured),
            "n_weighted": n_weighted,
            "min_factors": k,
            "weight_measured": w_measured,
            "weight_total": weight_total,
            "detail": "",
        }
        if len(measured) < k or w_measured <= 0:
            row["detail"] = (
                f"measured on {len(measured)} of {n_weighted} weighted factors "
                f"(needs {k}) — not ranked")
        else:
            row["score"] = sum(w * z for w, z in measured) / w_measured * weight_total
        out[s] = row
    return out


def composite_score(factor_values: Dict[str, Dict[str, Optional[float]]],
                    weights: Dict[str, float], winsorize_pct: float = 0.0,
                    min_factors: int = DEF_MIN_MEASURED_FACTORS) -> Dict[str, float]:
    """Weighted sum of per-factor cross-sectional z-scores, renormalized per symbol
    over the factors that were MEASURED for it.

    ``factor_values`` maps factor name -> {symbol: raw value or None}. A weight of 0
    disables that factor (and it then counts for nothing, including coverage).

    Only the symbols that cleared the coverage bar appear in the result -- an
    unrankable name must be absent from the ranking, not sitting in the middle of it.
    ``composite_detail`` carries the same numbers plus the reason each name was
    dropped. Symbol order is sorted, so the dict's key order (and therefore
    ``rank_symbols``' stable tie-break) is deterministic rather than
    PYTHONHASHSEED-dependent.
    """
    return {s: d["score"]
            for s, d in composite_detail(factor_values, weights, winsorize_pct,
                                         min_factors).items()
            if d["score"] is not None}


def rank_symbols(composite: Dict[str, float]) -> List[str]:
    """Symbols sorted by composite score, highest first.

    Ties are broken DETERMINISTICALLY by symbol (ascending). Without an explicit
    tie-break, ``sorted`` (being stable) preserves the input dict's key order for
    equal scores; that order can be process-dependent, so equal-score names would
    flip places run-to-run and change the top-N cut. Sorting by ``(-score, symbol)``
    makes the ranking — and therefore the held book — bit-stable.
    """
    return sorted(composite, key=lambda s: (-composite[s], s))
