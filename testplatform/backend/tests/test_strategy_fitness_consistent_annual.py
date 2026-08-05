"""consistent_annual_return fitness: ~30%/yr EVERY year, >=30 trades/yr, dd soft-cap 20%.

fitness = (adjusted) annualized_return x dd_guard x consistency(worst_year/mean_year).
The external fitness_trade_scale multiplier is a structural no-op for this metric.
"""
import pytest

from app.services.strategy_fitness import (
    ZERO_TRADE_SENTINEL,
    LOW_TRADE_SENTINEL,
    _calendar_year_returns,
    _consistency_factor,
    compute_fitness,
)


def _curve(points):
    return [{"date": d, "equity": e} for d, e in points]


def _r(**kw):
    """A healthy baseline result: 30%/yr, 100 trades/yr, no equity curve
    (fewer than 2 measurable years -> consistency factor 1.0).

    ``max_drawdown`` is pinned at the REFERENCE (-20%), where dd_guard is exactly 1.0, so these
    tests isolate what they actually assert (base / trade_gate / consistency). Before 2026-08-04
    the baseline was -10%, which was also neutral back when dd_guard was a flat 1.0 anywhere
    below 20% — now that the guard is continuous, -10% would silently multiply every expectation
    by 2.0. Drawdown behaviour itself is covered by the dedicated tests below.
    """
    base = {
        "total_trades": 300,
        "avg_trades_per_year": 100.0,
        "annualized_return": 30.0,
        "max_drawdown": -20.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Calendar-year return computation
# ---------------------------------------------------------------------------
def test_exact_three_calendar_years():
    curve = _curve([
        ("2020-01-02", 100_000.0),
        ("2020-06-30", 110_000.0),
        ("2020-12-31", 130_000.0),
        ("2021-06-30", 150_000.0),
        ("2021-12-31", 169_000.0),
        ("2022-06-30", 190_000.0),
        ("2022-12-30", 219_700.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert yrs == pytest.approx([30.0, 30.0, 30.0])


def test_partial_start_year_under_6_months_merges_into_next():
    # Oct-Dec 2020 stub (~3 months) merges into 2021: 136_500/100_000 - 1 = 36.5%.
    curve = _curve([
        ("2020-10-01", 100_000.0),
        ("2020-12-31", 105_000.0),
        ("2021-06-30", 120_000.0),
        ("2021-12-31", 136_500.0),
        ("2022-12-30", 150_150.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert len(yrs) == 2
    assert yrs[0] == pytest.approx(36.5)
    assert yrs[1] == pytest.approx(10.0)


def test_partial_start_year_over_6_months_kept():
    # Jun-Dec 2020 (~7 months) is its own year.
    curve = _curve([
        ("2020-06-01", 100_000.0),
        ("2020-12-31", 120_000.0),
        ("2021-12-31", 132_000.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert yrs == pytest.approx([20.0, 10.0])


def test_partial_end_year_under_6_months_merges_into_previous():
    # Jan-Feb 2023 stub merges into 2022: 145_200/110_000 - 1 = 32%.
    curve = _curve([
        ("2021-01-04", 100_000.0),
        ("2021-12-31", 110_000.0),
        ("2022-12-30", 143_000.0),
        ("2023-02-15", 145_200.0),
    ])
    yrs = _calendar_year_returns(curve)
    assert len(yrs) == 2
    assert yrs[0] == pytest.approx(10.0)
    assert yrs[1] == pytest.approx(32.0)


def test_short_or_empty_curve_yields_no_years():
    assert _calendar_year_returns(None) == []
    assert _calendar_year_returns([]) == []
    assert _calendar_year_returns(_curve([("2022-01-03", 100.0)])) == []


# ---------------------------------------------------------------------------
# 2. Consistency factor
# ---------------------------------------------------------------------------
def test_equal_years_factor_is_one():
    assert _consistency_factor([30.0, 30.0, 30.0]) == pytest.approx(1.0)


def test_uneven_50_10_50_factor():
    # mean 36.67, worst 10 -> 0.2727 (above the 0.25 floor).
    assert _consistency_factor([50.0, 10.0, 50.0]) == pytest.approx(10.0 / (110.0 / 3.0))


def test_negative_year_with_positive_mean_clamps_to_floor():
    # (40, -10, 60): mean 30, worst/mean = -0.33 -> clamped to 0.25.
    assert _consistency_factor([40.0, -10.0, 60.0]) == pytest.approx(0.25)


def test_negative_mean_factor_is_one():
    # The low/negative base already sinks it; scaling would reward inconsistency.
    assert _consistency_factor([-10.0, -20.0]) == pytest.approx(1.0)


def test_single_year_factor_is_one():
    assert _consistency_factor([30.0]) == pytest.approx(1.0)
    assert _consistency_factor([]) == pytest.approx(1.0)


def test_uneven_years_rank_below_even_years_at_equal_base():
    even = _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 130_000.0),
        ("2021-12-31", 169_000.0), ("2022-12-30", 219_700.0),
    ]))
    uneven = _r(equity_curve=_curve([
        ("2020-01-02", 100_000.0), ("2020-12-31", 150_000.0),   # +50%
        ("2021-12-31", 165_000.0),                               # +10%
        ("2022-12-30", 247_500.0),                               # +50%
    ]))
    f_even = compute_fitness("consistent_annual_return", even)
    f_uneven = compute_fitness("consistent_annual_return", uneven)
    assert f_even == pytest.approx(30.0)  # factor 1.0
    assert f_uneven == pytest.approx(30.0 * (10.0 / (110.0 / 3.0)))  # ~8.18
    assert f_uneven < f_even


# ---------------------------------------------------------------------------
# 3. Drawdown guard
# ---------------------------------------------------------------------------
def test_dd_exactly_at_reference_is_neutral():
    """20% is the reference risk budget: dd_guard == 1.0 exactly there."""
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-20.0)) == pytest.approx(30.0)


def test_dd_below_reference_scores_STRICTLY_BETTER():
    """REGRESSION (2026-08-04). dd_guard used to be a flat 1.0 anywhere below 20%, so the search
    was INDIFFERENT between a 4% and a 19% drawdown — and since higher drawdown usually carries
    higher return (rewarded in full by ``base``), it actively preferred the riskier genome. Lower
    drawdown must now score strictly better."""
    f4 = compute_fitness("consistent_annual_return", _r(max_drawdown=-4.0))
    f10 = compute_fitness("consistent_annual_return", _r(max_drawdown=-10.0))
    f19 = compute_fitness("consistent_annual_return", _r(max_drawdown=-19.0))
    f20 = compute_fitness("consistent_annual_return", _r(max_drawdown=-20.0))
    # 2026-08-05: dd_guard is now CAPPED at 2.0, which is exactly 20/10 -- so the gradient is
    # strictly monotone between the cap boundary and the reference, and deliberately FLAT below
    # it. Paying ever more for ever smaller drawdowns is what let a 4-trade/yr genome outrank a
    # richer one; below 10% the number is usually thinness rather than skill.
    assert f4 == f10                                 # flat below the cap boundary, by design
    assert f10 > f19 > f20                           # still strictly monotone above it
    assert f10 == pytest.approx(30.0 * 20.0 / 10.0)  # 2.0x at half the reference


def test_dd_above_reference_unchanged_from_before():
    """Above the reference the formula is byte-identical to the pre-2026-08-04 one."""
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-30.0)) == pytest.approx(30.0 * 20.0 / 30.0)
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-40.0)) == pytest.approx(30.0 * 20.0 / 40.0)


def test_tiny_drawdown_is_floored_not_infinite():
    """The floor is a divide-by-zero RAIL: dd -> 0 would otherwise send fitness to infinity
    (the calmar failure mode). Clamped at 1%, so the multiplier tops out at 20x — and anything
    at or below the floor scores the same, which is why the floor is kept small enough that it
    effectively never binds on real runs (observed drawdowns run 8.5-34%)."""
    capped = 30.0 * 2.0          # dd_guard ceiling (was 20/1 = 20.0 before the 2026-08-05 cap)
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-1.0)) == pytest.approx(capped)
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=-0.1)) == pytest.approx(capped)
    assert compute_fitness("consistent_annual_return", _r(max_drawdown=0.0)) == pytest.approx(capped)


# ---------------------------------------------------------------------------
# 4. Trade gate (proportional ramp toward 30/yr, no hard cliff)
# ---------------------------------------------------------------------------
def test_25_trades_per_year_ramped_not_disqualified():
    # 25/30 = 0.8333x, not a flat disqualification.
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=25.0)) == pytest.approx(
        30.0 * (25.0 / 30.0))


def test_15_trades_per_year_half_credit():
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=15.0)) == pytest.approx(30.0 * 0.5)


def test_zero_point_three_trades_per_year_tiny_but_nonzero():
    # Thin (near-zero) trading is heavily discounted, but proportionally -- not sentinel-flattened.
    # 2026-08-05: 0.3 trades/yr is now DISQUALIFIED by the hard floor rather than scored tiny.
    # A ramp alone let such configs win when paired with a small drawdown.
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=0.3)) == LOW_TRADE_SENTINEL


def test_30_trades_per_year_passes():
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=30.0)) == pytest.approx(30.0)


def test_60_trades_per_year_capped_at_full_credit():
    # Ramp clamps at 1.0 -- no reward for over-trading past the 30/yr target.
    assert compute_fitness("consistent_annual_return", _r(avg_trades_per_year=60.0)) == pytest.approx(30.0)


def test_gate_derives_trades_per_year_from_curve_when_key_missing():
    curve = _curve([("2020-01-02", 100_000.0), ("2022-12-30", 219_700.0)])  # ~3 years
    r = _r(equity_curve=curve, total_trades=95)  # ~31.8/yr -> full credit
    r.pop("avg_trades_per_year")
    assert compute_fitness("consistent_annual_return", r) > 0
    r2 = _r(equity_curve=curve, total_trades=60)  # ~20/yr -> ramped down, not disqualified
    r2.pop("avg_trades_per_year")
    f2 = compute_fitness("consistent_annual_return", r2)
    assert 0 < f2 < 30.0


def test_gate_underivable_disqualifies():
    r = _r()
    r.pop("avg_trades_per_year")  # no key, no equity curve -> no hidden default
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL


def test_sentinels_are_distinct_and_ordered():
    # no-trade (existing top-of-function guard) is WORSE than an underivable-trade-data config.
    assert compute_fitness("consistent_annual_return", _r(total_trades=0)) == ZERO_TRADE_SENTINEL
    assert ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0.0


# ---------------------------------------------------------------------------
# 5. Adjusted-base switch under profit caps
# ---------------------------------------------------------------------------
def test_profit_cap_switches_base_to_adjusted():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0, profit_cap_pct=2000.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(40.0)


def test_share_cap_also_switches_base_to_adjusted():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0, profit_share_cap_pct=25.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(40.0)


def test_no_cap_uses_raw_annualized_return():
    r = _r(annualized_return=80.0, adjusted_annualized_return=40.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# 6. fitness_trade_scale is a no-op for this metric
# ---------------------------------------------------------------------------
def test_trade_scale_is_noop():
    # 50/yr would scale calmar-style fitness x0.5; this metric must be unaffected.
    r = _r(avg_trades_per_year=50.0, fitness_trade_scale=True, fitness_trade_scale_cap=100.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(30.0)
    assert compute_fitness("consistent_annual_return", r) == compute_fitness(
        "consistent_annual_return", _r(avg_trades_per_year=50.0))


# ---------------------------------------------------------------------------
# Misc: aliases, negative base, unknown-metric error message
# ---------------------------------------------------------------------------
def test_aliases_and_case():
    for name in ("consistent_annual_return", "car", "goal", "CAR", "Goal"):
        assert compute_fitness(name, _r()) == pytest.approx(30.0)


def test_negative_base_returned_unfactored():
    # penalties on a negative would IMPROVE it -> returned as-is (still gated on trades).
    r = _r(annualized_return=-15.0, max_drawdown=-35.0)
    assert compute_fitness("consistent_annual_return", r) == pytest.approx(-15.0)


def test_unknown_metric_error_lists_new_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert "consistent_annual_return" in str(ei.value)


# ---------------------------------------------------------------------------------------------
# Hard trade floor + dd_guard ceiling (2026-08-05)
#
# Both added after the goal2020 mid band produced a 4.2 trades/yr winner (17 trades / 4 years).
# The proportional ramp scaled it to 0.14 but could not stop it: an uncapped dd_guard handed its
# 3.86% drawdown a 5.18x multiplier, which outweighed everything. Measured head-to-head, that
# genome (13.3% return) outranked one making 27.2% on 155 trades.
# ---------------------------------------------------------------------------------------------

def test_below_hard_trade_floor_is_disqualified():
    """Under 12 trades/yr the config is excluded outright, not merely scaled."""
    r = _r(total_trades=17, avg_trades_per_year=4.2)
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL


def test_at_the_hard_floor_is_scored_normally():
    r = _r(total_trades=48, avg_trades_per_year=12.0)
    assert compute_fitness("consistent_annual_return", r) > 0


def test_just_below_the_floor_is_disqualified():
    r = _r(total_trades=47, avg_trades_per_year=11.9)
    assert compute_fitness("consistent_annual_return", r) == LOW_TRADE_SENTINEL


def test_dd_guard_is_capped():
    """A 1% drawdown used to collect 20x; it now collects the 2.0 ceiling."""
    tiny = _r(max_drawdown=-1.0, avg_trades_per_year=40.0)
    ten = _r(max_drawdown=-10.0, avg_trades_per_year=40.0)
    # both sit at/above the cap boundary: 20/1 -> capped 2.0, 20/10 -> exactly 2.0
    assert compute_fitness("consistent_annual_return", tiny) == pytest.approx(compute_fitness("consistent_annual_return", ten))


def test_dd_guard_gradient_survives_above_the_cap_boundary():
    """Between 10% and 20% drawdown the Calmar gradient must still bite."""
    dd10 = compute_fitness("consistent_annual_return", _r(max_drawdown=-10.0, avg_trades_per_year=40.0))
    dd15 = compute_fitness("consistent_annual_return", _r(max_drawdown=-15.0, avg_trades_per_year=40.0))
    dd20 = compute_fitness("consistent_annual_return", _r(max_drawdown=-20.0, avg_trades_per_year=40.0))
    assert dd10 > dd15 > dd20


def test_cap_barely_touches_a_healthy_config():
    """The goal2020 large winner sits at 9.1% dd -> 2.20 uncapped, 2.0 capped: ~9% haircut, not a
    re-ranking. The cap is aimed at sub-10% thinness, not at real configs."""
    r = _r(max_drawdown=-9.1, avg_trades_per_year=134.0)
    assert compute_fitness("consistent_annual_return", r) > 0
