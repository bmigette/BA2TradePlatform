"""Map StrategyOptimization.fitness_metric -> a scalar from the BACKTEST results dict.

GA is maximize-only (FitnessMax weights=(1.0,)), so max_drawdown is negated.
0-trade configs return a sentinel LARGE-NEGATIVE fitness distinct from the 0.0
exception fallback in GeneticOptimizer.optimize (genetic.py: the evaluate wrapper
returns (0.0,) when fitness_function raises). Keeping the no-trade sentinel
distinct from 0.0 means a no-trade config is never confused with a crashed trial,
and is always worse than any real config.

The results-dict keys are those produced by the Phase-2 daily backtest runner
(results.build_results / _compute_metrics) which are byte-compatible with
_convert_bt_results: total_trades, sharpe_ratio, total_return, profit_factor,
win_rate, sortino_ratio, calmar_ratio, sqn, max_drawdown (all confirmed present).
"""
import math
from datetime import date, datetime

# Distinct from 0.0 (the exception fallback) so a no-trade config is never
# confused with a crashed trial, and is always worse than any real config.
ZERO_TRADE_SENTINEL = -1.0e9

# consistent_annual_return trade-floor sentinel: a config trading BELOW the 30/yr floor is
# disqualified, but with a value distinct from ZERO_TRADE_SENTINEL so a below-floor config is
# distinguishable from a no-trade config in logs/all_results, and (deliberately) ranks ABOVE a
# no-trade config — "trades too little" is less broken than "never trades". Both are always
# worse than any real (non-disqualified) fitness.
LOW_TRADE_SENTINEL = -1.0e8

# --- consistent_annual_return metric constants -------------------------------------------------
# Goal: ~30% return EVERY year — not 50% one year / 10% the next.
_CAR_MIN_TRADES_PER_YEAR = 30.0   # hard gate: below this the config is disqualified
_CAR_DD_SOFT_CAP = 20.0           # % drawdown tolerated at full credit; beyond it, soft penalty
_CAR_CONSISTENCY_FLOOR = 0.25     # worst_year/mean_year clamp lower bound
_CAR_PARTIAL_YEAR_MIN_DAYS = 182.62  # ~6 months: shorter partial start/end years merge into neighbor
_CAR_ALIASES = ("consistent_annual_return", "car", "goal")

# fitness_metric (lower-cased) -> results-dict key. max_drawdown is handled
# specially (negated) and is therefore NOT in this map.
_FITNESS_KEYS = {
    "sharpe": "sharpe_ratio",
    "sharpe_ratio": "sharpe_ratio",
    "return": "total_return",
    "total_return": "total_return",
    "profit_factor": "profit_factor",
    "win_rate": "win_rate",
    "sortino": "sortino_ratio",
    "sortino_ratio": "sortino_ratio",
    "calmar": "calmar_ratio",
    "calmar_ratio": "calmar_ratio",
    "sqn": "sqn",
    # max_drawdown handled specially (negated)
}


def compute_fitness(fitness_metric: str, results: dict) -> float:
    """Return the scalar fitness for a metric from a backtest results dict.

    - None results or 0-trade runs return ZERO_TRADE_SENTINEL (distinct from 0.0).
    - max_drawdown/max_dd/drawdown is NEGATED (smaller drawdown -> larger fitness).
    - NaN/inf metric values collapse to ZERO_TRADE_SENTINEL (degenerate trial).
    - An unknown fitness_metric raises ValueError (no-defaults, fail-early).
    """
    if results is None:
        return ZERO_TRADE_SENTINEL
    if int(results.get("total_trades", 0) or 0) == 0:
        return ZERO_TRADE_SENTINEL

    metric = fitness_metric.lower()
    if metric in ("max_drawdown", "max_dd", "drawdown"):
        dd = results.get("max_drawdown")
        if dd is None:
            return ZERO_TRADE_SENTINEL
        return -float(dd)  # smaller drawdown -> larger (less negative) fitness

    if metric in _CAR_ALIASES:
        # Early return ON PURPOSE: the trade-frequency scale block below must NOT apply to this
        # metric — its linear ramp-to-100/yr would penalize the 30-40 trades/yr target zone ~3x,
        # and the hard >=30/yr gate inside the metric already replaces it. fitness_trade_scale
        # is therefore a structural no-op for consistent_annual_return.
        return _consistent_annual_return(results)

    key = _FITNESS_KEYS.get(metric)
    if key is None:
        raise ValueError(
            f"Unknown fitness_metric: {fitness_metric!r}. "
            f"Valid: {sorted(set(_FITNESS_KEYS) | {'max_drawdown'} | set(_CAR_ALIASES))}"
        )
    # Profit-cap-aware: when EITHER cap was applied (per-trade basis cap ``profit_cap_pct`` or
    # portfolio-share cap ``profit_share_cap_pct``), the GA must rank on the ADJUSTED return-based
    # metric so one lucky, non-reproducible mega-winner (or one trade dominating total return) can't
    # win the search. Only return-based metrics have an adjusted variant; the rest fall back to raw.
    if results.get("profit_cap_pct") or results.get("profit_share_cap_pct"):
        adj_key = {"calmar_ratio": "adjusted_calmar_ratio",
                   "total_return": "adjusted_total_return",
                   "profit_factor": "adjusted_profit_factor",
                   "sqn": "adjusted_sqn"}.get(key)
        if adj_key is not None and results.get(adj_key) is not None:
            key = adj_key
    # NOTE: sharpe_ratio / sortino_ratio have no adjusted variant yet (they need an adjusted equity
    # curve, not just capped trade pnls), so they fall back to raw even under a cap.
    val = results.get(key)
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return ZERO_TRADE_SENTINEL
    val = float(val)
    # Optional TRADE-FREQUENCY scale (``fitness_trade_scale``): multiply the fitness by
    # min(avg_trades_per_year, cap) / 100, so a statistically thin config (few trades over the run)
    # is down-weighted. ~100 trades/yr is the break-even (factor 1.0); a 16-trade/3yr config (~5/yr)
    # is scaled x0.05, crushing lottery winners. The CAP (``fitness_trade_scale_cap``, default 100)
    # clamps avg_trades_per_year BEFORE scaling so the factor stops growing above it — the GA is
    # therefore NOT rewarded for over-trading (a scalper aiming for the multiplier). With the
    # default cap=100 the factor maxes at 1.0 (a pure thinness penalty); a higher cap allows some
    # up-weighting up to that rate. Applied only to a POSITIVE fitness — scaling a losing (<=0)
    # fitness toward 0 would wrongly FAVOUR a thin loser, so those are left unchanged.
    if results.get("fitness_trade_scale") and val > 0:
        cap = float(results.get("fitness_trade_scale_cap") or 100.0)
        tpy = results.get("avg_trades_per_year") or 0.0
        val *= min(float(tpy), cap) / 100.0
    return val


# ---------------------------------------------------------------------------
# consistent_annual_return ("car" / "goal")
# ---------------------------------------------------------------------------
def _consistent_annual_return(results: dict) -> float:
    """Fitness aligned with the trading goal: ~30%/yr EVERY year, >=30 trades/yr, dd <= 20% ok.

    fitness = base x dd_guard x consistency, where:

    * base — ``adjusted_annualized_return`` when a profit cap is active (same adjusted-metric
      switch as the other return metrics: a lucky mega-winner must not win the search), else
      ``annualized_return``. Both are %/yr.
    * trade gate — ``avg_trades_per_year`` must be >= 30 or the config is DISQUALIFIED with
      ``LOW_TRADE_SENTINEL`` (distinct from ZERO_TRADE_SENTINEL; see its comment). When the key
      is absent, derived as total_trades / calendar-years spanned by the equity curve; if that
      too is underivable the config is disqualified (no hidden defaults).
    * dd_guard — 1.0 while |max_drawdown| <= 20%; beyond that 20/|dd| (soft penalty, e.g.
      -30% dd -> x0.667), because up to 20% drawdown is explicitly acceptable.
    * consistency — clamp(worst_year / mean_year, 0.25, 1.0) over CALENDAR-YEAR returns from
      the equity curve. Equal years (30, 30, 30) -> 1.0 (no penalty); an uneven (50, 10, 50)
      -> 10 / 36.67 = 0.27 (a 50/10 profile is worth ~a quarter of its headline return); a
      negative year while the mean is positive drives worst/mean negative -> clamps to the
      0.25 floor (penalized hard, but the return signal still orders configs). When
      mean_year <= 0 the factor is 1.0 — the low/negative base already sinks the config, and
      scaling a negative would wrongly reward inconsistency. Partial calendar years at the
      run's start/end shorter than ~6 months are merged into their neighbor year so a 2-week
      stub can't fake a "bad year".

    A NEGATIVE base is returned unfactored: multiplying a negative by <1.0 factors would
    IMPROVE a losing config, flipping the penalty's sign.

    NOTE: the external ``fitness_trade_scale`` multiplier is intentionally NOT applied to this
    metric (compute_fitness returns before that block) — the hard 30/yr gate replaces it.
    """
    # --- base: (adjusted) annualized return, %/yr ---------------------------------------------
    if results.get("profit_cap_pct") or results.get("profit_share_cap_pct"):
        base = results.get("adjusted_annualized_return")
        if base is None:
            base = results.get("annualized_return")
    else:
        base = results.get("annualized_return")
    if base is None or (isinstance(base, float) and (math.isnan(base) or math.isinf(base))):
        return ZERO_TRADE_SENTINEL
    base = float(base)

    # --- trade gate: >= 30 trades/yr or disqualified -------------------------------------------
    tpy = results.get("avg_trades_per_year")
    if tpy is None:
        years = _years_spanned_by_curve(results.get("equity_curve"))
        total = int(results.get("total_trades", 0) or 0)
        tpy = (total / years) if years > 0 else None
    if tpy is None or float(tpy) < _CAR_MIN_TRADES_PER_YEAR:
        return LOW_TRADE_SENTINEL

    if base <= 0:
        return base  # unfactored: penalty factors on a negative would flip its sign

    # --- drawdown guard -------------------------------------------------------------------------
    dd = abs(float(results.get("max_drawdown") or 0.0))
    dd_guard = 1.0 if dd <= _CAR_DD_SOFT_CAP else _CAR_DD_SOFT_CAP / dd

    # --- yearly consistency ----------------------------------------------------------------------
    consistency = _consistency_factor(_calendar_year_returns(results.get("equity_curve")))
    return base * dd_guard * consistency


def _consistency_factor(year_returns: list) -> float:
    """clamp(worst_year / mean_year, 0.25, 1.0); 1.0 when mean <= 0 or < 2 measurable years."""
    if len(year_returns) < 2:
        return 1.0  # a single (or unmeasurable) year carries no consistency information
    mean = sum(year_returns) / len(year_returns)
    if mean <= 0:
        return 1.0  # the low base already sinks it; don't reward inconsistency on negatives
    worst = min(year_returns)
    return min(max(worst / mean, _CAR_CONSISTENCY_FLOOR), 1.0)


def _calendar_year_returns(equity_curve) -> list:
    """Per-calendar-year returns (%) from an equity curve of ``{date, equity}`` points.

    Year boundaries are calendar (Dec 31 close -> Dec 31 close, measured at the LAST equity
    point of each year). A partial year at the start/end of the run counts as its own year only
    if it spans >= ~6 months; shorter stubs are merged into their neighbor year.
    """
    pts = []
    for p in equity_curve or []:
        d = _parse_dt(p.get("date"))
        e = p.get("equity")
        if d is None or e is None:
            continue
        e = float(e)
        if e <= 0 or math.isnan(e) or math.isinf(e):
            continue
        pts.append((d, e))
    if len(pts) < 2:
        return []

    # Anchor points: the first point (opening) + the last point of every calendar year.
    anchors = [pts[0]]
    for i in range(1, len(pts)):
        if pts[i][0].year != pts[i - 1][0].year:
            anchors.append(pts[i - 1])  # close of the year that just ended
    anchors.append(pts[-1])
    anchors = [a for i, a in enumerate(anchors) if i == 0 or a[0] != anchors[i - 1][0]]
    if len(anchors) < 2:
        return []

    # Segments between consecutive anchors: middle segments are exact calendar years; the first
    # and last may be partial. [start_dt, end_dt, start_eq, end_eq]
    segs = [[anchors[i - 1][0], anchors[i][0], anchors[i - 1][1], anchors[i][1]]
            for i in range(1, len(anchors))]
    min_secs = _CAR_PARTIAL_YEAR_MIN_DAYS * 86400.0
    # Merge a <6-month partial FIRST year into the following year...
    if len(segs) >= 2 and (segs[0][1] - segs[0][0]).total_seconds() < min_secs:
        segs[1][0], segs[1][2] = segs[0][0], segs[0][2]
        segs.pop(0)
    # ...and a <6-month partial LAST year into the preceding year.
    if len(segs) >= 2 and (segs[-1][1] - segs[-1][0]).total_seconds() < min_secs:
        segs[-2][1], segs[-2][3] = segs[-1][1], segs[-1][3]
        segs.pop()
    return [(e_eq / s_eq - 1.0) * 100.0 for _s_dt, _e_dt, s_eq, e_eq in segs]


def _years_spanned_by_curve(equity_curve) -> float:
    """Calendar years between the first and last equity-curve timestamps (0.0 if unknown)."""
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    first = _parse_dt(equity_curve[0].get("date"))
    last = _parse_dt(equity_curve[-1].get("date"))
    if first is None or last is None:
        return 0.0
    secs = (last - first).total_seconds()
    return secs / (365.25 * 86400.0) if secs > 0 else 0.0


def _parse_dt(value):
    """ISO string / date / datetime -> datetime (None when unparseable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
