"""option_consistent_annual_return: the OPTION-only goal metric.

Identical to ``consistent_annual_return`` in every term but one -- the drawdown factor, which
is SUPERLINEAR here (``(20/dd)**2``) instead of linear (``20/dd``).

WHY A SEPARATE METRIC RATHER THAN A FLAG. Non-option grids were mid-run when this landed. A
flag that must be read correctly is a flag that can be read wrongly; a metric that is never
named is never reached. ``tests/test_strategy_fitness_equity_frozen.py`` is the other half of
that guarantee -- it pins the equity path bit-for-bit.

THE DEFECT THIS FIXES. Under the equity cap, doubling contract count doubles BOTH the
annualised return and the max drawdown. The linear guard shrinks as 1/dd, so the two cancel
EXACTLY: measured on the pre-change metric (base 7.5%/yr and 5% dd at size 1s),

    1s -> 15.0   2s -> 30.0   4s -> 30.0   8s -> 30.0   16s -> 30.0

i.e. leverage was REWARDED below a 10% drawdown and free above it. Nothing the strategy could
do with risk ever made it score worse.
"""
import math

import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _CAR_DD_GUARD_MAX,
    _CAR_DD_REFERENCE,
    _OCAR_ALIASES,
    _OCAR_DD_EXPONENT,
    _OCAR_DD_FLOOR,
    _OCAR_DD_REFERENCE,
    _option_dd_penalty,
    compute_fitness,
)

OCAR = "option_consistent_annual_return"


def _curve(points):
    return [{"date": d, "equity": e} for d, e in points]


def _r(**kw):
    """Healthy baseline: 30%/yr, 100 trades/yr, drawdown pinned at the 20% REFERENCE where the
    penalty is exactly 1.0, and no equity curve (fewer than 2 measurable years -> consistency
    1.0). Every test below therefore isolates the one term it names."""
    base = {
        "total_trades": 300,
        "avg_trades_per_year": 100.0,
        "annualized_return": 30.0,
        "max_drawdown": -20.0,
    }
    base.update(kw)
    return base


def _sized(k, base_at_1s=7.5, dd_at_1s=5.0, metric=OCAR):
    """Score a genome at k times the baseline contract count.

    The equity-cap premise: contract count scales the annualised return and the max drawdown
    TOGETHER and linearly. That premise is what makes the linear guard cancel, and it is
    verified against the live metric in ``test_the_cancellation_premise_holds_for_the_linear_guard``.
    """
    return compute_fitness(metric, _r(annualized_return=base_at_1s * k,
                                      max_drawdown=-(dd_at_1s * k)))


# --------------------------------------------------------------------------------------------
# 1. THE HEADLINE: doubling size at double the drawdown must now score STRICTLY WORSE
# --------------------------------------------------------------------------------------------
def test_the_cancellation_premise_holds_for_the_linear_guard():
    """Pin the DEFECT itself, on the equity metric, so the fix is measured against a fact.

    If this ever stops holding, the premise behind the option metric has changed and its shape
    needs revisiting -- so this failing is informative, not merely annoying.
    """
    scores = [_sized(k, metric="consistent_annual_return") for k in (1, 2, 4, 8, 16)]
    assert scores == pytest.approx([15.0, 30.0, 30.0, 30.0, 30.0])
    # 5% -> 10% REWARDED leverage outright; everything above 10% was exactly indifferent.
    assert scores[1] == pytest.approx(2.0 * scores[0])
    assert scores[2] == scores[3] == scores[4]


def test_doubling_size_now_scores_strictly_worse_at_every_step():
    """The fix, stated as the requirement: more size at more drawdown must never pay."""
    scores = [_sized(k) for k in (1, 2, 4, 8, 16)]
    for smaller, bigger in zip(scores, scores[1:]):
        assert bigger < smaller, f"leverage still pays: {smaller} -> {bigger}"


def test_the_size_vs_score_table_halves_exactly():
    """Not merely decreasing -- decreasing at the rate the square implies.

    Asserted separately from the strictness test above so that a mutation which makes the
    penalty decay too slowly (still monotone, but back toward cancellation) is caught. A
    "scores decrease" test alone would pass on a 1/dd**1.01 penalty.
    """
    scores = [_sized(k) for k in (1, 2, 4, 8, 16)]
    assert scores == pytest.approx([120.0, 60.0, 30.0, 15.0, 7.5])
    for smaller, bigger in zip(scores, scores[1:]):
        assert bigger == pytest.approx(smaller / 2.0)


def test_a_30_pct_drawdown_hurts_more_than_a_10_pct_one():
    """The user's requirement in their own terms, and by MORE than the linear guard managed."""
    f10 = compute_fitness(OCAR, _r(max_drawdown=-10.0))
    f30 = compute_fitness(OCAR, _r(max_drawdown=-30.0))
    assert f30 < f10

    car10 = compute_fitness("consistent_annual_return", _r(max_drawdown=-10.0))
    car30 = compute_fitness("consistent_annual_return", _r(max_drawdown=-30.0))
    # squared: (20/10)^2 / (20/30)^2 = 9x.  linear+cap: 2.0 / (20/30) = 3x.
    assert f10 / f30 == pytest.approx(9.0)
    assert car10 / car30 == pytest.approx(3.0)
    assert f10 / f30 > car10 / car30


# --------------------------------------------------------------------------------------------
# 2. The penalty's shape: monotonic, superlinear, bounded by the floor
# --------------------------------------------------------------------------------------------
def test_penalty_is_never_increasing_in_drawdown():
    """'Never let more drawdown score better' -- swept, not spot-checked."""
    prev = None
    dd = 0.0
    while dd <= 120.0:
        p = _option_dd_penalty(dd)
        if prev is not None:
            assert p <= prev, f"penalty rose at dd={dd}: {prev} -> {p}"
        prev = p
        dd += 0.1


def test_penalty_is_strictly_decreasing_above_the_floor():
    """Above the floor there must be a real gradient, not a staircase."""
    for a, b in ((5.0, 5.5), (8.0, 9.0), (12.0, 15.0), (20.0, 25.0), (34.0, 40.0)):
        assert _option_dd_penalty(b) < _option_dd_penalty(a)


def test_reference_drawdown_is_still_exactly_neutral():
    """20% remains the risk budget: the factor is exactly 1.0 there, as in the equity metric,
    so the two metrics agree on what 'the budget' means."""
    assert _option_dd_penalty(_OCAR_DD_REFERENCE) == pytest.approx(1.0)
    assert compute_fitness(OCAR, _r(max_drawdown=-20.0)) == pytest.approx(30.0)


def test_penalty_reads_the_magnitude_not_the_sign():
    """max_drawdown is recorded NEGATIVE (results.py `_drawdown_curve`), but a positive
    spelling must score identically rather than inverting the penalty."""
    assert compute_fitness(OCAR, _r(max_drawdown=-30.0)) == compute_fitness(
        OCAR, _r(max_drawdown=30.0))


def test_the_penalty_helper_takes_the_magnitude_on_its_own():
    """Asserted DIRECTLY on the helper, not only through compute_fitness.

    The metric normalises the sign before calling in, so a route through compute_fitness cannot
    tell whether the helper handles it -- the test above passes either way, and a mutation
    dropping the helper's own abs() survived until this was added. _option_dd_penalty is public
    enough to be called with a raw recorded (negative) drawdown, so it must be safe alone.
    """
    assert _option_dd_penalty(-30.0) == pytest.approx(_option_dd_penalty(30.0))
    assert _option_dd_penalty(-30.0) == pytest.approx((20.0 / 30.0) ** 2)
    assert _option_dd_penalty(-8.5) == pytest.approx((20.0 / 8.5) ** 2)


def test_the_plateau_is_the_floor_and_it_sits_below_the_observed_range():
    """THE ONE PLACE SUPERLINEARITY CANNOT HOLD, deliberately relocated.

    Any bounded penalty is flat somewhere, and inside a flat region doubling size doubles the
    score again. The equity metric put that region at 0-10% (the 2.0 guard cap), which is
    INSIDE the observed 8.5-34% drawdown range -- which is exactly why its table above shows
    5% -> 10% doubling. Here the bound is the drawdown FLOOR at 5%, below the observed range,
    and the multiplicative cap is gone.
    """
    assert _OCAR_DD_FLOOR == 5.0
    assert _option_dd_penalty(0.0) == _option_dd_penalty(4.9) == _option_dd_penalty(_OCAR_DD_FLOOR)
    assert _option_dd_penalty(5.01) < _option_dd_penalty(_OCAR_DD_FLOOR)
    # The equity metric's plateau is twice as wide and covers real configs.
    car_plateau_ends_at = _CAR_DD_REFERENCE / _CAR_DD_GUARD_MAX  # 10.0
    assert _OCAR_DD_FLOOR < car_plateau_ends_at


def test_the_reward_is_bounded_by_the_floor_alone():
    """No separate multiplicative cap: the floor IS the bound, at (20/5)**2 = 16.

    Reintroducing a cap below 16 would resurrect the flat region the floor was moved to
    escape, so its absence is load-bearing and worth pinning.
    """
    ceiling = (_OCAR_DD_REFERENCE / _OCAR_DD_FLOOR) ** _OCAR_DD_EXPONENT
    assert ceiling == pytest.approx(16.0)
    assert max(_option_dd_penalty(d / 10.0) for d in range(0, 1200)) == pytest.approx(ceiling)
    assert compute_fitness(OCAR, _r(max_drawdown=0.0)) == pytest.approx(30.0 * 16.0)


def test_a_zero_drawdown_is_a_real_measured_value_not_an_unknown():
    """`results._compute_metrics` emits max_drawdown UNCONDITIONALLY through `_finite`, which
    raises on None/NaN rather than coercing -- so a 0.0 arriving here is a measured 0%, not a
    missing one, and is scored by the floor rather than disqualified. (Verified against every
    path that reaches compute_fitness; see the module comment on _option_dd_penalty.)"""
    assert compute_fitness(OCAR, _r(max_drawdown=0.0)) == compute_fitness(
        OCAR, _r(max_drawdown=-1.0))
    assert compute_fitness(OCAR, _r(max_drawdown=0.0)) > 0


# The three unmeasurable spellings are asserted SEPARATELY rather than in one test, because
# `results.get("max_drawdown") or 0.0` -- the equity metric's read, and the obvious thing for
# someone to "simplify" this back to -- swallows the first two while NaN still raises. One
# combined test would report a single failure and hide which spellings regressed.
def test_an_absent_drawdown_key_raises_rather_than_scoring_as_zero():
    """An absent key cannot be a measured 0%. Under `or 0.0` it becomes the LARGEST multiplier
    the metric can produce -- the best possible score handed to a genome whose risk was never
    measured."""
    r = _r()
    r.pop("max_drawdown")
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCAR, r)


def test_a_none_drawdown_raises_rather_than_scoring_as_zero():
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCAR, _r(max_drawdown=None))


def test_a_non_finite_drawdown_raises_rather_than_scoring_as_zero():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="max_drawdown"):
            compute_fitness(OCAR, _r(max_drawdown=bad))


def test_a_non_numeric_drawdown_raises():
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCAR, _r(max_drawdown="lots"))


# --------------------------------------------------------------------------------------------
# 3. Every OTHER term must match the equity metric exactly (anti-drift)
# --------------------------------------------------------------------------------------------
_SHARED_CASES = {
    "plain": _r(),
    "curve_even": _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 130_000.0),
        ("2021-12-31", 169_000.0), ("2022-12-30", 219_700.0)])),
    "curve_uneven": _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 150_000.0),
        ("2021-12-31", 165_000.0), ("2022-12-30", 247_500.0)])),
    "thin_15": _r(avg_trades_per_year=15.0),
    "at_floor_12": _r(avg_trades_per_year=12.0),
    "over_60": _r(avg_trades_per_year=60.0),
    "per_run_thresholds": _r(avg_trades_per_year=10.0, car_hard_min_trades_per_year=8.0,
                             car_min_trades_per_year=20.0),
    "cap_basis": _r(annualized_return=80.0, adjusted_annualized_return=40.0,
                    profit_cap_pct=2000.0),
    "cap_share": _r(annualized_return=80.0, adjusted_annualized_return=40.0,
                    profit_share_cap_pct=25.0),
    # An adjusted figure present but NO cap active. Without this case, a metric that always
    # preferred the adjusted base would look identical to one that only prefers it under a cap
    # -- a mutation that survived the first battery.
    "no_cap_adjusted_present": _r(annualized_return=80.0, adjusted_annualized_return=40.0),
    "win_rate_factor": _r(fitness_win_rate_factor=True, win_rate=61.0),
    "dd_8_5": _r(max_drawdown=-8.5),
    "dd_34": _r(max_drawdown=-34.0),
}


@pytest.mark.parametrize("name", sorted(_SHARED_CASES))
def test_only_the_drawdown_term_differs_from_the_equity_metric(name):
    """DRIFT GUARD. The option metric is a near-copy, so the two can diverge by accident on a
    term nobody meant to touch. Every shared factor (base, adjusted-base switch, trade gate,
    per-run thresholds, consistency, win-rate factor) must cancel out of the ratio, leaving
    exactly the ratio of the two drawdown factors.
    """
    r = _SHARED_CASES[name]
    car = compute_fitness("consistent_annual_return", dict(r))
    ocar = compute_fitness(OCAR, dict(r))
    dd = abs(float(r["max_drawdown"]))
    car_guard = min(_CAR_DD_REFERENCE / max(dd, 1.0), _CAR_DD_GUARD_MAX)
    assert ocar == pytest.approx(car * _option_dd_penalty(dd) / car_guard, rel=1e-12)


def test_the_drift_guard_covers_more_than_one_drawdown():
    """A ratio test run only at the reference (where both factors are 1.0) proves nothing --
    it would pass on two metrics that are simply equal. Require real spread on both sides."""
    dds = {abs(float(r["max_drawdown"])) for r in _SHARED_CASES.values()}
    assert len(dds) >= 3
    ratios = {round(_option_dd_penalty(d) / min(_CAR_DD_REFERENCE / max(d, 1.0),
                                                _CAR_DD_GUARD_MAX), 6) for d in dds}
    assert len(ratios) >= 3, "drift guard cases do not distinguish the two drawdown factors"


def test_the_adjusted_base_is_used_ONLY_when_a_cap_is_active():
    """Both directions. Under a cap the capped figure must rank (one lucky mega-winner must not
    win the search); with no cap the raw figure must, even when an adjusted one is lying around
    in the results dict -- otherwise every uncapped run is silently scored on the wrong number.
    """
    assert compute_fitness(OCAR, _r(annualized_return=80.0, adjusted_annualized_return=40.0,
                                    profit_cap_pct=2000.0)) == pytest.approx(40.0)
    assert compute_fitness(OCAR, _r(annualized_return=80.0, adjusted_annualized_return=40.0,
                                    profit_share_cap_pct=25.0)) == pytest.approx(40.0)
    assert compute_fitness(OCAR, _r(annualized_return=80.0,
                                    adjusted_annualized_return=40.0)) == pytest.approx(80.0)


def test_trade_gate_ramp_and_hard_floor_are_inherited():
    assert compute_fitness(OCAR, _r(avg_trades_per_year=4.2)) == LOW_TRADE_SENTINEL
    assert compute_fitness(OCAR, _r(avg_trades_per_year=11.9)) == LOW_TRADE_SENTINEL
    assert compute_fitness(OCAR, _r(avg_trades_per_year=12.0)) > 0
    assert compute_fitness(OCAR, _r(avg_trades_per_year=15.0)) == pytest.approx(30.0 * 0.5)
    assert compute_fitness(OCAR, _r(avg_trades_per_year=60.0)) == pytest.approx(30.0)


def test_underivable_trade_rate_disqualifies():
    r = _r()
    r.pop("avg_trades_per_year")
    assert compute_fitness(OCAR, r) == LOW_TRADE_SENTINEL


def test_sentinels_are_inherited():
    assert compute_fitness(OCAR, _r(total_trades=0)) == ZERO_TRADE_SENTINEL
    assert compute_fitness(OCAR, _r(account_wiped_out=True)) == WIPED_OUT_SENTINEL
    assert compute_fitness(OCAR, None) == ZERO_TRADE_SENTINEL


def test_a_measured_total_loss_is_disqualified_regardless_of_return():
    """Added 2026-08-29 after a live stage-1 genome: total_return +3189% on
    max_drawdown -100% scored fitness +1.6 under the squared penalty alone -- a positive
    score kept a bust genome breeding. A curve that ends at zero without the engine's
    account_wiped_out flag must still be killed."""
    assert compute_fitness(OCAR, _r(max_drawdown=-100.0)) == WIPED_OUT_SENTINEL
    # an enormous base must not rescue it
    assert compute_fitness(OCAR, _r(annualized_return=500.0, max_drawdown=-100.0)) == \
        WIPED_OUT_SENTINEL
    # magnitude semantics like the penalty itself
    assert compute_fitness(OCAR, _r(max_drawdown=100.0)) == WIPED_OUT_SENTINEL
    assert compute_fitness(OCAR, _r(max_drawdown=-141.7)) == WIPED_OUT_SENTINEL
    # ranked WORSE than never having traded at all
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL


def test_a_near_total_loss_is_penalized_but_not_disqualified():
    score = compute_fitness(OCAR, _r(max_drawdown=-99.9))
    assert score != WIPED_OUT_SENTINEL
    assert math.isfinite(score)


# --------------------------------------------------------------------------------------------
# F9(a), 2026-08-30: the wipeout check must fire BEFORE the `base <= 0` early return and BEFORE
# the trade-count gate, not after. Both holes let a wiped-out genome escape WIPED_OUT_SENTINEL
# and land somewhere that outranks it -- an ordinary negative score, or LOW_TRADE_SENTINEL,
# both of which are numerically ABOVE WIPED_OUT_SENTINEL. Mutation: reverting the ordering (dd
# read moved back below the trade gate / `base <= 0` return) makes these fail.
# --------------------------------------------------------------------------------------------
def test_a_wiped_out_genome_with_negative_return_is_still_disqualified():
    """dd >= 100 AND a losing annualized_return. Pre-fix: `base <= 0` returned the unfactored
    negative base (an ordinary small negative) before the dd check was ever reached -- ranking
    a total wipeout ABOVE both sentinels. Post-fix: the wipeout must win regardless of sign."""
    score = compute_fitness(OCAR, _r(annualized_return=-25.0, max_drawdown=-100.0))
    assert score == WIPED_OUT_SENTINEL
    assert score != pytest.approx(-25.0)
    # an even deeper measured loss must not change the verdict
    assert compute_fitness(OCAR, _r(annualized_return=-90.0, max_drawdown=-250.0)) == \
        WIPED_OUT_SENTINEL


def test_a_wiped_out_genome_under_the_trade_floor_is_still_disqualified():
    """dd >= 100 AND avg_trades_per_year under the hard floor (12/yr). Pre-fix: the trade gate
    returned LOW_TRADE_SENTINEL (-1e8) before the dd check was ever reached -- a 3-trade
    blow-up outranked LOW_TRADE's sibling ZERO_TRADE_SENTINEL (-1e9) and every other
    WIPED_OUT_SENTINEL (-2e9) case. Post-fix: the wipeout must win regardless of trade count."""
    score = compute_fitness(OCAR, _r(avg_trades_per_year=3.0, max_drawdown=-100.0))
    assert score == WIPED_OUT_SENTINEL
    assert score != LOW_TRADE_SENTINEL


def test_wipeout_ranks_worst_of_all_sentinels_by_construction():
    """The deliberate order, stated as a chain: a wiped account is worse than never trading,
    which is worse than a data-thin trial that was merely disqualified, full stop -- not "worse
    unless it also happened to lose money slowly" or "worse unless it also happened to barely
    trade". Both are half-measures this finding closes."""
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0


def test_wipeout_outranks_even_an_absent_base_review_fix_2026_08_30():
    """The invariant made LITERAL, not just scoped: the dd read now sits ahead of the `base`
    derivation itself, so a wiped drawdown wins even against results a live producer would
    never actually emit together (results._compute_metrics always emits annualized_return, so
    this combination cannot occur in practice -- but the earlier ordering only happened to be
    correct for every reachable case, not because the code said so). Before this fix the
    `base is None`/NaN/inf check ran FIRST and returned ZERO_TRADE_SENTINEL before the dd read
    was ever reached."""
    r = _r(max_drawdown=-100.0)
    r["annualized_return"] = None
    assert compute_fitness(OCAR, r) == WIPED_OUT_SENTINEL
    assert compute_fitness(OCAR, r) != ZERO_TRADE_SENTINEL

    r_nan = _r(max_drawdown=-100.0, annualized_return=float("nan"))
    assert compute_fitness(OCAR, r_nan) == WIPED_OUT_SENTINEL

    r_inf = _r(max_drawdown=-100.0, annualized_return=float("inf"))
    assert compute_fitness(OCAR, r_inf) == WIPED_OUT_SENTINEL


def test_negative_base_returned_unfactored():
    """INHERITED, and deliberately so: multiplying a negative by a <1 factor would IMPROVE a
    losing genome. The consequence -- that no risk term ever touches a losing genome, so risk
    is priced only along the profitable ridge -- is reported, not fixed, in Track D."""
    assert compute_fitness(OCAR, _r(annualized_return=-15.0, max_drawdown=-35.0)) == \
        pytest.approx(-15.0)
    # ...and the squared penalty must not sneak in through the back door either.
    assert compute_fitness(OCAR, _r(annualized_return=-15.0, max_drawdown=-5.0)) == \
        pytest.approx(-15.0)


def test_missing_curve_with_trades_still_raises_loudly():
    """The half-restored-DB-row guard is inherited: re-scoring a stored Backtest without its
    equity_curve column silently inflates the consistency term."""
    r = _r(trades=[{"pnl": 1.0, "pnl_pct": 0.1}])
    with pytest.raises(ValueError, match="equity_curve"):
        compute_fitness(OCAR, r)


# --------------------------------------------------------------------------------------------
# 4. Registration: reachable by name, and NOT reachable from an equity run
# --------------------------------------------------------------------------------------------
def test_aliases_and_case_resolve():
    for name in ("option_consistent_annual_return", "option_car", "ocar",
                 "OCAR", "Option_CAR"):
        assert compute_fitness(name, _r()) == pytest.approx(30.0)


def test_an_equity_run_cannot_reach_the_option_metric():
    """THE HARD CONSTRAINT, asserted structurally rather than by inspection: none of the
    option names is an equity name, and none of the equity names dispatches to the new code.
    A grid already running names one of the latter, so it cannot arrive here."""
    from app.services import strategy_fitness as sf

    equity_names = set(sf._FITNESS_KEYS) | {"max_drawdown", "max_dd", "drawdown"} | set(
        sf._CAR_ALIASES)
    assert equity_names.isdisjoint(set(_OCAR_ALIASES))

    # And no equity name produces the squared penalty: at 40% drawdown the two factors differ
    # by 2x, so a metric that had silently switched would show it here.
    r = _r(max_drawdown=-40.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0 * 0.5)
    assert compute_fitness(OCAR, r) == pytest.approx(30.0 * 0.25)


def test_catalog_lists_the_option_metric_with_metadata():
    from app.services import strategy_fitness as sf

    sf.assert_catalog_complete()
    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    entry = by_key[OCAR]
    assert set(entry["aliases"]) == set(_OCAR_ALIASES) - {OCAR}
    assert entry["supports_trade_scale"] is False   # trade_gate replaces it, as for CAR
    assert entry["supports_win_rate_factor"] is True
    assert entry["uses_adjusted_under_caps"] is True
    assert "option" in entry["label"].lower()


def test_catalog_completeness_helper_covers_the_option_aliases():
    """The drift guard must actually police the new aliases, not just tolerate them."""
    from app.services import strategy_fitness as sf

    accepted = sf.catalog_accepted_metrics()
    assert set(_OCAR_ALIASES) <= accepted


def test_unknown_metric_error_advertises_the_option_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert OCAR in str(ei.value)


# --------------------------------------------------------------------------------------------
# 5. The shared post-processing wrappers still apply
# --------------------------------------------------------------------------------------------
def test_trade_scale_is_a_noop_as_it_is_for_car():
    r = _r(avg_trades_per_year=50.0, fitness_trade_scale=True, fitness_trade_scale_cap=100.0)
    assert compute_fitness(OCAR, r) == pytest.approx(
        compute_fitness(OCAR, _r(avg_trades_per_year=50.0)))


def test_win_rate_factor_applies():
    r = _r(fitness_win_rate_factor=True, win_rate=25.0)
    assert compute_fitness(OCAR, r) == pytest.approx(30.0 * 0.5)


def test_robustness_and_stress_wrappers_engage():
    """The new metric must ride the same _min_with_stressed / _maybe_robust plumbing rather
    than bypassing it -- otherwise --robust-fitness and --stress-spread would silently do
    nothing on an option grid."""
    trades = [{"pnl": 100.0, "pnl_pct": 1.0, "exit_time": "2020-06-01"},
              {"pnl": -40.0, "pnl_pct": -0.4, "exit_time": "2021-06-01"},
              {"pnl": 500.0, "pnl_pct": 5.0, "exit_time": "2022-06-01"}]
    curve = _curve([("2020-01-02", 100_000.0), ("2020-12-31", 130_000.0),
                    ("2021-12-31", 169_000.0), ("2022-12-30", 219_700.0)])

    plain = _r(equity_curve=curve, trades=trades, initial_capital=100_000.0)
    baseline = compute_fitness(OCAR, dict(plain))

    robust = dict(plain, robust_fitness=True)
    out = compute_fitness(OCAR, robust)
    assert robust["fitness_raw"] == pytest.approx(baseline)
    assert robust["robustness"] is not None
    assert out <= baseline

    stressed = dict(plain, stress_spread_bps=40.0)
    assert compute_fitness(OCAR, stressed) <= baseline


def test_stress_cannot_turn_a_disqualified_genome_into_a_scored_one():
    r = _r(avg_trades_per_year=4.2, stress_spread_bps=40.0,
           trades=[{"pnl": 1.0, "pnl_pct": 0.1, "exit_time": "2020-06-01"}],
           equity_curve=_curve([("2020-01-02", 100_000.0), ("2022-12-30", 219_700.0)]),
           initial_capital=100_000.0)
    assert compute_fitness(OCAR, r) == LOW_TRADE_SENTINEL


# --------------------------------------------------------------------------------------------
# 6. Realistic-range sanity
# --------------------------------------------------------------------------------------------
def test_the_metric_is_exactly_calmar_per_unit_of_drawdown():
    """The closed form the square implies, worth stating because it is the whole policy:

        base * (REF/dd)**2  ==  (base/dd) * REF**2/dd  ==  Calmar * REF**2/dd

    So this metric ranks on RISK-ADJUSTED RETURN, further divided by absolute drawdown. Every
    consequence below follows from that one line.
    """
    for base_ret, dd in ((30.0, 17.0), (12.0, 8.5), (51.0, 34.0), (25.0, 20.0)):
        got = compute_fitness(OCAR, _r(annualized_return=base_ret, max_drawdown=-dd))
        calmar = base_ret / dd
        assert got == pytest.approx(calmar * _OCAR_DD_REFERENCE ** 2 / dd)


def test_return_still_decides_between_genomes_at_comparable_drawdown():
    """A risk term so steep that return stops mattering would be its own failure. At similar
    drawdowns the better earner must still win."""
    rich = compute_fitness(OCAR, _r(annualized_return=30.0, max_drawdown=-18.0))
    poor = compute_fitness(OCAR, _r(annualized_return=25.0, max_drawdown=-17.0))
    assert rich > poor


def test_a_lower_drawdown_genome_can_outrank_a_better_calmar_one():
    """THE PRICE OF SUPERLINEARITY, recorded rather than discovered later.

    From the closed form, genome A beats B iff (calmar_A/calmar_B) > (dd_A/dd_B). So a genome
    must improve Calmar FASTER than it increases drawdown -- and a higher-Calmar genome that
    got there with more drawdown LOSES. Measured: 30%/yr at 17% dd (Calmar 1.76) is beaten by
    10%/yr at 8.5% dd (Calmar 1.18).

    This is not a bug in the shape, it is what "more drawdown must hurt more than
    proportionally" means arithmetically. It is dialled by _OCAR_DD_EXPONENT alone.
    """
    rich = compute_fitness(OCAR, _r(annualized_return=30.0, max_drawdown=-17.0))
    safe = compute_fitness(OCAR, _r(annualized_return=10.0, max_drawdown=-8.5))
    assert safe > rich
    # ...and the crossover really is where the closed form says it is.
    assert (10.0 / 8.5) / (30.0 / 17.0) > 8.5 / 17.0


def test_equal_calmar_now_prefers_the_less_levered_genome():
    """Genomes with IDENTICAL Calmar are the same trade at different leverage. Above the equity
    metric's cap boundary they tie EXACTLY -- that tie IS the cancellation. The user's
    requirement that more drawdown hurt more necessarily breaks the tie toward the smaller one;
    recording it so the consequence is a decision on the record, not a surprise.
    """
    small = _r(annualized_return=18.0, max_drawdown=-12.0)   # calmar 1.5
    large = _r(annualized_return=51.0, max_drawdown=-34.0)   # calmar 1.5
    assert compute_fitness("consistent_annual_return", dict(small)) == pytest.approx(
        compute_fitness("consistent_annual_return", dict(large)))
    assert compute_fitness(OCAR, dict(small)) > compute_fitness(OCAR, dict(large))


def test_the_equity_metric_penalises_the_SAFEST_equal_calmar_genome():
    """A sharper statement of the defect, found while writing the test above.

    Below the 2.0 cap boundary the equity metric does not merely stop rewarding safety -- the
    cap makes the safest genome score WORSE than its levered twin at identical Calmar
    (25.5 vs 30.0). So the pre-change metric was, in that band, an argument FOR leverage.
    """
    safest = _r(annualized_return=12.75, max_drawdown=-8.5)   # calmar 1.5, below the boundary
    levered = _r(annualized_return=51.0, max_drawdown=-34.0)  # calmar 1.5, above it
    assert compute_fitness("consistent_annual_return", dict(safest)) == pytest.approx(25.5)
    assert compute_fitness("consistent_annual_return", dict(levered)) == pytest.approx(30.0)
    # The option metric orders them the other way round, monotonically in drawdown.
    assert compute_fitness(OCAR, dict(safest)) > compute_fitness(OCAR, dict(levered))


def test_risk_versus_return_dominance_over_the_observed_band_is_pinned():
    """How loudly the risk term now speaks relative to the return term -- a number, not a
    feeling, so changing _OCAR_DD_EXPONENT forces an explicit re-decision.

    Over the observed 8.5-34% drawdown band and the observed 13-64%/yr CAR band, comparing
    log-spans: the equity metric's linear+capped guard scores 0.77x (return leads), exponent
    1.5 would score 1.30x and exponent 2.0 scores 1.74x (risk leads).

    Calibration reference from this same module: the concentration penalty's _CONC_EXP comment
    records that 3.7x dominance was REJECTED as "the GA would optimise diversification with
    return as a tiebreaker" and ~1.4x was accepted. 1.74x sits between the two -- deliberate,
    since making drawdown matter more is the entire point, but close enough to the rejected
    regime to be worth watching.
    """
    dd_lo, dd_hi = 8.5, 34.0
    return_log_span = math.log(64.0 / 13.0)
    risk_log_span = math.log(_option_dd_penalty(dd_lo) / _option_dd_penalty(dd_hi))
    dominance = risk_log_span / return_log_span
    assert dominance == pytest.approx(1.74, abs=0.01)

    car_guard = lambda d: min(_CAR_DD_REFERENCE / max(d, 1.0), _CAR_DD_GUARD_MAX)  # noqa: E731
    car_dominance = math.log(car_guard(dd_lo) / car_guard(dd_hi)) / return_log_span
    assert car_dominance == pytest.approx(0.77, abs=0.01)
    assert dominance > car_dominance


def test_scores_stay_finite_across_the_whole_drawdown_sweep():
    # 2026-08-29: dd >= 100% now returns WIPED_OUT_SENTINEL outright (kill switch), so the
    # sweep splits at the threshold instead of demanding positivity everywhere.
    for dd in [d / 4.0 for d in range(0, 400)]:
        f = compute_fitness(OCAR, _r(max_drawdown=-dd))
        assert math.isfinite(f) and f > 0
    for dd in [d / 4.0 for d in range(400, 481)]:
        assert compute_fitness(OCAR, _r(max_drawdown=-dd)) == WIPED_OUT_SENTINEL
