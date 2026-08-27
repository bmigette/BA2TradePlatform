"""Ad-hoc probe: can a simple, reproducible EPS-multiple model reconstruct REAL analyst
consensus price targets from data we ALREADY have cached?

TWO MODELS, head to head:

  v1 TRAILING (the original probe -- kept as the baseline to measure against):
      model_target(as_of) = trailing_PE(as_of) * forward_next_FY_EPS_estimate(as_of)
      trailing_PE(as_of)  = trailing_price(as_of) / trailing_EPS_ttm(as_of)
      trailing_EPS_ttm    = sum of the 4 most recently REPORTED quarterly EPS (GAAP, as-reported)

      FLAW, found by running it: a company with a depressed/near-zero TRAILING GAAP EPS (a
      one-off charge, a cyclical trough) makes trailing_PE explode -- VRTX 2025-02-01 priced a
      target at $16,727 (905x trailing P/E) against a real consensus of $486. That is a
      denominator artifact of the model, nothing to do with the stock or a missing catalyst --
      the SAME near-zero-denominator failure mode already fixed this session in
      FMPEarningsDrift's expected-profit cap.

  v2 FORWARD-FORWARD (the fix):
      model_target(as_of) = forward_PE(as_of) * next_next_FY_EPS_estimate(as_of)
      forward_PE(as_of)   = trailing_price(as_of) / nearest_FY_EPS_estimate(as_of)

      Anchors the multiple on a consensus ESTIMATE instead of a raw GAAP actual. Analyst
      estimates are already largely one-off-adjusted / non-GAAP, so this multiple stays sane
      even when trailing reported earnings are noisy -- and it mirrors how sell-side desks
      actually roll a valuation forward (NTM P/E, not trailing P/E, is the standard multiple;
      see the price-target-methodology sources in the earlier probe's report).

GROUND TRUTH (both models): FMPRating._consensus_target_as_of() -- the SAME no-lookahead
reconstruction the backtest engine itself uses: average of individual analyst price-target rows
whose publishedDate falls within `window_days` on/before as_of.

HERMETIC: everything goes through hermetic_fmp_history() + frozen_ttl_cache(), so a genuine
cache miss raises FMPHistoryCacheMiss instead of silently hitting the live FMP API. This is a
pure "what can we already reconstruct" probe, not a data-collection run.

Usage:
    .venv/Scripts/python.exe test_files/probe_analyst_price_target_model.py
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone

from ba2_common.core.db import configure_db
from ba2_common.core.native_cache import find_timeseries_path
from ba2_providers.fmp_common import FMPHistoryCacheMiss, frozen_ttl_cache, hermetic_fmp_history
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import FMPCompanyDetailsProvider
from ba2_experts.FMPRating import FMPRating, fetch_price_target_history_cached

# The default DB_FILE resolves to BA2_HOME/db.sqlite, which has no FMP_API_KEY configured -- the
# real dev trade DB (where the key + everything else this session has touched actually lives) is
# one level down, per the "Dev/Prod environments" project memory.
configure_db(r"C:\Users\basti\Documents\ba2\trade\db.sqlite")

# The 30-symbol large-cap universe already driving this session's DeterministicScorer/FactorRanker
# goal2020 grid runs -- picked because its FMP histories are certainly warm, not cherry-picked for
# result quality.
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "COST", "NFLX",
    "AMD", "PEP", "ADBE", "CSCO", "TMUS", "INTC", "QCOM", "INTU", "AMAT", "TXN",
    "AMGN", "ISRG", "BKNG", "HON", "VRTX", "ADP", "SBUX", "GILD", "MU", "LRCX",
]

_ANCHOR = datetime(2026, 8, 1, tzinfo=timezone.utc)  # _consensus_target_as_of compares against
# tz-aware parsed publishedDate values (parse_provider_date), so as_of must be tz-aware too.
AS_OF_DATES = [_ANCHOR - timedelta(days=91 * k) for k in range(8)]  # ~2 years, quarterly
CONSENSUS_WINDOW_DAYS = 90
MIN_TARGETS_BEHIND_CONSENSUS = 3
# A P/E multiple anchored on a near-zero EPS is not a valuation, it's a division artifact -- real
# analysts switch to EV/EBITDA, EV/Sales or DCF for such names rather than quote a 900x P/E. 80x
# is already generous (NVDA/TSLA-grade hypergrowth multiples sit well under this); anything past
# it means the anchor EPS itself is degenerate, not that the stock is genuinely priced there.
MAX_SANE_ANCHOR_PE = 80.0
_OHLCV_PROVIDER_DIRS = ("FMPOHLCVProvider", "AlpacaOHLCVProvider",
                        "YFinanceDataProvider", "AlphaVantageOHLCVProvider")


def _trailing_price(symbol: str, as_of: datetime) -> float | None:
    """Last daily close on/before as_of, from whichever local OHLCV parquet cache has it."""
    import pandas as pd

    as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
    for provider_dir in _OHLCV_PROVIDER_DIRS:
        path = find_timeseries_path(provider_dir, symbol, "1d")
        if not path:
            continue
        df = pd.read_parquet(path)
        col = "Close" if "Close" in df.columns else "close"
        dcol = "Date" if "Date" in df.columns else "date"
        d = pd.to_datetime(df[dcol]).dt.tz_localize(None)
        sel = df[d <= pd.Timestamp(as_of_naive)]
        if sel.empty:
            continue
        idx = d[d <= pd.Timestamp(as_of_naive)].idxmax()
        return float(df.loc[idx, col])
    return None


def _row(provider: FMPCompanyDetailsProvider, symbol: str, as_of: datetime) -> dict | None:
    price = _trailing_price(symbol, as_of)
    if price is None:
        return None

    # -- ground truth: no-lookahead reconstructed consensus (needed regardless of which
    # model(s) end up computable, so fetch it first and bail early if there's nothing to score
    # against) --
    pt_history = fetch_price_target_history_cached(provider.api_key, symbol)
    consensus = FMPRating._consensus_target_as_of(pt_history, as_of, CONSENSUS_WINDOW_DAYS)
    if consensus is None:
        return None
    n_targets = consensus.get("targetCount")
    if n_targets is None:
        # _consensus_target_as_of doesn't itself return a count in every version; derive one.
        from datetime import timedelta as _td
        floor = as_of - _td(days=CONSENSUS_WINDOW_DAYS)
        n_targets = sum(1 for r in pt_history
                        if r.get("priceTarget") is not None
                        and (d := r.get("publishedDate"))
                        and floor.isoformat() <= str(d)[:10] <= as_of.isoformat()[:10])
    if n_targets < MIN_TARGETS_BEHIND_CONSENSUS:
        return None  # degenerate thin consensus -- same guard FMPRating itself uses
    actual = consensus["targetConsensus"]

    out = {
        "symbol": symbol, "as_of": as_of.date().isoformat(), "price": round(price, 2),
        "actual_consensus": round(actual, 2), "n_targets": n_targets,
    }

    # -- v1 TRAILING: trailing GAAP P/E * next-FY forward EPS estimate --
    past = provider.get_past_earnings(symbol, "quarterly", as_of, lookback_periods=4,
                                      format_type="dict")
    earnings = past.get("earnings", [])
    v1_target = None
    if len(earnings) == 4:
        trailing_eps = sum(e["reported_eps"] for e in earnings)
        surprise_pct = earnings[0].get("surprise_percent")
        out["surprise_pct"] = round(surprise_pct, 1) if surprise_pct is not None else None
    else:
        trailing_eps = None
        out["surprise_pct"] = None

    # NB: get_earnings_estimates's own docstring warns "annual" never reaches FMP as a request
    # param -- it only selects the cache NAMESPACE, and every disk-cached history in this repo
    # was built under the "quarterly" namespace (see FMPCompanyDetailsProvider.get_earnings_
    # estimates's docstring: "Verified against the 4,695 cached earnings_estimates_quarterly__*
    # .json payloads"). Passing "annual" here hits an empty namespace and looks like a cache
    # miss even when the (truly annual) data is sitting right there under "quarterly".
    est = provider.get_earnings_estimates(symbol, "quarterly", as_of, lookback_periods=2,
                                          format_type="dict")
    fwd_rows = est.get("estimates", [])  # ascending by fiscal_date_ending

    if trailing_eps is not None and trailing_eps > 0 and fwd_rows:
        fy_next = fwd_rows[0]["estimated_eps_avg"]
        trailing_pe = price / trailing_eps
        if fy_next > 0 and trailing_pe <= MAX_SANE_ANCHOR_PE:
            v1_target = trailing_pe * fy_next
            out["trailing_pe"] = round(trailing_pe, 1)
    if v1_target is not None:
        out["v1_target"] = round(v1_target, 2)
        out["v1_error_pct"] = round((v1_target - actual) / actual * 100.0, 1)

    # -- v2 FORWARD-FORWARD: forward P/E (price / nearest-FY estimate) * following-FY estimate --
    v2_target = None
    if len(fwd_rows) >= 2:
        fy0, fy1 = fwd_rows[0]["estimated_eps_avg"], fwd_rows[1]["estimated_eps_avg"]
        if fy0 > 0:
            forward_pe = price / fy0
            if fy1 > 0 and forward_pe <= MAX_SANE_ANCHOR_PE:
                v2_target = forward_pe * fy1
                out["forward_pe"] = round(forward_pe, 1)
    if v2_target is not None:
        out["v2_target"] = round(v2_target, 2)
        out["v2_error_pct"] = round((v2_target - actual) / actual * 100.0, 1)

    if v1_target is None and v2_target is None:
        return None  # neither model was computable for this (symbol, as_of) -- skip
    return out


def main() -> None:
    provider = FMPCompanyDetailsProvider()
    rows: list[dict] = []
    misses: list[str] = []
    skipped: list[str] = []

    with frozen_ttl_cache(), hermetic_fmp_history():
        for symbol in UNIVERSE:
            for as_of in AS_OF_DATES:
                try:
                    r = _row(provider, symbol, as_of)
                except FMPHistoryCacheMiss as e:
                    misses.append(f"{symbol} {as_of.date()}: {e}")
                    continue
                if r is None:
                    skipped.append(f"{symbol} {as_of.date()}")
                    continue
                rows.append(r)

    print(f"\n{len(rows)} usable (symbol, as_of) pairs; "
          f"{len(skipped)} skipped (insufficient data), {len(misses)} cache miss(es)")
    if misses:
        print(f"  first few cache misses: {misses[:5]}")

    if not rows:
        print("Nothing to analyze -- cache coverage is too thin for this universe/window.")
        return

    print(f"\n{'symbol':<7}{'as_of':<12}{'price':>9}{'v1_target':>11}{'v1_err%':>9}"
          f"{'v2_target':>11}{'v2_err%':>9}{'actual':>10}{'nT':>4}")
    for r in sorted(rows, key=lambda r: (r["symbol"], r["as_of"])):
        v1s = f"{r['v1_target']}" if "v1_target" in r else "-"
        v1e = f"{r['v1_error_pct']}" if "v1_error_pct" in r else "-"
        v2s = f"{r['v2_target']}" if "v2_target" in r else "-"
        v2e = f"{r['v2_error_pct']}" if "v2_error_pct" in r else "-"
        print(f"{r['symbol']:<7}{r['as_of']:<12}{r['price']:>9}{v1s:>11}{v1e:>9}"
              f"{v2s:>11}{v2e:>9}{r['actual_consensus']:>10}{r['n_targets']:>4}")

    def _stats(label: str, key: str, row_set: list = None) -> None:
        errs = [r[key] for r in (row_set if row_set is not None else rows) if key in r]
        if not errs:
            print(f"\n--- {label}: no usable pairs ---")
            return
        abs_errs = [abs(e) for e in errs]
        within_10 = sum(1 for e in abs_errs if e <= 10) / len(abs_errs) * 100
        within_20 = sum(1 for e in abs_errs if e <= 20) / len(abs_errs) * 100
        print(f"\n--- {label} ({len(errs)} pairs) ---")
        print(f"  mean signed error : {statistics.mean(errs):+.1f}%  "
              f"(bias: {'runs HIGH' if statistics.mean(errs) > 0 else 'runs LOW'})")
        print(f"  MAE               : {statistics.mean(abs_errs):.1f}%")
        print(f"  median |error|    : {statistics.median(abs_errs):.1f}%")
        if len(errs) > 1:
            print(f"  stdev signed error: {statistics.stdev(errs):.1f}%")
        print(f"  max |error|       : {max(abs_errs):.1f}%")
        print(f"  within +/-10%     : {within_10:.0f}%")
        print(f"  within +/-20%     : {within_20:.0f}%")

    _stats("v1 TRAILING (trailing GAAP P/E * next-FY estimate)", "v1_error_pct")
    _stats("v2 FORWARD-FORWARD (forward P/E * following-FY estimate)", "v2_error_pct")

    # head-to-head on the pairs where BOTH models produced a target, so the comparison isn't
    # skewed by one model happening to cover an easier/harder subset
    both = [r for r in rows if "v1_error_pct" in r and "v2_error_pct" in r]
    if both:
        v1_abs = [abs(r["v1_error_pct"]) for r in both]
        v2_abs = [abs(r["v2_error_pct"]) for r in both]
        v2_wins = sum(1 for r in both if abs(r["v2_error_pct"]) < abs(r["v1_error_pct"]))
        print(f"\n--- head-to-head on the {len(both)} pairs both models could score ---")
        print(f"  v1 median |error| = {statistics.median(v1_abs):.1f}%   "
              f"v1 MAE = {statistics.mean(v1_abs):.1f}%   v1 max = {max(v1_abs):.1f}%")
        print(f"  v2 median |error| = {statistics.median(v2_abs):.1f}%   "
              f"v2 MAE = {statistics.mean(v2_abs):.1f}%   v2 max = {max(v2_abs):.1f}%")
        print(f"  v2 closer to actual than v1 on {v2_wins}/{len(both)} pairs "
              f"({v2_wins / len(both) * 100:.0f}%)")

    # AMD/AMAT/MU dominate the remaining large errors on BOTH models, and NOT as one-off
    # blowups -- MU alone is >100% off on 7 of its 8 as_of dates. That's an AI-cycle
    # memory/semicap supercycle: consensus EPS ESTIMATES have been revised up faster than
    # analyst PRICE TARGETS have followed, so ANY constant-multiple model overshoots for this
    # cohort specifically -- a sector-momentum/estimate-revision-lag effect, not a numerical
    # artifact like the P/E-anchor blowups the cap above fixed. Segment it out to show what the
    # model's real accuracy looks like OUTSIDE that one violated regime.
    _SUPERCYCLE_COHORT = {"AMD", "AMAT", "MU"}
    clean = [r for r in rows if r["symbol"] not in _SUPERCYCLE_COHORT]
    print(f"\n--- excluding the AMD/AMAT/MU AI-supercycle cohort "
          f"({len(rows) - len(clean)} pairs removed) ---")
    _stats("v1 TRAILING, ex-supercycle", "v1_error_pct", clean)
    _stats("v2 FORWARD-FORWARD, ex-supercycle", "v2_error_pct", clean)


if __name__ == "__main__":
    main()
