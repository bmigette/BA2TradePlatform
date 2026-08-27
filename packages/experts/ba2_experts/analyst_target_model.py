"""Fundamentals-only price-target estimator, for experts with NO real analyst-target mechanism.

FMPRating already prices off REAL FMP consensus price targets and doesn't need this. This module
is for experts whose expected-profit/target-price is currently either a flat constant
(FMPInsiderClusterBuy: ``expected_profit_percent`` is just a setting, never computed), a heuristic
divorced from valuation (FMPEarningsDrift: scales with EPS-surprise magnitude only), or unrelated
to fundamentals entirely (DeterministicScorer: ``atr_target_price`` is pure price-volatility, ±k
* ATR). This is an OPT-IN fallback each of those experts wires in behind its own setting, default
OFF everywhere.

METHODOLOGY (validated in ``test_files/probe_analyst_price_target_model.py`` against the REAL FMP
consensus reconstruction — 30 large-cap names, 8 quarterly as-of snapshots over ~2 years; see that
file for the full write-up):

    target = anchor_PE * following_period_EPS_estimate
    anchor_PE = price / anchor_EPS

  method="forward" (default): anchor_EPS = the nearest-FY consensus estimate, following =
    the FY after that. Anchoring the multiple on a consensus ESTIMATE rather than a raw GAAP
    actual keeps it sane even when trailing reported earnings are noisy (a one-off charge, a
    cyclical trough). Validated: +0.1% mean signed error (effectively unbiased) vs "trailing"'s
    persistent -10.5% low bias, on the clean (non-degenerate) subset of the validation run.
  method="trailing": anchor_EPS = trailing TTM (sum of the 4 most recently REPORTED quarters).
    Slightly lower MAE / worst-case in the validation run, at the cost of a systematic low bias
    and needing no forward-estimate coverage beyond one period out.

  EITHER anchor is REJECTED (the whole estimate returns None) when the implied anchor P/E
  exceeds ``max_anchor_pe``: a P/E built on a near-zero EPS is a division artifact, not a
  valuation. Validated failure mode: VRTX priced at a 905x trailing P/E, a $16,727 "target"
  against a real consensus of $486 -- the exact near-zero-denominator blowup already fixed in
  FMPEarningsDrift's own expected-profit cap this session. Real analysts switch to a different
  valuation method (EV/EBITDA, EV/Sales, DCF) rather than quote a triple-digit P/E; this module's
  only "switch" is to decline to answer.

HERMETIC IN BACKTEST, LIVE-FETCHES IN LIVE -- for free, not by anything in this module: both
fetches in ``fetch_estimator_inputs`` go through ``FMPCompanyDetailsProvider.get_past_earnings``/
``get_earnings_estimates``, which route through ``fmp_history_disk_cached`` -- gated by the SAME
thread-local ``frozen_ttl_cache()`` / ``hermetic_fmp_history()`` flags
``daily_backtest_handler.py`` already sets globally for the whole backtest run (a missing
per-symbol history raises ``FMPHistoryCacheMiss`` instead of silently fetching mid-run; live
analysis, where those flags are never set, always hits the live API). This module does not, and
must not, set up its own caching layer -- doing so would silently break that contract for every
expert that opts into it.

KNOWN LIMITATION -- do not "fix" by hardcoding symbols or sectors: names mid a demand supercycle
where consensus EPS ESTIMATES are being revised up faster than analyst PRICE TARGETS follow
(AMD/AMAT/MU during the 2025-26 AI/memory cycle in the validation run) persistently overshoot
under EITHER method, well beyond what the P/E cap alone catches -- not a numerical artifact, a
real estimate-revision-vs-target-revision lag. There is no general fix for this here; it is a
real accuracy ceiling for a constant-multiple methodology during any sector re-rating episode.
Callers that need tighter accuracy than ~20% on ~70% of names should not lean on this alone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ba2_common.core.failure_modes import absorb_if_benign
from ba2_common.logger import logger

#: A P/E anchored on a near-zero EPS is not a valuation (see module docstring). 80x is already
#: generous -- genuine hypergrowth multiples (NVDA/TSLA-grade) sit well under this; past it the
#: anchor EPS itself is degenerate, not the stock's actual valuation regime.
DEFAULT_MAX_ANCHOR_PE = 80.0

#: Independent of the anchor-P/E rejection above: even a SANE anchor can compound with a large
#: following-period growth estimate into an unrealistic multi-bagger "target" (the AI-supercycle
#: cohort in the validation run overshot by 100%+ this way, not via a degenerate anchor). Cap the
#: OUTPUT at a max 2x price by default, mirroring FMPEarningsDrift's own expected-profit cap
#: (added this session after a live 3977% blowup) -- a ceiling on the answer, not a rejection of
#: the computation.
DEFAULT_MAX_EXPECTED_PROFIT_PERCENT = 100.0

VALID_METHODS = ("forward", "trailing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# I/O (Phase 1 _gather half) -- mirrors DeterministicScorer/data.py's fetch_past_earnings /
# fetch_statements conventions: absorb_if_benign, empty-on-failure so a data gap degrades this
# estimator to "not computable" rather than aborting the caller's whole analysis.
# --------------------------------------------------------------------------- #
def fetch_estimator_inputs(providers: Any, symbol: str,
                           as_of: Optional[datetime]) -> Dict[str, List[dict]]:
    """Trailing quarterly earnings + forward EPS estimates, point-in-time.

    ``providers`` is a ProviderBundle (``providers.fundamentals_details()`` -> a
    CompanyFundamentalsDetailsInterface, e.g. FMPCompanyDetailsProvider). Returns
    ``{"earnings": [...], "estimates": [...]}``; either list is empty on a fetch failure or
    genuine data gap, never raises for that case (a hermetic/defect error still propagates via
    ``absorb_if_benign``).
    """
    ref = as_of if as_of is not None else _utcnow()
    det = providers.fundamentals_details()

    try:
        past = det.get_past_earnings(symbol=symbol, frequency="quarterly", end_date=ref,
                                     lookback_periods=4, format_type="dict")
        earnings = past.get("earnings", []) if isinstance(past, dict) else []
    except Exception as e:  # noqa: BLE001 - hermetic/defect errors re-raise via absorb_if_benign
        absorb_if_benign(e)
        logger.warning("analyst_target_model: past-earnings fetch failed for %s: %s", symbol, e)
        earnings = []

    try:
        # "quarterly" is the CACHE NAMESPACE, not a request for quarterly rows -- FMP's
        # analyst-estimates endpoint always returns ANNUAL rows regardless of what's asked for,
        # and every history already on disk in this repo was warmed under the "quarterly"
        # namespace (see FMPCompanyDetailsProvider.get_earnings_estimates's own docstring:
        # "Verified against the 4,695 cached earnings_estimates_quarterly__*.json payloads").
        # Passing "annual" here hits an empty namespace and looks like a cache miss even when
        # the (truly annual) data is sitting right there under "quarterly".
        est = det.get_earnings_estimates(symbol=symbol, frequency="quarterly", as_of_date=ref,
                                         lookback_periods=2, format_type="dict")
        estimates = est.get("estimates", []) if isinstance(est, dict) else []
    except Exception as e:  # noqa: BLE001 - hermetic/defect errors re-raise via absorb_if_benign
        absorb_if_benign(e)
        logger.warning("analyst_target_model: earnings-estimates fetch failed for %s: %s",
                       symbol, e)
        estimates = []

    return {"earnings": earnings, "estimates": estimates}


# --------------------------------------------------------------------------- #
# pure (Phase 1 _process half)
# --------------------------------------------------------------------------- #
def estimate_price_target(inputs: Dict[str, List[dict]], current_price: Optional[float], *,
                          method: str = "forward",
                          max_anchor_pe: float = DEFAULT_MAX_ANCHOR_PE,
                          max_expected_profit_percent: float = DEFAULT_MAX_EXPECTED_PROFIT_PERCENT
                          ) -> Optional[Dict[str, Any]]:
    """Model a price target from already-fetched fundamentals. See module docstring for
    methodology, validation numbers, and the known supercycle-cohort limitation.

    Returns ``None`` whenever the inputs don't support a sane estimate (insufficient earnings/
    estimate history, non-positive EPS, or an anchor P/E past ``max_anchor_pe``) -- callers MUST
    treat that exactly like "no analyst coverage available", not an error, and fall back to
    whatever this expert did before this estimator existed.

    ``max_expected_profit_percent`` (default 100%, i.e. target capped at 2x price) is a CEILING
    on the answer, applied AFTER a sane anchor was found -- distinct from ``max_anchor_pe``,
    which rejects the computation outright. A sane anchor can still compound with a large
    following-period growth estimate into an unrealistic multi-bagger target (the validation
    run's AI-supercycle cohort overshot exactly this way). Both ``target_price`` and
    ``expected_profit_percent`` are capped together so they stay mutually consistent; pass
    ``float("inf")`` to disable.

    ``expected_profit_percent`` in the return is BUY-oriented (``(target/price - 1) * 100``).
    A caller that also supports SELL/short (DeterministicScorer) should compute its own
    directional figure from ``target_price`` instead of using this field.
    """
    if not current_price or current_price <= 0:
        return None
    if method not in VALID_METHODS:
        raise ValueError(f"method must be one of {VALID_METHODS}, got {method!r}")

    earnings = inputs.get("earnings") or []
    estimates = inputs.get("estimates") or []  # ascending by fiscal_date_ending

    if method == "trailing":
        if len(earnings) < 4:
            return None
        anchor_eps = sum(e["reported_eps"] for e in earnings[:4])
        if not estimates:
            return None
        following_eps = estimates[0]["estimated_eps_avg"]
    else:  # "forward"
        if len(estimates) < 2:
            return None
        anchor_eps = estimates[0]["estimated_eps_avg"]
        following_eps = estimates[1]["estimated_eps_avg"]

    if anchor_eps is None or anchor_eps <= 0 or following_eps is None or following_eps <= 0:
        return None

    anchor_pe = current_price / anchor_eps
    if anchor_pe > max_anchor_pe:
        return None

    target = anchor_pe * following_eps
    expected_profit_percent = (target / current_price - 1.0) * 100.0
    if expected_profit_percent > max_expected_profit_percent:
        expected_profit_percent = max_expected_profit_percent
        target = current_price * (1.0 + max_expected_profit_percent / 100.0)

    return {
        "target_price": round(target, 4),
        "expected_profit_percent": round(expected_profit_percent, 2),
        "anchor_pe": round(anchor_pe, 2),
        "anchor_eps": round(anchor_eps, 4),
        "following_eps": round(following_eps, 4),
        "method": method,
    }
