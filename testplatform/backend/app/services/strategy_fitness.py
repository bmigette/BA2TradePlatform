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
import os as _os
from typing import Optional

# Distinct from 0.0 (the exception fallback) so a no-trade config is never
# confused with a crashed trial, and is always worse than any real config.
ZERO_TRADE_SENTINEL = -1.0e9

# consistent_annual_return: returned when avg_trades_per_year is genuinely UNDERIVABLE (no
# avg_trades_per_year key AND no equity-curve-derivable years) — a data problem, not a thin-trading
# config (those are now scored via a proportional ramp, not disqualified; see _consistent_annual_return).
# Distinct from ZERO_TRADE_SENTINEL so it's distinguishable from a no-trade config in logs/all_results,
# and (deliberately) ranks ABOVE a no-trade config. Both are always worse than any real fitness.
LOW_TRADE_SENTINEL = -1.0e8

# A trial whose account hit net_liquidating_value <= 0 (results["account_wiped_out"], set by
# BacktestAccount.snapshot_equity / DailyBacktestEngine.run — the sim stops the moment this
# happens, since real capital can't go negative). Blowing up the account is a WORSE outcome
# than never having traded at all, so this must rank BELOW ZERO_TRADE_SENTINEL, not just
# alongside it.
WIPED_OUT_SENTINEL = -2.0e9

# --- consistent_annual_return metric constants -------------------------------------------------
# Goal: ~30% return EVERY year — not 50% one year / 10% the next.
_CAR_MIN_TRADES_PER_YEAR = 30.0   # trade_gate ramp target: full credit at/above this, linear below
# These two are the DEFAULT trade-frequency objective, applied to every expert that does not
# state its own. An expert whose natural cadence differs can override BOTH per run by declaring
# ``car_min_trades_per_year`` / ``car_hard_min_trades_per_year`` as class attributes; the backtest
# handler resolves them and puts them in ``results``, and the reader below prefers them. That keeps
# a new expert's objective from silently re-scaling every existing result -- changing these two
# constants moves the fitness of EVERY config between the floor and the ramp, so a grid that mixes
# the two scales cannot be ranked against itself.

# HARD trade-frequency floor: below this a config is DISQUALIFIED, not merely scaled down. The
# proportional ramp alone was not enough -- on the mid band a 4.2 trades/yr genome won its job
# outright (17 trades over 4 years), because a 0.14 gate multiplied against a huge low-drawdown
# dd_guard still beat every richer config. A handful of trades cannot evidence an edge whatever
# its ratios, so it is excluded rather than ranked.
_CAR_HARD_MIN_TRADES_PER_YEAR = 12.0

# Ceiling on the drawdown reward. dd_guard is 20/dd, i.e. base x dd_guard IS 20 x Calmar, so an
# unbounded guard lets a tiny-drawdown config buy its way past a materially better one: measured
# at dd 3.86% it collected 5.18x. Capping at 2.0 keeps the full Calmar gradient down to a 10%
# drawdown -- which is where real configs live (the goal2020 large winner sits at 9.1%, barely
# touched) -- and stops paying for drawdowns below that, where the number is usually thinness
# rather than skill.
_CAR_DD_GUARD_MAX = 2.0
_CAR_DD_REFERENCE = 20.0          # % drawdown scoring exactly 1.0; below scores >1, above <1
_CAR_DD_FLOOR = 1.0               # % floor on dd before dividing -- divide-by-zero rail only
_CAR_CONSISTENCY_FLOOR = 0.25     # worst_year/mean_year clamp lower bound
_CAR_PARTIAL_YEAR_MIN_DAYS = 182.62  # ~6 months: shorter partial start/end years merge into neighbor
_CAR_ALIASES = ("consistent_annual_return", "car", "goal")

# --- option_consistent_annual_return metric constants ------------------------------------------
# An OPTION-ONLY variant of consistent_annual_return. Every term is identical EXCEPT the drawdown
# factor, which is SUPERLINEAR here.
#
# WHY A SEPARATE METRIC AND NOT A FLAG ON THE EXISTING ONE. Non-option grids were mid-run when
# this landed, and re-ranking a search already in progress silently invalidates every result
# already banked. A flag that must be read correctly is a flag that can be read wrongly; a metric
# that an equity run never NAMES is a code path an equity run cannot reach. The equity path is
# frozen bit-for-bit by tests/test_strategy_fitness_equity_frozen.py, and
# _consistent_annual_return below is deliberately untouched -- not one line.
#
# THE DEFECT. Under the equity cap, doubling contract count doubles the annualised return AND the
# max drawdown. The linear guard (20/dd, capped at 2.0) shrinks as 1/dd, so the two cancel
# EXACTLY. Measured on the live metric at base 7.5%/yr and 5% dd for one unit of size:
#
#     size    1s      2s      4s      8s     16s
#     dd      5%     10%     20%     40%     80%
#     score  15.0    30.0    30.0    30.0    30.0
#
# i.e. leverage was REWARDED below a 10% drawdown and FREE above it. No amount of risk-taking
# ever made a genome score worse, which is the opposite of what the metric is for.
#
# THE SHAPE. penalty = (REFERENCE / max(|dd|, FLOOR)) ** 2. Squaring is the minimum that works:
# the score at k times the size is k * base * P(k*dd), so P must decay strictly FASTER than 1/dd
# or leverage keeps paying. At exponent 2 the same table becomes 120 / 60 / 30 / 15 / 7.5 --
# every doubling of size at double the drawdown now halves the score exactly.
#
# A closed form on the measured drawdown, NOT a sampled tail statistic (CVaR and friends). At
# realistic option trade counts -- tens per year -- a 5% quantile is estimated from about two
# observations, so a tail measure would contribute mostly estimator noise to the ranking. This
# term is deterministic and zero-variance.
_OCAR_DD_REFERENCE = 20.0   # % drawdown scoring exactly 1.0 -- the same risk budget as CAR
_OCAR_DD_EXPONENT = 2.0     # > 1 is what breaks the cancellation; 2 makes each doubling halve
# THE FLOOR IS THE ONLY BOUND, AND IT REPLACES THE 2.0 MULTIPLICATIVE CAP.
#
# Any bounded penalty is flat somewhere, and inside a flat region doubling size doubles the score
# outright -- worse than the indifference being fixed. So the flat region cannot be removed, only
# MOVED, and the whole design question is where to put it. The equity metric's 2.0 cap put it at
# 0-10%, INSIDE the observed 8.5-34% drawdown range, which is precisely why its table above shows
# 5% -> 10% doubling. Keeping a 2.0 cap here would be worse still: under the square it binds at
# 20/sqrt(2) = 14.1%, even further inside the range.
#
# So the cap is REMOVED and the floor is raised from CAR's 1.0 to 5.0, which bounds the reward at
# (20/5)^2 = 16x while sitting below the observed range, where it should not bind on a real
# config. CAR's 1% rail cannot be reused: squared, it would pay 400x, and the search would be
# a drawdown-minimisation contest with return as a tiebreaker.
#
# The residual, stated plainly: below 5% drawdown this metric stops rewarding safety and leverage
# pays again. That region is unreachable for a genome trading enough to clear the 12/yr floor.
_OCAR_DD_FLOOR = 5.0
# A measured total loss is a DISQUALIFICATION, not a penalty. Added 2026-08-29 after a stage-1
# long-call genome printed total_return +3189% on max_drawdown -100% and still scored fitness
# +1.6 under the squared penalty ((20/100)^2 = 0.04 on an enormous base stays positive): a
# wiped-out account kept breeding into the population. The account_wiped_out flag is the
# primary detector (entry guard in compute_fitness), but it only fires when the engine stops
# the sim at NLV <= 0 -- a curve that asymptotes to zero (or ends exactly at it on the last
# bar) measures dd = -100% WITHOUT the flag. So the measured drawdown itself must disqualify.
# NOTE: lives in this metric only, not in compute_fitness, because the equity fleet is
# mid-run and re-ranking its population mid-search invalidates banked results. Lift it into
# the entry guard when no grid is running.
#
# ORDERING (F9(a), 2026-08-30, option-program-review-findings.md): the dd>=100 check inside
# the function body runs FIRST -- before the trade-count gate and before the `base <= 0` early
# return -- and this is a deliberate ranking decision, not just a bugfix. Checking it later let
# a wiped genome escape through either of those returns: a losing return hit `base <= 0` and
# scored an ordinary small negative (ABOVE both sentinels); a thin-trade wipeout hit
# LOW_TRADE_SENTINEL (-1e8), which is numerically ABOVE WIPED_OUT_SENTINEL (-2e9) and even
# ABOVE ZERO_TRADE_SENTINEL (-1e9) -- "a 3-trade blow-up outranks never trading". The decided
# invariant is that a measured wipeout ranks WORST of every other disqualification the metric
# produces, full stop: WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0. A
# wiped account teaches the GA nothing a losing-but-alive or merely-thin-data genome would, and
# collapsing it into either of those buckets would let the search read it as "somewhat bad"
# instead of "never do this again".
_OCAR_WIPED_OUT_DD_PCT = 100.0
_OCAR_ALIASES = ("option_consistent_annual_return", "option_car", "ocar")

# --- option_convex metric constants (CONFIG, not genes) ----------------------------------------
# The CONVEX-HARVEST fitness (docs/superpowers/specs/2026-08-31-convex-harvest-grid-design.md
# §3). A book of cheap far-OTM medium/long-dated calls across many names: most tickets expire
# worthless (expected, not failure) and the few large winners must pay for the graveyard.
#
# WHY THE CAR FAMILY CANNOT JUDGE IT. ``consistent_annual_return`` and its option twin rank on
# a year-by-year consistency factor times a drawdown guard. Convex harvesting bleeds premium
# steadily and wins lumpily, so a genuinely profitable convex book scores like a bad strategy
# under either -- and the GA would "learn" to gut the convexity (tight stops, near-dated, low
# delta) to fake smoothness. The metric has to match the payoff shape, so it is a SEPARATE
# metric rather than a flag: a metric an existing grid never NAMES is a code path it cannot
# reach, and the equity path stays frozen bit-for-bit
# (tests/test_strategy_fitness_equity_frozen.py). Scores from this metric are NOT comparable
# with any CAR-family score.
#
# THE THRESHOLDS BELOW ARE CONFIG, NOT SEARCHABLE. Exposing them to the GA would let a genome
# buy its way past the floors it exists to be held to.
_CONVEX_DD_FREE_PCT = 50.0    # % peak-to-trough drawdown penalised not at all -- the premium
#                               bleed IS the cost of the book, so charging for it would price
#                               the strategy's own mechanism as a defect.
_CONVEX_DD_DEAD_PCT = 90.0    # % at which the penalty factor reaches 0.0. Linear between the
#                               two: factor = 1 - (dd - FREE) / (DEAD - FREE).
# Between DEAD and the wipeout the factor stays 0.0: a winning book that gave back nine tenths
# of its peak keeps NOTHING of its return, but it is still a scored run (0.0), ranked above
# every disqualification. That band is deliberately explicit rather than folded into the
# sentinel -- a 92% drawdown is a survivable catastrophe, a 100% one is a dead account.
_CONVEX_WIPED_OUT_DD_PCT = 100.0  # >= this is terminal: WIPED_OUT_SENTINEL, checked FIRST.
#
# BREADTH replaces the CAR trade floor (design §3.4). A convex result built on five tickets is
# a coin flip, not a strategy; rare-win strategies need many independent draws. BOTH conditions
# must hold -- ticket COUNT alone can be met by pyramiding a handful of names, and underlying
# count alone can be met by one ticket each. The conjunction is the floor.
_CONVEX_MIN_TICKETS_PER_YEAR = 30.0   # STRUCTURES per year (see _trades_per_year), not legs
_CONVEX_MIN_UNDERLYINGS = 20          # distinct underlyings traded over the whole window
_CONVEX_ALIASES = ("option_convex",)
# One name on purpose. ``option_car``'s aliases exist because the CAR family predates the
# fitness catalog; a new metric with three spellings is three strings a routing rail has to
# know about. TASK 13 SEAM: the O_CONVEX strategy key and the mutual refusal (an O_CONVEX job
# must refuse ``option_car`` and an option_car job must refuse O_CONVEX -- never silently
# cross-score) land in the LAUNCHER's ``_resolve_fitness`` / ``_OPTION_CAR_STRATEGIES``
# routing, not here: this module scores what it is handed and does not know strategy kinds.

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


# --- UI single source of truth: fitness-metrics catalog ---------------------------------------
# One entry per SELECTABLE fitness metric. The optimization UI reads this (via
# GET /api/optimization/fitness-options) so its metric list, tooltips and trade-scale gating can
# never drift from this module. Metrics that share a results-dict key but differ only in spelling
# (e.g. "sharpe" / "sharpe_ratio") are collapsed to ONE canonical entry — but every alias is still
# an accepted compute_fitness input.
#
# Fields:
#   key                       canonical metric name accepted by compute_fitness (lower-case)
#   label                     human-friendly UI label
#   description               one-line tooltip
#   supports_trade_scale      whether the fitness_trade_scale multiplier applies to this metric.
#                             False for max_drawdown (negated, not return-based) and for
#                             consistent_annual_return (compute_fitness returns before the scale
#                             block — the hard >=30/yr gate replaces it).
#   uses_adjusted_under_caps  whether an ADJUSTED (cap-aware) variant of the metric is ranked when a
#                             profit cap is active (only return-based metrics have one).

# Per-canonical-key metadata. compute_fitness collapses aliases onto these canonical keys, so the
# catalog carries one entry per DISTINCT metric behaviour, not one per alias.
_CATALOG_META = {
    "sharpe_ratio": {
        "label": "Sharpe Ratio",
        "description": "Risk-adjusted return (mean/stdev). No adjusted variant under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": False,
    },
    "total_return": {
        "label": "Total Return",
        "description": "Total return over the run. Ranks on the capped (adjusted) return when a "
                       "profit cap is active.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
    "profit_factor": {
        "label": "Profit Factor",
        "description": "Gross profit / gross loss. Cap-aware (adjusted) variant used under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
    "win_rate": {
        "label": "Win Rate",
        "description": "Share of winning trades. No adjusted variant under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": False,
    },
    "sortino_ratio": {
        "label": "Sortino Ratio",
        "description": "Downside-risk-adjusted return. No adjusted variant under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": False,
    },
    "calmar_ratio": {
        "label": "Calmar Ratio",
        "description": "Annualized return / max drawdown. Cap-aware (adjusted) variant under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
    "sqn": {
        "label": "System Quality Number",
        "description": "Van Tharp SQN (expectancy x sqrt(N)). Cap-aware (adjusted) variant under caps.",
        "supports_trade_scale": True,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
}

# Canonical key for the specials (handled outside _FITNESS_KEYS in compute_fitness).
_MAX_DRAWDOWN_KEY = "max_drawdown"
_CAR_KEY = _CAR_ALIASES[0]  # "consistent_annual_return"
_OCAR_KEY = _OCAR_ALIASES[0]  # "option_consistent_annual_return"
_CONVEX_KEY = _CONVEX_ALIASES[0]  # "option_convex"

_SPECIAL_META = {
    _MAX_DRAWDOWN_KEY: {
        "label": "Max Drawdown",
        "description": "Largest peak-to-trough equity drop (minimized: fitness is the negated dd).",
        # Negated, not return-based: neither multiplicative factor applies.
        "supports_trade_scale": False,
        "supports_win_rate_factor": False,
        "uses_adjusted_under_caps": False,
    },
    _CAR_KEY: {
        "label": "Consistent Annual Return",
        "description": "Goal metric: ~30%/yr EVERY year, >=30 trades/yr, dd<=20% ok. The hard "
                       "trade-rate gate replaces the trade-scale multiplier; win rate isn't part "
                       "of the CAR formula, so the optional win-rate factor still applies.",
        # Early-return in compute_fitness: fitness_trade_scale is a structural no-op here.
        "supports_trade_scale": False,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
    _OCAR_KEY: {
        "label": "Consistent Annual Return (Option)",
        "description": "Consistent Annual Return with a SUPERLINEAR drawdown penalty "
                       "((20/dd)^2 instead of 20/dd), so doubling position size at double the "
                       "drawdown scores strictly WORSE instead of scoring the same. For OPTION "
                       "grids: scores are NOT comparable with the plain metric.",
        "supports_trade_scale": False,
        "supports_win_rate_factor": True,
        "uses_adjusted_under_caps": True,
    },
    _CONVEX_KEY: {
        "label": "Convex Harvest (Option)",
        "description": "For a CONVEX option book (many cheap far-OTM long-dated tickets): "
                       "end-of-window total return, penalised only past a 50% drawdown, "
                       "behind a breadth floor of >=30 tickets/yr AND >=20 underlyings. "
                       "Hit rate and top-1/top-5 concentration are RECORDED, never scored. "
                       "Scores are NOT comparable with any CAR-family metric.",
        # The breadth floor replaces the trade scale (compute_fitness returns before that
        # block). The win-rate factor would SCORE the hit rate, which this metric records and
        # must never rank on -- a convex book's low hit rate is the design, not a defect. The
        # adjusted-under-caps switch clips exactly the mega-winners the thesis is about.
        "supports_trade_scale": False,
        "supports_win_rate_factor": False,
        "uses_adjusted_under_caps": False,
    },
}


def _build_metrics_catalog() -> list:
    """Build the catalog by iterating the canonical metrics + specials, REQUIRING metadata for each.

    Because the catalog is DERIVED from _FITNESS_KEYS (+ specials) and demands a metadata row for
    every canonical key, a new metric added to _FITNESS_KEYS without a matching _CATALOG_META entry
    raises here at import time (and via assert_catalog_complete in tests) — the drift guard.

    Each entry's ``key`` is the canonical metric name; ``aliases`` lists every additional
    compute_fitness input that maps to it (e.g. "sharpe" -> "sharpe_ratio"). The union of every
    entry's {key} + aliases is exactly the set of accepted fitness_metric strings.
    """
    catalog = []
    # Canonical return/ratio metrics: collapse _FITNESS_KEYS aliases to their distinct target keys,
    # then require metadata per canonical key. Aliases are every _FITNESS_KEYS name (other than the
    # canonical itself) that maps to the same target.
    seen_canonical = []
    for canonical in _FITNESS_KEYS.values():
        if canonical not in seen_canonical:
            seen_canonical.append(canonical)
    for canonical in seen_canonical:
        meta = _CATALOG_META.get(canonical)
        if meta is None:
            raise KeyError(
                f"strategy_fitness METRICS_CATALOG drift: _FITNESS_KEYS maps to {canonical!r} but "
                f"_CATALOG_META has no metadata for it. Add a _CATALOG_META entry."
            )
        aliases = sorted(a for a, tgt in _FITNESS_KEYS.items() if tgt == canonical and a != canonical)
        catalog.append({"key": canonical, "aliases": aliases, **meta})
    # Specials (max_drawdown + consistent_annual_return). max_drawdown's aliases mirror the
    # compute_fitness special-case list; CAR carries its _CAR_ALIASES.
    special_aliases = {
        _MAX_DRAWDOWN_KEY: ["drawdown", "max_dd"],
        _CAR_KEY: sorted(a for a in _CAR_ALIASES if a != _CAR_KEY),
        _OCAR_KEY: sorted(a for a in _OCAR_ALIASES if a != _OCAR_KEY),
        _CONVEX_KEY: sorted(a for a in _CONVEX_ALIASES if a != _CONVEX_KEY),
    }
    for special in (_MAX_DRAWDOWN_KEY, _CAR_KEY, _OCAR_KEY, _CONVEX_KEY):
        meta = _SPECIAL_META.get(special)
        if meta is None:
            raise KeyError(f"strategy_fitness METRICS_CATALOG drift: no metadata for {special!r}.")
        catalog.append({"key": special, "aliases": special_aliases[special], **meta})
    return catalog


METRICS_CATALOG = _build_metrics_catalog()


def catalog_accepted_metrics() -> set:
    """Every fitness_metric string the catalog claims to cover (canonical keys + all aliases)."""
    accepted = set()
    for m in _build_metrics_catalog():
        accepted.add(m["key"])
        accepted.update(m.get("aliases", ()))
    return accepted


def assert_catalog_complete() -> None:
    """Drift guard: rebuild the catalog and assert it covers EVERY _FITNESS_KEYS entry + specials.

    Rebuilding (rather than reading the module-level list) means a metric added to _FITNESS_KEYS at
    runtime — or, in practice, in source — without metadata raises here (via _build_metrics_catalog
    when the new metric's canonical target lacks _CATALOG_META, or here when a new alias/target is
    not covered). Called from the unit test; safe to call anywhere.
    """
    accepted = catalog_accepted_metrics()  # raises if any canonical/special lacks metadata
    expected = (set(_FITNESS_KEYS) | {_MAX_DRAWDOWN_KEY} | set(_CAR_ALIASES)
                | set(_OCAR_ALIASES) | set(_CONVEX_ALIASES))
    missing = expected - accepted
    if missing:
        raise AssertionError(f"METRICS_CATALOG does not cover fitness inputs: {sorted(missing)}")


def compute_fitness(fitness_metric: str, results: dict,
                    stress_spread_bps: float = 0.0,
                    robust: Optional[bool] = None) -> float:
    """Return the scalar fitness for a metric from a backtest results dict.

    ``stress_spread_bps`` > 0 additionally scores the run as if the spread were that many bps
    WIDER (see ``stressed_results``) and returns the MINIMUM of the two. Default 0.0 is exactly
    the previous behaviour, byte for byte -- this is opt-in because it RESCALES every fitness,
    so scores either side of it are not comparable (same trap as the 2026-08-04 dd_guard
    change). The min() rather than the stressed value alone: stressing can only be a penalty,
    never a way for a genome to score higher than it did at the real modelled cost.

    - None results or 0-trade runs return ZERO_TRADE_SENTINEL (distinct from 0.0).
    - A wiped-out account (results["account_wiped_out"]) returns WIPED_OUT_SENTINEL, ranked
      WORSE than ZERO_TRADE_SENTINEL — blowing up the account is worse than never trading.
    - max_drawdown/max_dd/drawdown is NEGATED (smaller drawdown -> larger fitness).
    - NaN/inf metric values collapse to ZERO_TRADE_SENTINEL (degenerate trial).
    - An unknown fitness_metric raises ValueError (no-defaults, fail-early).
    """
    if results is None:
        return ZERO_TRADE_SENTINEL
    if results.get("account_wiped_out"):
        return WIPED_OUT_SENTINEL
    if int(results.get("total_trades", 0) or 0) == 0:
        return ZERO_TRADE_SENTINEL

    metric = fitness_metric.lower()
    if metric in ("max_drawdown", "max_dd", "drawdown"):
        dd = results.get("max_drawdown")
        if dd is None:
            return ZERO_TRADE_SENTINEL
        return -float(dd)  # smaller drawdown -> larger (less negative) fitness; not win-rate-scaled
        # (negated distance metric, not a return — same reasoning as supports_trade_scale=False).

    if metric in _CAR_ALIASES:
        # Early return for the trade-frequency scale ONLY (see its own comment): its linear
        # ramp-to-100/yr would penalize the 30-40 trades/yr target zone ~3x, and the hard >=30/yr
        # gate inside the metric already replaces it. The win-rate factor is NOT similarly
        # exclusive with CAR's own machinery (win rate isn't part of the CAR formula at all), so
        # it still applies via the shared _apply_win_rate_factor call below.
        _fit = _apply_win_rate_factor(_consistent_annual_return(results), results)
        return _maybe_robust(
            _min_with_stressed(_fit, fitness_metric, results, stress_spread_bps),
            fitness_metric, results, stress_spread_bps, robust)

    if metric in _OCAR_ALIASES:
        # OPTION-ONLY variant. Same wrappers as CAR above (win-rate factor, spread stress,
        # robustness) so --robust-fitness / --stress-spread behave identically on an option
        # grid; only the drawdown term inside differs. Reached ONLY by an explicit option
        # metric name, which is what keeps a running equity grid out of this code path.
        _fit = _apply_win_rate_factor(_option_consistent_annual_return(results), results)
        return _maybe_robust(
            _min_with_stressed(_fit, fitness_metric, results, stress_spread_bps),
            fitness_metric, results, stress_spread_bps, robust)

    if metric in _CONVEX_ALIASES:
        # CONVEX-HARVEST-ONLY. Reached only by an explicit ``option_convex``, which is what
        # keeps every running equity and option_car grid out of this code path entirely (no
        # branch, no cost). NONE of the shared wrappers below is applied, and each omission is
        # a decision, not an oversight -- see _option_convex's docstring:
        #   * _apply_win_rate_factor would SCORE the hit rate this metric only records;
        #   * robust_fitness's concentration screen would score top-5 share, the exact skew the
        #     design keeps as telemetry (standing decision, 2026-08-06);
        #   * _min_with_stressed cannot be applied honestly, because ``stressed_results``
        #     restates ``annualized_return`` and ``max_drawdown`` but NOT ``total_return``,
        #     which is what this metric ranks on. Wiring it would produce a stress that looks
        #     applied and moves only the drawdown term. Fix stressed_results (and re-freeze the
        #     equity baseline, which pins its current output) when no grid is running.
        _fit = _option_convex(results)
        if isinstance(results, dict):
            # Same two keys _maybe_robust records on its non-robust path, so downstream
            # readers (reports, top-N re-scores) find what they expect.
            results["fitness_raw"] = _fit
            results["fitness_robust"] = None
        return _fit

    key = _FITNESS_KEYS.get(metric)
    if key is None:
        raise ValueError(
            f"Unknown fitness_metric: {fitness_metric!r}. "
            f"Valid: {sorted(set(_FITNESS_KEYS) | {'max_drawdown'} | set(_CAR_ALIASES) | set(_OCAR_ALIASES) | set(_CONVEX_ALIASES))}"
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
    # min(avg_trades_per_year, cap) / target, so a statistically thin config (few trades over the
    # run) is down-weighted. ~target trades/yr is the break-even (factor 1.0); a 16-trade/3yr
    # config (~5/yr) against the default target=100 is scaled x0.05, crushing lottery winners. The
    # TARGET (``fitness_trade_scale_target``, default 100) is the trades/yr rate that earns full
    # credit -- lower it (e.g. 50) for an asset class/cadence where 100/yr is an unrealistic bar
    # (options strategies trade far less often than equities). The CAP (``fitness_trade_scale_cap``,
    # default 100) separately clamps avg_trades_per_year BEFORE dividing by the target, so the GA is
    # NOT rewarded for over-trading (a scalper aiming for the multiplier) even if target is lowered.
    # Applied only to a POSITIVE fitness — scaling a losing (<=0) fitness toward 0 would wrongly
    # FAVOUR a thin loser, so those are left unchanged.
    if results.get("fitness_trade_scale") and val > 0:
        cap = float(results.get("fitness_trade_scale_cap") or 100.0)
        target = float(results.get("fitness_trade_scale_target") or 100.0)
        # STRUCTURES per year, for the same reason the CAR trade_gate uses it: on the leg rate
        # a 4-leg structure bought 4x the frequency credit it earned.
        tpy = _trades_per_year(results) or 0.0
        val *= min(float(tpy), cap) / target
    return _maybe_robust(
        _min_with_stressed(_apply_win_rate_factor(val, results),
                           fitness_metric, results, stress_spread_bps),
        fitness_metric, results, stress_spread_bps, robust)


def _maybe_robust(val: float, fitness_metric: str, results: dict,
                  stress_spread_bps: float, robust: Optional[bool]) -> float:
    """Apply the robustness adjustment and RECORD BOTH VIEWS on the results dict.

    Opt-in, exactly like the spread stress: `robust` None falls back to the level the RUN was
    configured with (echoed into results by build_results), so the flag reaches remote workers and
    top-N re-runs through the config that already crosses all of them, with no new argument in the
    worker protocol.

    Both numbers are stored -- fitness_raw and fitness_robust, plus every component -- because a
    single blended score that cannot be decomposed is not auditable, and because scores either
    side of this flag are NOT comparable (same trap as the 2026-08-04 dd_guard rescale).
    """
    if robust is None:
        robust = bool(results.get("robust_fitness") or False)
    try:
        results["fitness_raw"] = val
    except Exception:  # noqa: BLE001 -- results may be a non-dict in odd callers
        return val
    if not robust:
        results["fitness_robust"] = None
        return val
    adj, comp = robust_fitness(val, results, stress_spread_bps
                               or float(results.get("stress_spread_bps") or 0.0))
    results["fitness_robust"] = adj
    results["robustness"] = comp
    return adj


def _apply_win_rate_factor(val: float, results: dict) -> float:
    """Optional multiplicative win-rate factor (``fitness_win_rate_factor``): rewards a higher
    share of winning trades, penalizes a lower one. factor = 2 * win_rate_fraction, so 0% win ->
    0.0x, 50% win -> 1.0x (break-even, no change), 100% win -> 2.0x. Mirrors fitness_trade_scale's
    guard: applied only to a POSITIVE fitness, since scaling a losing (<=0) value by a <1.0 factor
    would IMPROVE it, wrongly rewarding a low-win-rate loser."""
    if not results.get("fitness_win_rate_factor") or val <= 0:
        return val
    win_rate = results.get("win_rate")
    if win_rate is None:
        return val
    return val * (2.0 * (float(win_rate) / 100.0))


# ---------------------------------------------------------------------------
# Trade FREQUENCY: structures, not legs
# ---------------------------------------------------------------------------
def _structure_count(trades) -> Optional[int]:
    """How many independent BETS a trade list represents, or None when it is absent.

    ``BacktestAccount.get_round_trip_trades`` keys on ``(transaction_id, contract_symbol)`` --
    ONE ROW PER LEG -- so an iron condor is 4 rows and a vertical is 2. Every count-based
    quantity built on that row count is inflated by the structure's leg count, and the
    inflation lands hardest on exactly the multi-leg credit structures whose per-bet means are
    least estimable.

    The partition is the one ``results._cap_groups`` already uses for the profit cap: option
    legs (marked by ``contract_symbol``) that share a ``transaction_id`` are ONE bet;
    everything else -- equity, and any option leg with no transaction id -- is its own. The
    equity carve-out is deliberate and matches ``_cap_groups``: a covered call books shares and
    a short call under one transaction, but the shares are a separate cost basis, so folding
    them in would UNDER-count the equity side.

    A missing ``transaction_id`` is UNKNOWN structure identity, not a shared one. Merging on
    ``None`` would join unrelated legs into a single fabricated bet and could disqualify an
    active genome outright, so unknowns stay separate.

    Returns None (never 0) when there is no trade list at all -- the caller must fall back to
    the published rate rather than read "no data" as "no trades".
    """
    if trades is None:
        return None
    slots = set()
    n = 0
    for t in trades:
        if not isinstance(t, dict):
            n += 1
            continue
        txn = t.get("transaction_id")
        if t.get("contract_symbol") and txn is not None:
            if txn in slots:
                continue
            slots.add(txn)
        n += 1
    return n


def _trades_per_year(results: dict) -> Optional[float]:
    """The run's trade frequency in STRUCTURES per year, or None when underivable.

    ``results["avg_trades_per_year"]`` is ``len(trades) / years`` computed in
    ``services/backtest/results.py`` -- the LEG rate. Rather than re-deriving a calendar
    denominator here (which would silently disagree with the equity cap's year span), the leg
    rate is rescaled by the structures/legs ratio, so the denominator is untouched and an
    equity-only run is bit-for-bit unchanged (every equity trade is its own structure).

    Falls back to the published leg rate when ``results`` carries no ``trades`` list -- the
    signature of a re-scored DB row, since ``backtests.results`` stores the trade list in its
    own column. Degraded (today's inflated behaviour), not broken.
    """
    tpy = results.get("avg_trades_per_year")
    if tpy is None:
        years = _years_spanned_by_curve(results.get("equity_curve"))
        total = int(results.get("total_trades", 0) or 0)
        tpy = (total / years) if years > 0 else None
    if tpy is None:
        return None
    tpy = float(tpy)
    trades = results.get("trades")
    structures = _structure_count(trades)
    if structures is None or not trades:
        return tpy
    return tpy * (float(structures) / float(len(trades)))


# ---------------------------------------------------------------------------
# consistent_annual_return ("car" / "goal")
# ---------------------------------------------------------------------------
def _min_with_stressed(base_fitness: float, fitness_metric: str, results: dict,
                       stress_spread_bps: float) -> float:
    """Re-score under a wider spread and keep the WORSE of the two.

    A no-op (returns ``base_fitness`` unchanged) when the stress is off, when the run has no
    usable trades, or when the base is already a sentinel -- a disqualified or wiped-out genome
    must keep its sentinel rank rather than be swapped for an ordinary negative number.

    Recurses ONCE with the stress disabled, so the stressed pass runs the identical metric code
    (win-rate factor, trade scale, cap-awareness and all) rather than a parallel reimplementation
    that could drift from it.
    """
    if stress_spread_bps <= 0:
        # Fall back to the level the RUN was configured with (echoed into results by
        # build_results). This is what makes the flag work everywhere without threading a new
        # argument through the worker protocol, the remote client and the top-N re-run path:
        # the config already crosses all of them.
        stress_spread_bps = float(results.get("stress_spread_bps") or 0.0)
    if stress_spread_bps <= 0:
        return base_fitness
    if base_fitness in (ZERO_TRADE_SENTINEL, LOW_TRADE_SENTINEL, WIPED_OUT_SENTINEL):
        return base_fitness
    stressed = stressed_results(results, stress_spread_bps)
    if stressed is None:
        return base_fitness
    return min(base_fitness, compute_fitness(fitness_metric, stressed, 0.0))


# ---------------------------------------------------------------------------------------------
# ROBUSTNESS-ADJUSTED FITNESS
# ---------------------------------------------------------------------------------------------
# REVERSAL, DELIBERATE, RECORDED. A concentration penalty in fitness was DECLINED 2026-08-06 on
# the grounds that skew is a legitimate profile and concentration belongs at deploy time. The
# 2026-08-16 robustness sweep over 84 FMPRating goal2020 results is what changed the call:
#   * 81 of 84 had top-5 trades > 40% of net P&L;
#   * 23 had top-5 > 100% -- the five best trades outweighed the ENTIRE net result, so every
#     other trade combined lost money;
#   * the headline small-band results (CAR 54-64%) were ONE position: top-1 alone was 76-79%.
# Those genomes are what an unpenalised CAR search selects for. Keeping concentration purely as a
# deploy-time check meant the GA spent its whole budget finding them and a human threw them away
# afterwards. Scoring it moves the filter INTO the search.
#
# The three screens are deliberately different in kind, because they fail differently:
#   spread        -- is the edge bigger than its transaction cost?      (re-scores finished trades)
#   monte carlo   -- does the result survive reordering the trades?     (bootstrap on the pnl list)
#   concentration -- is the result CARRIED by a handful of trades?      (share of net P&L)
# All three are POST-HOC on the finished trade list: no re-simulation, a few ms per trial. That is
# also their limit -- none can see the trades a different genome WOULD have taken.
#
# MEASURED CAVEAT (2026-08-16): the spread screen barely discriminates on these runs -- 81 of 84
# kept >80% of CAR -- because per-trade notional is only 0.8-15% of equity, so even 80bps costs a
# few points over 4 years. Expect concentration and MC to do nearly all the work here; spread only
# bites at live deployment size.

_ROBUST_MC_PATHS = int(_os.getenv("BT_ROBUST_MC_PATHS", "1000")) if False else 1000
_ROBUST_MC_SEED = 42
# top-5 share at/below which no concentration penalty applies, and the share at which it reaches
# zero. 40% is the deploy-time bar already in use; 100% is where the five best trades ARE the
# entire result.
# CONCENTRATION penalty shape: CONTINUOUS, and tending to zero as the top-5 share tends to 100%.
#
#   headroom = (DEAD - top5) / (DEAD - FREE)          # 1.0 at FREE, 0.0 at DEAD
#   factor   = clamp(headroom, 0, 1) ** EXP
#
# WHY THIS SHAPE, after two rejected ones:
#   * a LINEAR fall to a hard 0 at 100% (the original) deleted the gradient over a large region:
#     measured on opt 330, 111 of 276 trials hit it and 61 scored exactly 0.00, so selection could
#     not tell "slightly too concentrated" from "catastrophic".
#   * a FLOORED decay (floor 0.25) fixed the gradient but was too soft at the top: bt 918 -- whose
#     book is NEGATIVE without its five best trades (-1,777) -- won its band anyway, because its
#     raw fitness was 37% higher than the cleaner bt 930's.
#   * a hard GATE at 90% worked but reintroduced a cliff.
# The power curve gives all three: continuous everywhere, a real gradient across the whole 40-100
# range, and a tail that rips the score off near 100 without any discontinuity.
#
# EXP=1.5 reference points: 40% -> 1.00   55% -> 0.68   65% -> 0.50   72% -> 0.36
#                          80% -> 0.24   90% -> 0.068  95% -> 0.024  100%+ -> 0.00
#
# 100% is not an arbitrary end point: top5 >= 100% of net P&L means the book is net NEGATIVE
# without those five trades -- a sign change, not a degree. Note the Monte Carlo does NOT catch
# this (measured: bt 918 scored mc 0.95 while being negative ex-top-5), because the bootstrap
# resamples the empirical distribution and so treats the mega-winners as a repeatable feature.
# Concentration is the only screen that asks whether the edge survives REMOVING them.
_CONC_FREE_PCT = float(_os.getenv("BT_CONC_FREE_PCT", "40"))    # no penalty at or below this
_CONC_DEAD_PCT = float(_os.getenv("BT_CONC_DEAD_PCT", "100"))   # factor reaches 0 here
_CONC_EXP = float(_os.getenv("BT_CONC_EXP", "1.5"))             # >1 bites harder near DEAD
# 1.5, not 2: measured on the goal2020 corpus, EXP=2 gave the concentration factor sd(log)=2.21
# against the return metric's 0.59 -- 3.7x, i.e. the GA would optimise diversification with return
# as a tiebreaker. The factor self-attenuates as the population cleans up (opt 330: median top5
# fell 137.9% -> 50.9%, sd 2.21 -> 1.09), but at EXP=2 it still led the ranking AMONG FINALISTS,
# which is where return should decide. EXP=1.5 keeps the top-end bite (90% -> 0.068, 95% -> 0.024)
# while dropping converged influence to ~1.4x return.


def robustness_metrics(results: dict, spread_bps: float = 0.0) -> dict:
    """The three robustness screens for one finished run. Pure post-hoc, no re-simulation.

    Returns a dict with every component so BOTH the raw and the adjusted view are inspectable
    afterwards -- a single blended number that cannot be decomposed is not auditable.
    """
    out = {"top1_pct": None, "top5_pct": None, "mc_p5": None, "mc_prob_neg": None,
           "spread_keep_pct": None, "conc_factor": 1.0, "mc_factor": 1.0, "spread_factor": 1.0}
    trades = results.get("trades") or []
    pnl = [float(t.get("pnl") or 0.0) for t in trades]
    net = sum(pnl)
    if len(pnl) < 2 or net <= 0:
        return out

    # --- concentration -------------------------------------------------------------------
    srt = sorted(pnl, reverse=True)
    out["top1_pct"] = 100.0 * srt[0] / net
    out["top5_pct"] = 100.0 * sum(srt[:5]) / net
    t5 = out["top5_pct"]
    if t5 <= _CONC_FREE_PCT:
        out["conc_factor"] = 1.0
    else:
        headroom = (_CONC_DEAD_PCT - t5) / max(1e-9, _CONC_DEAD_PCT - _CONC_FREE_PCT)
        out["conc_factor"] = max(0.0, min(1.0, headroom)) ** _CONC_EXP

    # --- monte carlo: resample the TRADE ORDER ------------------------------------------
    # Bootstrap the pnl_pct sequence. This asks "was the equity path luck of ordering?" -- it
    # CANNOT see that one huge winner is a single position (resampling redraws it), which is
    # exactly why the concentration screen above exists alongside it.
    try:
        import numpy as _np
        pct = _np.array([float(t.get("pnl_pct") or 0.0) for t in trades], dtype=float) / 100.0
        if pct.size >= 2 and _np.isfinite(pct).all():
            rng = _np.random.default_rng(_ROBUST_MC_SEED)
            idx = rng.integers(0, pct.size, size=(_ROBUST_MC_PATHS, pct.size))
            finals = _np.prod(1.0 + pct[idx], axis=1)
            out["mc_p5"] = float(_np.percentile(finals, 5) - 1.0) * 100.0
            out["mc_prob_neg"] = float((finals < 1.0).mean())
            # full credit when the 5th percentile is still positive; zero when a majority of
            # reorderings lose money.
            pn = out["mc_prob_neg"]
            out["mc_factor"] = 0.0 if pn >= 0.5 else (1.0 - pn / 0.5)
            if out["mc_p5"] is not None and out["mc_p5"] <= 0:
                out["mc_factor"] *= 0.5      # a negative left tail is a real demerit, not fatal
    except Exception:  # noqa: BLE001 -- a degenerate trade list must not kill a trial
        pass

    # --- spread --------------------------------------------------------------------------
    if spread_bps and spread_bps > 0:
        st = stressed_results(results, spread_bps)
        if st is not None:
            base_car = float(results.get("annualized_return") or 0.0)
            st_car = float(st.get("annualized_return") or 0.0)
            if base_car > 0:
                keep = 100.0 * st_car / base_car
                out["spread_keep_pct"] = keep
                out["spread_factor"] = max(0.0, min(1.0, keep / 100.0))
    return out


def robust_fitness(base_fitness: float, results: dict, spread_bps: float = 0.0) -> tuple:
    """(adjusted_fitness, components). Multiplicative, so a genome must clear ALL THREE screens.

    Sentinels pass through untouched: a disqualified or wiped-out genome keeps its sentinel RANK
    rather than being replaced by an ordinary small number that would sort above a real loser.
    A NEGATIVE base is returned unchanged too -- multiplying a negative by a <1 factor would make
    a bad genome look BETTER, which is the classic sign-flip bug in penalty schemes.
    """
    comp = robustness_metrics(results, spread_bps)
    if base_fitness in (ZERO_TRADE_SENTINEL, LOW_TRADE_SENTINEL, WIPED_OUT_SENTINEL):
        return base_fitness, comp
    if base_fitness <= 0:
        return base_fitness, comp
    factor = comp["conc_factor"] * comp["mc_factor"] * comp["spread_factor"]
    return base_fitness * factor, comp


def stressed_results(results: dict, stress_spread_bps: float) -> Optional[dict]:
    """A shallow copy of ``results`` re-scored as if the spread were ``stress_spread_bps`` WIDER.

    WHY. A genome whose per-trade edge barely clears the assumed spread looks excellent at the
    modelled cost and collapses at a slightly higher one. Measured across 90 top-N runs, the
    worst offender went from +43.7%/yr to -12.4%/yr on +40bps while a low-turnover run moved
    11.4 -> 8.3. Ranking on the stressed score selects against that fragility directly, instead
    of via a proxy like average win size (which correlates at -0.85 but misses turnover).

    WHAT IT IS NOT. This does NOT re-simulate. ``apply_spread_cost`` deducts a round-trip cost
    from each trade's equity-relative pnl_pct and the equity path is rebuilt from those; WHICH
    trades happened is unchanged. At a genuinely wider spread some marginal winners would have
    stopped out instead and sizing would have shifted, so this UNDERSTATES the damage. That is
    the conservative direction for a robustness screen, and it is what makes it cost ~1ms rather
    than another full backtest -- but it is not a substitute for re-running a finalist for real.

    Returns None when the run has no usable trades, so the caller can fall back to the
    unstressed fitness rather than score a genome on an empty path.
    """
    trades = results.get("trades") or []
    if not trades or stress_spread_bps <= 0:
        return None
    from app.services.backtest.monte_carlo import (
        _path_metrics, apply_spread_cost, equity_path_from_trade_pcts,
    )

    initial = float(results.get("initial_capital") or 0.0)
    if initial <= 0:
        eq = results.get("equity_curve") or []
        initial = float(eq[0].get("equity")) if eq and eq[0].get("equity") else 0.0
    if initial <= 0:
        return None

    years = _years_spanned_by_curve(results.get("equity_curve"))
    adjusted = apply_spread_cost(trades, initial, float(stress_spread_bps))
    path = equity_path_from_trade_pcts(adjusted, initial)
    metrics = _path_metrics(path, initial, years) if years > 0 else None
    if not metrics:
        return None

    out = dict(results)
    # MUST clear the request, or the inner compute_fitness reads it back off this copy and
    # stresses again -- unbounded recursion. Passing 0.0 explicitly is not enough, because the
    # config-echo fallback in _min_with_stressed consults the dict when the argument is 0.
    out["stress_spread_bps"] = 0.0
    car = metrics.get("annualized_return")
    out["annualized_return"] = car
    # Overwrite BOTH: _consistent_annual_return prefers the adjusted figure whenever a profit
    # cap is active, so leaving the adjusted key at its unstressed value would silently ignore
    # the stress on every capped run -- i.e. on the whole grid.
    if "adjusted_annualized_return" in out:
        out["adjusted_annualized_return"] = car
    out["max_drawdown"] = metrics.get("max_drawdown")

    # Synthesize a dated curve at trade exits so the CONSISTENCY term is measured on the
    # stressed path too. Sparser than the real per-bar curve, but _calendar_year_returns only
    # reads the last point of each calendar year, which this preserves. Falls back to the
    # unstressed curve if exit times are unavailable.
    pts = []
    for t, equity in zip(trades, path[1:]):
        ts = t.get("exit_time") or t.get("entry_time")
        if ts is None:
            pts = []
            break
        pts.append({"date": ts, "equity": equity})
    if pts:
        out["equity_curve"] = pts
    return out


def _consistent_annual_return(results: dict) -> float:
    """Fitness aligned with the trading goal: ~30%/yr EVERY year, ~30 trades/yr, dd <= 20% ok.

    fitness = base x dd_guard x consistency x trade_gate, where:

    * base — ``adjusted_annualized_return`` when a profit cap is active (same adjusted-metric
      switch as the other return metrics: a lucky mega-winner must not win the search), else
      ``annualized_return``. Both are %/yr.
    * trade_gate — a PROPORTIONAL ramp, ``clamp(avg_trades_per_year / 30, 0.0, 1.0)``, not a hard
      cliff: a 15-trades/yr config scores at 0.5x, a 3-trades/yr config at 0.1x, full credit only
      at >=30/yr. This gives the GA a gradient to climb toward the trade-frequency target instead
      of a flat disqualification, where every below-floor config (29/yr or 0.3/yr alike) scored
      identically and crossover/mutation had no signal to distinguish "close" from "nowhere
      close". ``avg_trades_per_year`` missing is derived as total_trades / calendar-years spanned
      by the equity curve; if that too is underivable, the config is disqualified with
      ``LOW_TRADE_SENTINEL`` (a genuine data problem, not a thin-trading config).
    * dd_guard — 20/max(|max_drawdown|, 1%): CONTINUOUS in drawdown, exactly 1.0 at 20%, >1
      below it (10% dd -> x2.0) and <1 above it (30% dd -> x0.667, unchanged from before
      2026-08-04). 20% is the reference risk budget, not a cliff — lower drawdown scores
      strictly better, which the previous "flat 1.0 below 20%" form did not do.
    * consistency — clamp(worst_year / mean_year, 0.25, 1.0) over CALENDAR-YEAR returns from
      the equity curve. Equal years (30, 30, 30) -> 1.0 (no penalty); an uneven (50, 10, 50)
      -> 10 / 36.67 = 0.27 (a 50/10 profile is worth ~a quarter of its headline return); a
      negative year while the mean is positive drives worst/mean negative -> clamps to the
      0.25 floor (penalized hard, but the return signal still orders configs). When
      mean_year <= 0 the factor is 1.0 — the low/negative base already sinks the config, and
      scaling a negative would wrongly reward inconsistency. Partial calendar years at the
      run's start/end shorter than ~6 months are merged into their neighbor year so a 2-week
      stub can't fake a "bad year".

    A NEGATIVE base is returned unfactored: multiplying a negative by <1.0 factors (dd_guard,
    consistency, OR trade_gate) would IMPROVE a losing config, flipping the penalty's sign.

    NOTE: the external ``fitness_trade_scale`` multiplier is intentionally NOT applied to this
    metric (compute_fitness returns before that block) — trade_gate replaces it.
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

    # --- trade gate: proportional ramp toward 30 trades/yr (no hard cliff) --------------------
    # STRUCTURES per year, not legs (see _trades_per_year): an iron condor is one bet the
    # round-trip recorder emits as four rows, so the leg rate let three condors a year clear
    # the 12/yr disqualification floor and 7.5 a year earn full credit.
    tpy = _trades_per_year(results)
    if tpy is None:
        return LOW_TRADE_SENTINEL  # genuinely no trade-frequency data to score against
    # Per-run objective, defaulting to the module constants. run_daily_backtest stamps these from
    # the expert's own class attributes when it declares them, so one expert's cadence cannot
    # re-scale another's fitness (see _car_trade_thresholds_for_experts).
    _floor = float(results.get("car_hard_min_trades_per_year") or _CAR_HARD_MIN_TRADES_PER_YEAR)
    _ramp = float(results.get("car_min_trades_per_year") or _CAR_MIN_TRADES_PER_YEAR)
    if float(tpy) < _floor:
        return LOW_TRADE_SENTINEL          # disqualified: too few trades to evidence anything
    trade_gate = min(max(float(tpy) / _ramp, 0.0), 1.0)

    if base <= 0:
        return base  # unfactored: penalty factors on a negative would flip its sign

    # --- drawdown guard -------------------------------------------------------------------------
    # CONTINUOUS, not a cliff (changed 2026-08-04). The old form was
    #     1.0 if dd <= 20 else 20/dd
    # which read "20% drawdown is acceptable" but IMPLEMENTED "everything below 20% is equally
    # good" -- a different claim. It left the search indifferent between a 4% and a 19% drawdown,
    # and because higher drawdown usually carries higher return (which ``base`` rewards in full
    # while the guard stayed silent), it actively PREFERRED the riskier genome as long as it kept
    # under the cap. Measured on the aborted goal2020 pass: S2 scored 2.86 on 32% drawdowns while
    # LOSING money in two of five years, while S1 at a third of the drawdown earned nothing for
    # being safer.
    #
    # Identical to the old formula above the reference; only the blind region below it changes,
    # and it is still exactly 1.0 AT the reference so "20% is the risk budget" survives as a
    # reference point instead of a cliff.
    #
    # The floor is a DIVIDE-BY-ZERO RAIL, not a policy knob: without it dd -> 0 sends fitness to
    # infinity. It is deliberately small (1%) because a larger floor would simply relocate the
    # flaw being fixed -- every drawdown below it flattened to one value. At 1% it effectively
    # never binds (observed drawdowns run 8.5-34%), so the metric stays continuous across the
    # whole realistic range.
    #
    # NOTE: this makes CAR risk-ADJUSTED rather than risk-capped, so it now behaves much like the
    # `consistent_calmar` proposed in docs/plans/2026-07-29-regime-risk-scaling-overlay.md -- one
    # fix serves both and no second metric is needed. CAR fitness numbers from BEFORE this change
    # are NOT comparable with numbers after it; rankings within a single run still are.
    dd = abs(float(results.get("max_drawdown") or 0.0))
    dd_guard = min(_CAR_DD_REFERENCE / max(dd, _CAR_DD_FLOOR), _CAR_DD_GUARD_MAX)

    # --- yearly consistency ----------------------------------------------------------------------
    # LOUD on a MISSING curve. `consistency` is measured from calendar-year returns off the
    # equity curve; with no curve _calendar_year_returns returns [] and _consistency_factor
    # falls back to 1.0 -- a silent no-penalty default that INFLATES fitness. Measured
    # 2026-08-16 while re-scoring stored rows: 19.04 against the true 4.76, a 4x overstatement,
    # and it looked entirely plausible. The trap is specific to re-scoring from the DB, because
    # `backtests.results` deliberately EXCLUDES the curve (it lives in its own column), so a
    # caller that passes the blob straight back in gets no curve and no warning.
    # An ABSENT KEY is a caller error and raises; a curve that is present but too short to
    # measure (a sub-year run) keeps the documented 1.0 -- that case carries no consistency
    # information and is not a mistake.
    # Narrowed 2026-08-17: fire ONLY when the caller supplied a TRADES LIST but no curve. That is
    # exactly the half-restored-DB-row signature (both live in their own columns; restoring one and
    # not the other is the mistake). A synthetic fixture that carries neither is a legitimate
    # caller asking for the base metric, and the "no curve -> consistency 1.0" fallback is its
    # DOCUMENTED behaviour -- 20 existing tests assert it, and the first version of this guard
    # broke every one of them.
    if results.get("trades") and "equity_curve" not in results:
        raise ValueError(
            "consistent_annual_return requires results['equity_curve'] to measure the "
            "consistency factor, and the key is absent. If you are re-scoring a stored "
            "Backtest, note that `results` excludes the curve -- restore it from the "
            "equity_curve column first, or the score is silently inflated (~4x when the run "
            "has an uneven year)."
        )
    consistency = _consistency_factor(_calendar_year_returns(results.get("equity_curve")))
    return base * dd_guard * consistency * trade_gate


def _option_dd_penalty(dd: float) -> float:
    """The SUPERLINEAR drawdown factor: ``(REFERENCE / max(|dd|, FLOOR)) ** EXPONENT``.

    Reads the MAGNITUDE: ``max_drawdown`` is recorded negative (``results._drawdown_curve``),
    but a positive spelling must score identically rather than inverting the penalty.

    Exactly 1.0 at the 20% reference, 16.0 at or below the 5% floor, 0.444 at 30%, 0.25 at 40%.
    Non-increasing everywhere and strictly decreasing above the floor. See the constants above
    for why the shape is a closed form on the measured drawdown and why the floor, not a
    multiplicative cap, is what bounds it.
    """
    return (_OCAR_DD_REFERENCE / max(abs(float(dd)), _OCAR_DD_FLOOR)) ** _OCAR_DD_EXPONENT


def _option_consistent_annual_return(results: dict) -> float:
    """OPTION-ONLY goal metric: ``base x dd_penalty x consistency x trade_gate``.

    A near-copy of ``_consistent_annual_return`` differing in TWO terms -- the drawdown
    factor is ``_option_dd_penalty`` (superlinear) rather than the linear, capped ``dd_guard``,
    and a measured drawdown >= 100% returns WIPED_OUT_SENTINEL outright (2026-08-29: the
    squared penalty alone still scored a bust genome +1.6).
    Every other term (the adjusted-base switch under profit caps, the proportional trade gate
    and its hard floor, the per-run cadence overrides, the calendar-year consistency factor and
    its missing-curve guard, the unfactored negative-base early return) is intentionally
    identical, and ``test_strategy_fitness_option_car.py`` pins that with a ratio test so the
    two cannot drift apart on a term nobody meant to touch.

    IT IS A COPY, NOT A REFACTOR, ON PURPOSE. Factoring the shared body out would have edited
    ``_consistent_annual_return`` while non-option grids were mid-run, and the equity path had
    to stay bit-identical -- see tests/test_strategy_fitness_equity_frozen.py. Fold the two
    together when no grid is running.

    AND HERE IS WHAT THAT COSTS, so the next person can price it. The two were written in
    different worktrees and the copy silently missed a fix: the trade gate read
    ``avg_trades_per_year`` (the LEG rate) for weeks after its twin had been switched to
    ``_trades_per_year`` (STRUCTURES), so three iron condors a year cleared the 12/yr floor on
    the one metric whose whole population is multi-leg. The ratio test in
    ``test_strategy_fitness_option_car.py`` did not catch it because it varies the drawdown,
    and ``test_strategy_fitness_structure_count.py`` only exercised the equity twin. Both gaps
    are now closed -- the latter asserts the two metrics agree on the trade gate, and a drift
    guard there refuses ANY read of ``avg_trades_per_year`` outside ``_trades_per_year``.

    Scores from this metric are NOT comparable with plain ``consistent_annual_return`` scores.

    NOTE ON THE DRAWDOWN READ. ``_consistent_annual_return`` uses
    ``abs(float(results.get("max_drawdown") or 0.0))``, whose ``or 0.0`` would turn an
    unmeasurable drawdown into the MAXIMUM reward. That branch was traced and is unreachable:
    ``results._compute_metrics`` emits ``max_drawdown`` unconditionally through ``_finite``,
    which RAISES on None/NaN rather than coercing, and every path into ``compute_fitness``
    (the GA trial worker, the in-process fitness_function, the launcher top-N re-score, the
    remote /submit-trial-full round trip, and ``stressed_results``, whose
    ``monte_carlo._path_metrics`` always returns the key) goes through it. So the equity metric
    is not silently defective and no guard was added there. This metric nonetheless RAISES
    rather than defaulting, because a caller that manages to produce an absent, None or
    non-finite drawdown is passing something no live producer emits and that this metric --
    whose whole purpose is pricing drawdown -- cannot honestly score. A measured 0.0 is a real
    value and is scored via the floor, not treated as missing.
    """
    # --- superlinear drawdown penalty: READ AND DISQUALIFY FIRST, LITERALLY FIRST --------------
    # F9(a), 2026-08-30 (option-program-review-findings.md): originally this sat AFTER the
    # trade gate and the `base <= 0` early return below it, which let a wiped-out genome escape
    # WIPED_OUT_SENTINEL through either of them -- a dd>=100 genome with a losing return hit
    # `base <= 0` and returned an ordinary small negative (ranking ABOVE both sentinels), and a
    # dd>=100 genome under the trade floor hit LOW_TRADE_SENTINEL (-1e8), which numerically
    # OUTRANKS WIPED_OUT_SENTINEL (-2e9) and even ZERO_TRADE_SENTINEL (-1e9) -- "a 3-trade
    # blow-up outranks never trading". A first fix moved it ahead of both, but left it AFTER
    # the `base` derivation's own ZERO_TRADE_SENTINEL return -- unreachable from any live
    # producer (results._compute_metrics always emits max_drawdown, so an absent/NaN base
    # combined with a valid, wiped drawdown cannot occur in practice), but it made the stated
    # invariant -- a wiped account ranks WORST of all, ahead of every other early return in this
    # function -- true only for the returns that happen to come after it, not literally. Review
    # fix, 2026-08-30: moved to the top of the function body, ahead of `base` too, so the
    # invariant holds unconditionally rather than by scope. The not-finite/absent guards are
    # unchanged.
    dd_raw = results.get("max_drawdown")
    if dd_raw is None:
        raise ValueError(
            "option_consistent_annual_return requires results['max_drawdown'] and it is "
            "absent or None. An unmeasurable drawdown is not a zero drawdown: defaulting it "
            "would hand this genome the largest multiplier the metric can produce."
        )
    try:
        dd = abs(float(dd_raw))
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"option_consistent_annual_return: max_drawdown is not numeric: {dd_raw!r}"
        ) from e
    if not math.isfinite(dd):
        raise ValueError(
            f"option_consistent_annual_return: max_drawdown is not finite ({dd_raw!r}). The "
            f"run produced nonsense and is rejected rather than scored as risk-free."
        )
    if dd >= _OCAR_WIPED_OUT_DD_PCT:
        # Total loss is terminal. Ranked WORSE than never trading (ZERO_TRADE_SENTINEL) and
        # worse than a data-thin disqualification (LOW_TRADE_SENTINEL), same as an
        # engine-flagged wipeout. See _OCAR_WIPED_OUT_DD_PCT for why the flag alone does not
        # catch this, and the block comment above for why this check must run before both
        # early returns below it.
        return WIPED_OUT_SENTINEL

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

    # --- trade gate: proportional ramp, hard floor below it -----------------------------------
    # STRUCTURES per year, not legs (see _trades_per_year, which also carries the fallback to
    # the equity-curve-derived rate this used to inline). Until 2026-08-26 this read
    # ``avg_trades_per_year`` directly -- the LEG rate, since the round-trip recorder emits one
    # row per leg and an iron condor is four. Three condors a year therefore cleared the 12/yr
    # disqualification floor and 7.5 a year earned full credit: the same defect Track C fixed
    # in ``_consistent_annual_return``, still live here only because this near-copy was written
    # in a different worktree and never got the substitution.
    #
    # It matters MORE here than there. This is the DEFAULT metric for pure-option grids
    # (``_resolve_fitness``), where multi-leg structures are not an edge case but the entire
    # population, so the inflation applied to essentially every genome being ranked.
    tpy = _trades_per_year(results)
    if tpy is None:
        return LOW_TRADE_SENTINEL  # genuinely no trade-frequency data to score against
    _floor = float(results.get("car_hard_min_trades_per_year") or _CAR_HARD_MIN_TRADES_PER_YEAR)
    _ramp = float(results.get("car_min_trades_per_year") or _CAR_MIN_TRADES_PER_YEAR)
    if float(tpy) < _floor:
        return LOW_TRADE_SENTINEL          # disqualified: too few trades to evidence anything
    trade_gate = min(max(float(tpy) / _ramp, 0.0), 1.0)

    if base <= 0:
        return base  # unfactored: penalty factors on a negative would flip its sign

    dd_penalty = _option_dd_penalty(dd)

    # --- yearly consistency -------------------------------------------------------------------
    # Same loud guard as CAR: re-scoring a stored Backtest whose equity_curve column was not
    # restored silently inflates this factor to 1.0 (measured 4x overstatement).
    if results.get("trades") and "equity_curve" not in results:
        raise ValueError(
            "option_consistent_annual_return requires results['equity_curve'] to measure the "
            "consistency factor, and the key is absent. If you are re-scoring a stored "
            "Backtest, note that `results` excludes the curve -- restore it from the "
            "equity_curve column first, or the score is silently inflated (~4x when the run "
            "has an uneven year)."
        )
    consistency = _consistency_factor(_calendar_year_returns(results.get("equity_curve")))
    return base * dd_penalty * consistency * trade_gate


# ---------------------------------------------------------------------------
# option_convex: the CONVEX-HARVEST metric
# ---------------------------------------------------------------------------
def _convex_dd_factor(dd: float) -> float:
    """The drawdown factor: 1.0 below the FREE threshold, linear to 0.0 at the DEAD one.

    Reads the MAGNITUDE (``max_drawdown`` is recorded negative by ``results._drawdown_curve``
    and by ``equity_cap.capped_drawdown_curve``, but a positive spelling must score the same
    rather than inverting the shape).

    Exactly 1.0 at and below 50%, 0.5 at 70%, 0.0 at and above 90%. Non-increasing everywhere
    and strictly decreasing between the two thresholds. Deliberately NOT the CAR family's
    ``REFERENCE / dd`` shape: that prices EVERY drawdown, and a convex book's steady premium
    bleed would be charged for as if it were a defect rather than the mechanism.
    """
    d = abs(float(dd))
    if d <= _CONVEX_DD_FREE_PCT:
        return 1.0
    if d >= _CONVEX_DD_DEAD_PCT:
        return 0.0
    return 1.0 - (d - _CONVEX_DD_FREE_PCT) / (_CONVEX_DD_DEAD_PCT - _CONVEX_DD_FREE_PCT)


def _distinct_underlyings(trades) -> int:
    """How many distinct UNDERLYINGS a trade list touched.

    ``underlying_symbol`` is set on option legs (``results._trade_row``) and ``symbol`` on
    equity rows, so the union of the two is the name the bet was on either way. A set is
    insensitive to a structure's leg count, so no structures-vs-legs correction is needed here
    (unlike the ticket RATE, which needs ``_trades_per_year``).
    """
    names = set()
    for t in trades:
        if not isinstance(t, dict):
            continue
        name = t.get("underlying_symbol") or t.get("symbol")
        if name:
            names.add(str(name))
    return len(names)


def _convex_telemetry(trades, tickets_per_year: float, underlyings: int) -> dict:
    """Diagnostics RECORDED alongside the score and never folded into it.

    Hit rate and top-1/top-5 share of net P&L are the two numbers a reader will want first on a
    convex result, and both are things this metric must not rank on: a low hit rate is the
    design (design §1), and the concentration deploy-check will light up BY DESIGN (standing
    decision, 2026-08-06 -- skew is a legitimate profile and stays a deploy-time check, not a
    GA signal). Recording them here is what makes that checkable after the fact.

    Computed directly rather than through ``robustness_metrics`` on purpose: that function also
    runs a 1000-path Monte Carlo (cost this metric has no use for) and returns every share as
    None whenever the book is net negative, which is a screening decision rather than a
    reporting one. The share convention is nonetheless kept identical -- a share of a negative
    or zero net is not a share, so those stay None.
    """
    pnl = [float(t.get("pnl") or 0.0) for t in trades if isinstance(t, dict)]
    n = len(pnl)
    out = {"hit_rate_pct": None, "top1_pct": None, "top5_pct": None,
           "tickets_per_year": float(tickets_per_year), "distinct_underlyings": int(underlyings),
           "tickets_scored": n}
    if n == 0:
        return out
    out["hit_rate_pct"] = 100.0 * sum(1 for p in pnl if p > 0) / n
    net = sum(pnl)
    if net <= 0:
        return out
    srt = sorted(pnl, reverse=True)
    out["top1_pct"] = 100.0 * srt[0] / net
    out["top5_pct"] = 100.0 * sum(srt[:5]) / net
    return out


def _option_convex(results: dict) -> float:
    """CONVEX-HARVEST goal metric: ``total_return x drawdown_factor``, behind a breadth floor.

    THE ORDER OF THIS FUNCTION BODY IS THE CONTRACT (F9(a) discipline, the same literal-ordering
    rule ``_option_consistent_annual_return`` carries). Steps run in exactly this sequence and
    ``tests/test_strategy_fitness_convex_frozen.py`` pins each boundary:

      1. WIPEOUT SENTINEL, literally first. A measured drawdown >= 100% is terminal and must
         rank WORST of every disqualification this metric can produce
         (WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0). Checked after
         ANY other early return, a wiped genome escapes through it: an unreadable return would
         hand it ZERO_TRADE_SENTINEL (-1e9) and a thin book LOW_TRADE_SENTINEL (-1e8), both
         numerically ABOVE -2e9 -- "a 3-ticket blow-up outranks never trading".
      2. RETURN TERM -- the end-of-window total return, net of costs. ONE number: did the
         winners beat the graveyard. Not year-by-year consistency, which a convex book cannot
         have and should not be asked for.
      3. DRAWDOWN PENALTY -- ``_convex_dd_factor``: free below 50%, linear 50 -> 90, dead above.
      4. BREADTH FLOOR -- >= 30 tickets/yr AND >= 20 distinct underlyings, else
         LOW_TRADE_SENTINEL. The CAR family's 12/yr floor is the wrong shape here: a convex
         book's evidence comes from the NUMBER OF INDEPENDENT DRAWS, so both the rate and the
         spread across names have to clear, and either alone can be gamed (pyramid a handful
         of names for the rate; one ticket each for the spread).
      5. TELEMETRY -- recorded on ``results``, never scored.

    WHERE THE RETURN COMES FROM, AND WHY IT IS NOT THE CAPPED SERIES. Option grids run under
    ``equity_cap``. Three different series exist and conflating them is the whole trap
    (``services/backtest/equity_cap.py``):

      * DEPLOYED equity, ``min(cap, real_equity)`` -- what the SIZER sees. It reports ZERO P&L
        for every period spent above the cap, so a 5x convex winner reads as nothing. It never
        reaches a results dict, and this metric must never reconstruct it.
      * the SCORING curve, ``scoring_curve`` -- the REAL recorded equity restated with every
        period's P&L divided by the FIXED cap and compounded. No P&L is dropped. Under a cap
        ``build_results`` puts this in ``results["equity_curve"]`` and sets ``initial`` to the
        cap, so ``results["total_return"]`` IS the run's uncapped cumulative P&L expressed
        against the starting capital -- which is what this reads.
      * the CAPPED DRAWDOWN curve, ``capped_drawdown_curve`` -- peak-to-trough on cumulative
        P&L divided by the cap. Under a cap ``build_results`` derives
        ``results["max_drawdown"]`` from exactly this, which is what this reads. Peak-to-trough
        on the deployed series would report a flat, risk-free run.

    ``test_a_capped_run_ranks_on_uncapped_pnl_not_on_the_capped_equity_series`` pins it with a
    binding cap: +400% of the cap must outrank +40%, and it does not if the deployed series is
    ranked on (both read as zero).

    NO ADJUSTED-RETURN SWITCH UNDER PROFIT CAPS, deliberately, and unlike every other
    return-based metric here. ``--profit-cap-pct`` is default-ON in the launcher and clips a
    bet's gain at a multiple of its cost basis -- i.e. it clips precisely the mega-winners a
    convex book exists to catch. Ranking the adjusted figure would re-introduce, one layer up,
    the same masking the equity-cap amendment removes. The raw figure is the honest one for
    THIS thesis; the concentration it implies is reported in the telemetry instead.

    NO WIN-RATE FACTOR AND NO ROBUSTNESS ADJUSTMENT either -- see the branch in
    ``compute_fitness`` for the reasoning on each.

    A NEGATIVE return is returned UNFACTORED: multiplying it by a <1 drawdown factor would
    IMPROVE a losing book, flipping the penalty's sign (the same guard the CAR family carries).
    """
    # --- 1. WIPEOUT SENTINEL -- LITERALLY FIRST, ahead of every other read -------------------
    # Same guards and same reasoning as _option_consistent_annual_return: an unmeasurable
    # drawdown is NOT a zero drawdown, and defaulting it would hand the genome the largest
    # factor this metric can produce. A measured 0.0 is a real value and is scored.
    dd_raw = results.get("max_drawdown")
    if dd_raw is None:
        raise ValueError(
            "option_convex requires results['max_drawdown'] and it is absent or None. An "
            "unmeasurable drawdown is not a zero drawdown: defaulting it would hand this "
            "genome the largest multiplier the metric can produce."
        )
    try:
        dd = abs(float(dd_raw))
    except (TypeError, ValueError) as e:
        raise ValueError(f"option_convex: max_drawdown is not numeric: {dd_raw!r}") from e
    if not math.isfinite(dd):
        raise ValueError(
            f"option_convex: max_drawdown is not finite ({dd_raw!r}). The run produced nonsense "
            f"and is rejected rather than scored as risk-free."
        )
    if dd >= _CONVEX_WIPED_OUT_DD_PCT:
        return WIPED_OUT_SENTINEL

    # --- 2. RETURN TERM: end-of-window total return, net of costs, on UNCAPPED P&L -----------
    ret = results.get("total_return")
    if ret is None or (isinstance(ret, float) and (math.isnan(ret) or math.isinf(ret))):
        return ZERO_TRADE_SENTINEL
    ret = float(ret)

    # --- 3. DRAWDOWN PENALTY ------------------------------------------------------------------
    dd_factor = _convex_dd_factor(dd)

    # --- 4. BREADTH FLOOR: tickets/yr AND distinct underlyings, both --------------------------
    # LOUD on an absent trade list. Breadth is unmeasurable without it, and silently
    # disqualifying every genome would read as "the strategy never traded". The signature is
    # re-scoring a stored Backtest: ``backtests.results`` keeps the trade list in its OWN
    # column, so a caller that passes the blob straight back in has no ``trades`` key. An
    # EMPTY list is a real (if degenerate) measurement and is scored, not raised on.
    if "trades" not in results:
        raise ValueError(
            "option_convex requires results['trades'] to measure the breadth floor (>= "
            f"{_CONVEX_MIN_UNDERLYINGS} distinct underlyings), and the key is absent. If you "
            "are re-scoring a stored Backtest, note that `results` excludes the trade list -- "
            "restore it from the trades column first, or every genome is disqualified."
        )
    trades = results["trades"] or []
    tpy = _trades_per_year(results)
    if tpy is None:
        return LOW_TRADE_SENTINEL  # genuinely no trade-frequency data to score against
    tpy = float(tpy)
    underlyings = _distinct_underlyings(trades)
    # AND, not OR: both floors must clear. See the constants block.
    if tpy < _CONVEX_MIN_TICKETS_PER_YEAR or underlyings < _CONVEX_MIN_UNDERLYINGS:
        return LOW_TRADE_SENTINEL

    # --- 5. TELEMETRY: recorded, NEVER scored -------------------------------------------------
    telemetry = _convex_telemetry(trades, tpy, underlyings)
    try:
        results["convex_telemetry"] = telemetry
    except Exception:  # noqa: BLE001 -- results may be a non-dict in odd callers
        pass

    if ret <= 0:
        return ret  # unfactored: a penalty factor on a negative would flip its sign
    return ret * dd_factor


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
