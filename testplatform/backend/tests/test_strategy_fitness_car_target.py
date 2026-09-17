"""``option_car_target``: the "CAR > 35 AND CAR > DD" option metric.

WHY IT EXISTS. The operator stated the actual option objective precisely on 2026-09-17, after
watching the two existing option metrics rank a real population: "a fitness calibrated for
CAR > 35 and CAR > DD". TWO TARGETS -- and neither existing metric aims at them:

  * ``option_consistent_annual_return``'s drawdown term is ``(20/max(dd,5))**2``, which pays up
    to 16x for a tiny drawdown; it converged a gated stage-1 job onto 10.6%-CAR / 8.9%-DD
    grinders.
  * ``option_car_over_risk`` divides by ``sqrt(dd)``, which is INDIFFERENT to the CAR/DD ratio:
    a 40%-CAR / 40%-DD genome (ratio 1.00, the target met exactly) and a 20%-CAR / 10%-DD one
    (ratio 2.00, half the return) both score 6.32 under it. The live run's whole elite sits at
    ratios 0.18-0.53, a region that metric says nothing about at all.

THE SHAPE:

    score = base
          x min(base / 35, 1.0)        # ramp to the CAR target, neutral above
          x min((base / dd) / 1.0, 1)  # ramp to CAR == DD, neutral above (NO over-safety bonus)
          x high_dd_penalty(dd)        # 1.0 to 40% dd, then (40/dd) ** 1.5
          x consistency x trade_gate

BOTH RAMPS ARE SOFT ON PURPOSE. Nothing in the live run meets either target yet, so a hard gate
would score the entire population 0 and leave the GA a flat landscape with no gradient to climb.
Section 3's "the ramps are soft" test is that decision, pinned.

Everything else -- the drawdown read-and-disqualify guard, the profit-cap base switch, the
STRUCTURES-per-year trade gate, the unfactored negative base, the missing-curve guard, the
calendar-year consistency factor -- is ``_option_car_over_risk``'s behaviour, copied term for
term. Section 5 pins each of those; section 6 pins that NEITHER older metric moved.

THE TABLE IN SECTION 1 IS THE APPROVED RANKING (signed off 2026-09-17). It is the specification,
not an observation of the implementation: if a future change moves one of these numbers, the
change is wrong until the operator re-approves the table.
"""
import math

import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _OCT_ALIASES,
    _OCT_CAR_TARGET,
    _OCT_DD_EXPONENT,
    _OCT_DD_TOLERANCE,
    _OCT_MAR_TARGET,
    _option_car_target,
    _option_car_target_dd_penalty,
    _option_car_target_factor,
    compute_fitness,
)

OCT = "option_car_target"
OCR = "option_car_over_risk"
OCAR = "option_consistent_annual_return"

# ONE calendar year, so ``_calendar_year_returns`` yields fewer than two measurable years and
# ``consistency`` is exactly 1.0. Combined with 100 structures/yr (>= the 30/yr ramp target,
# gate exactly 1.0) every score below is ``base x factor`` and nothing else -- which is what lets
# the table be asserted as absolute numbers rather than as ratios.
_CURVE = [{"date": "2020-01-02", "equity": 100_000.0},
          {"date": "2020-12-31", "equity": 130_000.0}]


def _r(car=30.0, dd=20.0, **kw):
    """A result whose consistency and trade_gate are both EXACTLY 1.0."""
    base = {
        "total_trades": 300,
        "avg_trades_per_year": 100.0,   # no ``trades`` list -> _trades_per_year uses this rate
        "annualized_return": float(car),
        "max_drawdown": -float(dd),     # recorded negative, as the engine emits it
        "equity_curve": _CURVE,
    }
    base.update(kw)
    return base


def test_the_two_neutral_terms_really_are_neutral():
    """DISCRIMINATOR for every absolute number below: if ``_r`` ever stopped producing
    consistency == trade_gate == 1.0, the whole table would drift together and still look
    self-consistent. Pin it against the factor helper directly."""
    score = compute_fitness(OCT, _r(car=50.0, dd=30.0))
    assert score == pytest.approx(50.0 * _option_car_target_factor(50.0, 30.0))


# --------------------------------------------------------------------------------------------
# 1. THE APPROVED TABLE
# --------------------------------------------------------------------------------------------
_APPROVED = [
    ("your 'interesting'",   80.0, 40.0, 80.00),
    ("aggressive",           60.0, 45.0, 50.28),
    ("BOTH targets met",     50.0, 30.0, 50.00),
    ("same CAR, worse DD",   50.0, 45.0, 41.90),
    ("just over both",       40.0, 35.0, 40.00),
    ("at both thresholds",   35.0, 35.0, 35.00),
    ("big CAR, big DD",     100.0, 90.0, 29.63),
    ("safe moderate",        25.0, 10.0, 17.86),
    ("current leader",       23.7, 44.4,  7.33),
    ("old grinder",          10.6,  8.9,  3.21),
    ("current 2nd",          18.1, 46.4,  2.92),
    ("low CAR great MAR",     8.0,  4.0,  1.83),
]


@pytest.mark.parametrize("label,car,dd,expected", _APPROVED,
                         ids=[row[0] for row in _APPROVED])
def test_the_approved_scores(label, car, dd, expected):
    assert compute_fitness(OCT, _r(car=car, dd=dd)) == pytest.approx(expected, abs=0.005)


def test_the_approved_table_is_in_descending_order():
    """The table is a RANKING, and the ranking is the thing that was approved. Asserting the
    order separately from the numbers catches a change that shifts every score by a constant
    (which the per-row assertions would also catch) as well as one that reorders two rows while
    leaving both inside their tolerance (which they would not)."""
    scores = [compute_fitness(OCT, _r(car=car, dd=dd)) for _, car, dd, _ in _APPROVED]
    assert scores == sorted(scores, reverse=True), list(zip([r[0] for r in _APPROVED], scores))


# --------------------------------------------------------------------------------------------
# 2. THE SIX PROPERTIES THE METRIC EXISTS FOR -- as COMPARISONS, not numbers
# --------------------------------------------------------------------------------------------
def _score(car, dd):
    return compute_fitness(OCT, _r(car=car, dd=dd))


def test_meeting_both_targets_beats_meeting_neither():
    """PROPERTY 1, and the headline claim: the metric is calibrated FOR the two targets, so
    every genome that clears both must outrank every genome that clears neither -- whatever
    their individual CARs and drawdowns are."""
    both = {(car, dd): _score(car, dd) for _, car, dd, _ in _APPROVED
            if car >= _OCT_CAR_TARGET and car >= dd * _OCT_MAR_TARGET}
    neither = {(car, dd): _score(car, dd) for _, car, dd, _ in _APPROVED
               if car < _OCT_CAR_TARGET and car < dd * _OCT_MAR_TARGET}
    assert len(both) >= 5 and len(neither) >= 2, (both, neither)   # the table really covers both
    assert min(both.values()) > max(neither.values()), (both, neither)


def test_above_both_targets_the_ranking_follows_car():
    """PROPERTY 2: once both ramps have saturated, more return is simply better. 80/40 is the
    'interesting' candidate and must top the table."""
    assert _score(80.0, 40.0) > _score(60.0, 45.0) > _score(50.0, 30.0)
    # and the discriminator: all three genuinely clear both targets, so this is the saturated
    # region and not an accident of one of the ramps still biting.
    for car, dd in ((80.0, 40.0), (60.0, 45.0), (50.0, 30.0)):
        assert car >= _OCT_CAR_TARGET and car / dd >= _OCT_MAR_TARGET


def test_drawdown_still_discriminates_above_the_ratio_threshold():
    """PROPERTY 3, and THE reason the knee sits at 40 rather than 50. Both of these clear the
    CAR target and the CAR >= DD ratio, so both ramps are saturated at 1.0 for each; without a
    penalty knee BELOW 45 they would tie at exactly 50.00 and the metric would be blind to 15
    points of extra drawdown."""
    safer = _score(50.0, 30.0)
    riskier = _score(50.0, 45.0)
    assert safer > riskier
    assert safer == pytest.approx(50.0)            # saturated: no ramp is biting
    assert _OCT_DD_TOLERANCE < 45.0                # the knee is what breaks the tie
    assert riskier == pytest.approx(50.0 * (_OCT_DD_TOLERANCE / 45.0) ** _OCT_DD_EXPONENT)


def test_over_safety_earns_nothing():
    """PROPERTY 4: 25% CAR at 10% DD has a CAR/DD ratio of 2.5 -- two and a half times what the
    metric asks for -- and still ranks BELOW a genome sitting exactly ON both thresholds,
    because neither ramp pays above its target and 25 is only 71% of the CAR one."""
    over_safe = _score(25.0, 10.0)
    on_target = _score(35.0, 35.0)
    assert over_safe < on_target
    # the discriminator: the over-safe genome has the FAR better ratio, and it buys nothing.
    assert (25.0 / 10.0) > (35.0 / 35.0)
    assert _option_car_target_factor(25.0, 10.0) == pytest.approx(25.0 / _OCT_CAR_TARGET)


def test_a_ninety_percent_drawdown_is_crushed_even_though_its_ratio_passes():
    """PROPERTY 5: 100% CAR at 90% DD has ratio 1.11, so the ratio term is fully satisfied and a
    pure CAR>DD test would call it a complete success. The high-drawdown penalty is what stops
    that, and it must drop the genome below the 50/30 one earning half the return."""
    reckless = _score(100.0, 90.0)
    target = _score(50.0, 30.0)
    assert (100.0 / 90.0) >= _OCT_MAR_TARGET       # the ratio term really does pass
    assert reckless < target
    assert reckless == pytest.approx(100.0 * (_OCT_DD_TOLERANCE / 90.0) ** _OCT_DD_EXPONENT,
                                     abs=0.005)


def test_the_ramps_are_soft_so_the_ga_still_has_a_gradient():
    """PROPERTY 6, and the reason neither ramp is a hard gate. The live run's leader (23.7% CAR
    at 44.4% DD) meets NEITHER target, and a hard gate would score it -- and every other genome
    in that population -- exactly 0, leaving the search a flat landscape and no way to tell
    'nearly there' from 'hopeless'. It must keep a real positive score."""
    leader = _score(23.7, 44.4)
    assert leader > 0.0
    assert leader not in (LOW_TRADE_SENTINEL, ZERO_TRADE_SENTINEL, WIPED_OUT_SENTINEL)
    # and the gradient is real: improving EITHER term alone must raise the score.
    assert _score(30.0, 44.4) > leader       # more CAR, same drawdown
    assert _score(23.7, 35.0) > leader       # same CAR, less drawdown
    # the whole live elite (ratios 0.18-0.53) stays scored, not zeroed.
    for car, dd in ((8.0, 44.0), (12.0, 40.0), (20.0, 42.0)):
        assert _score(car, dd) > 0.0, (car, dd)


# --------------------------------------------------------------------------------------------
# 3. THE SHAPE OF THE FACTORS, on their own
# --------------------------------------------------------------------------------------------
def test_the_car_ramp_is_linear_to_the_target_then_exactly_flat():
    """Isolated by taking dd == 0, where the ratio term is 1.0 by definition and the penalty is
    1.0, so the factor IS the CAR ramp and nothing else."""
    for base in (1.0, 8.0, 17.5, 34.999):
        assert _option_car_target_factor(base, 0.0) == pytest.approx(base / _OCT_CAR_TARGET)
    for base in (_OCT_CAR_TARGET, 35.001, 50.0, 100.0, 1000.0):
        assert _option_car_target_factor(base, 0.0) == pytest.approx(1.0)


def test_the_car_ramp_is_monotone_non_decreasing_then_flat():
    prev = None
    for tenth in range(1, 1501):
        base = tenth / 10.0
        f = _option_car_target_factor(base, 0.0)
        if prev is not None:
            assert f >= prev - 1e-15, base
        prev = f
    # strictly increasing below the target, exactly flat above it
    assert _option_car_target_factor(20.0, 0.0) < _option_car_target_factor(30.0, 0.0)
    assert (_option_car_target_factor(40.0, 0.0)
            == _option_car_target_factor(400.0, 0.0) == pytest.approx(1.0))


def test_the_mar_ramp_is_linear_in_the_ratio_then_exactly_flat():
    """Isolated at base == 20 (a constant CAR ramp of 20/35) and dd <= 40 (penalty exactly 1.0),
    so the only thing moving is the ratio term ``min(20/dd, 1)``."""
    car_ramp = 20.0 / _OCT_CAR_TARGET
    for dd in (40.0, 30.0, 25.0, 20.001):
        assert _option_car_target_factor(20.0, dd) == pytest.approx(car_ramp * (20.0 / dd))
    for dd in (20.0, 15.0, 5.0, 0.5):         # ratio >= 1: saturated, no further reward
        assert _option_car_target_factor(20.0, dd) == pytest.approx(car_ramp)


def test_the_mar_ramp_is_monotone_non_decreasing_in_the_ratio_then_flat():
    """Sweeping the drawdown DOWN raises the ratio; the factor must never fall as it does."""
    prev = None
    for tenth in range(400, 0, -1):           # dd 40.0 -> 0.1, penalty flat throughout
        dd = tenth / 10.0
        f = _option_car_target_factor(20.0, dd)
        if prev is not None:
            assert f >= prev - 1e-15, dd
        prev = f
    assert _option_car_target_factor(20.0, 10.0) == _option_car_target_factor(20.0, 1.0)


def test_a_zero_drawdown_treats_the_ratio_as_fully_satisfied_and_never_divides_by_zero():
    """A measured zero drawdown must not divide by zero. The ratio term is 1.0 -- FULLY
    satisfied -- because a run that never gave anything back has met "CAR > DD" as completely
    as a run can. 0.0 would rank a flawless curve worst; an infinite ratio is the same 1.0 after
    the ``min``."""
    for dd in (0.0, -0.0, -1e-12):
        assert _option_car_target_factor(50.0, dd) == pytest.approx(1.0)
        assert _option_car_target_factor(20.0, dd) == pytest.approx(20.0 / _OCT_CAR_TARGET)
    # end to end, through the real metric, with the engine's negative-zero spelling.
    assert compute_fitness(OCT, _r(car=50.0, max_drawdown=0.0)) == pytest.approx(50.0)
    assert compute_fitness(OCT, _r(car=20.0, max_drawdown=-0.0)) == pytest.approx(
        20.0 * 20.0 / _OCT_CAR_TARGET)


def test_the_penalty_is_exactly_one_up_to_the_tolerance_and_continuous_at_the_knee():
    for dd in (0.0, 5.0, 20.0, 39.999, _OCT_DD_TOLERANCE):
        assert _option_car_target_dd_penalty(dd) == 1.0
    just_below = _option_car_target_dd_penalty(_OCT_DD_TOLERANCE - 1e-6)
    just_above = _option_car_target_dd_penalty(_OCT_DD_TOLERANCE + 1e-6)
    assert just_below == pytest.approx(1.0)
    assert just_above == pytest.approx(1.0, rel=1e-6)
    assert just_above < 1.0                   # it really does start biting immediately above


def test_the_penalty_is_non_increasing_and_strictly_decreasing_above_the_knee():
    prev = None
    for tenth in range(0, 1001):
        dd = tenth / 10.0
        p = _option_car_target_dd_penalty(dd)
        if prev is not None:
            assert p <= prev + 1e-15, dd
        prev = p
    for dd in (40.0, 45.0, 50.0, 70.0, 90.0):
        assert _option_car_target_dd_penalty(dd + 0.5) < _option_car_target_dd_penalty(dd), dd
    for dd in (45.0, 50.0, 90.0):
        assert _option_car_target_dd_penalty(dd) == pytest.approx(
            (_OCT_DD_TOLERANCE / dd) ** _OCT_DD_EXPONENT)


def test_the_factors_read_the_magnitude_not_the_sign():
    """``max_drawdown`` is recorded negative; a positive spelling must score identically rather
    than inverting the shape."""
    for dd in (2.0, 12.0, 30.0, 50.0, 70.0):
        assert _option_car_target_dd_penalty(-dd) == _option_car_target_dd_penalty(dd)
        assert _option_car_target_factor(50.0, -dd) == _option_car_target_factor(50.0, dd)
    assert compute_fitness(OCT, _r(car=50.0, dd=30.0)) == pytest.approx(
        compute_fitness(OCT, _r(car=50.0, max_drawdown=30.0)))


def test_the_factor_product_is_exactly_the_three_terms():
    """The helper is the whole shape; assert it against the formula so a silently dropped term
    cannot hide behind a table row that happens to saturate it."""
    for base, dd in ((23.7, 44.4), (18.1, 46.4), (50.0, 45.0), (100.0, 90.0), (8.0, 4.0)):
        expected = (min(base / _OCT_CAR_TARGET, 1.0)
                    * min((base / dd) / _OCT_MAR_TARGET, 1.0)
                    * _option_car_target_dd_penalty(dd))
        assert _option_car_target_factor(base, dd) == pytest.approx(expected)


# --------------------------------------------------------------------------------------------
# 4. REGISTRATION
# --------------------------------------------------------------------------------------------
def test_the_name_resolves_and_is_case_insensitive():
    assert compute_fitness(OCT, _r()) == compute_fitness(OCT.upper(), _r())


def test_catalog_lists_the_metric_with_metadata():
    from app.services import strategy_fitness as sf

    sf.assert_catalog_complete()
    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    entry = by_key[OCT]
    assert set(entry["aliases"]) == set(_OCT_ALIASES) - {OCT}
    assert entry["supports_trade_scale"] is False    # the trade gate replaces it, as for CAR
    assert entry["supports_win_rate_factor"] is True
    assert entry["uses_adjusted_under_caps"] is True
    assert "option" in entry["label"].lower()
    assert set(_OCT_ALIASES) <= sf.catalog_accepted_metrics()


def test_unknown_metric_error_advertises_the_new_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert OCT in str(ei.value)


def test_the_new_name_is_disjoint_from_every_other_metrics_names():
    """A grid naming one metric must never reach another. All three option metrics now begin
    "option_car", so the alias sets are asserted disjoint rather than merely different."""
    from app.services.strategy_fitness import (
        _CAR_ALIASES, _CONVEX_ALIASES, _FITNESS_KEYS, _OCAR_ALIASES, _OCR_ALIASES,
    )

    assert set(_OCT_ALIASES).isdisjoint(set(_CAR_ALIASES))
    assert set(_OCT_ALIASES).isdisjoint(set(_OCAR_ALIASES))
    assert set(_OCT_ALIASES).isdisjoint(set(_OCR_ALIASES))
    assert set(_OCT_ALIASES).isdisjoint(set(_CONVEX_ALIASES))
    assert set(_OCT_ALIASES).isdisjoint(set(_FITNESS_KEYS))


# --------------------------------------------------------------------------------------------
# 5. EVERY INHERITED GUARD
# --------------------------------------------------------------------------------------------
def test_an_absent_drawdown_key_raises_rather_than_scoring_as_zero():
    r = _r()
    del r["max_drawdown"]
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCT, r)


def test_a_none_drawdown_raises():
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCT, _r(max_drawdown=None))


def test_a_non_numeric_drawdown_raises():
    with pytest.raises(ValueError, match="not numeric"):
        compute_fitness(OCT, _r(max_drawdown="a lot"))


def test_a_non_finite_drawdown_raises():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="not finite"):
            compute_fitness(OCT, _r(max_drawdown=bad))


def test_a_measured_total_loss_is_disqualified_regardless_of_return():
    assert compute_fitness(OCT, _r(car=3189.0, max_drawdown=-100.0)) == WIPED_OUT_SENTINEL
    assert compute_fitness(OCT, _r(max_drawdown=100.0)) == WIPED_OUT_SENTINEL   # magnitude
    assert compute_fitness(OCT, _r(max_drawdown=-141.7)) == WIPED_OUT_SENTINEL
    # 99.9% is a survivable catastrophe, not a dead account: penalised, not disqualified.
    survived = compute_fitness(OCT, _r(car=120.0, max_drawdown=-99.9))
    assert survived != WIPED_OUT_SENTINEL and survived > 0.0


def test_the_engine_wipeout_flag_is_still_honoured():
    assert compute_fitness(OCT, _r(account_wiped_out=True)) == WIPED_OUT_SENTINEL


def test_the_wipeout_check_runs_before_the_trade_gate_and_before_base():
    """THE ordering invariant, inherited verbatim (F9(a), 2026-08-30). A dd=150 genome with 3
    structures/yr AND a negative base must return WIPED_OUT_SENTINEL -- not LOW_TRADE_SENTINEL
    (-1e8, which numerically OUTRANKS it), and not the small negative that `base <= 0` would
    hand back (which outranks both sentinels).

    Asserted on the metric FUNCTION: ``compute_fitness``'s own entry guards would answer first
    for some of these dicts and hide which branch fired."""
    r = _r(car=-20.0, dd=150.0, avg_trades_per_year=3.0)
    assert _option_car_target(dict(r)) == WIPED_OUT_SENTINEL
    # each escape hatch on its own, too
    assert _option_car_target(_r(car=-20.0, dd=150.0)) == WIPED_OUT_SENTINEL
    assert _option_car_target(_r(dd=150.0, avg_trades_per_year=3.0)) == WIPED_OUT_SENTINEL
    # and ahead of the base derivation's own ZERO_TRADE_SENTINEL return
    assert _option_car_target(_r(dd=150.0, annualized_return=None)) == WIPED_OUT_SENTINEL
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0


def test_an_absent_or_degenerate_base_is_a_zero_trade_sentinel():
    assert _option_car_target(_r(annualized_return=None)) == ZERO_TRADE_SENTINEL
    assert _option_car_target(_r(annualized_return=float("nan"))) == ZERO_TRADE_SENTINEL
    assert _option_car_target(_r(annualized_return=float("inf"))) == ZERO_TRADE_SENTINEL


def test_an_underivable_trade_rate_disqualifies():
    """Asserted on the FUNCTION: ``compute_fitness`` rewrites a 0-trade dict to
    ZERO_TRADE_SENTINEL on the way in, which would hide which branch fired."""
    assert _option_car_target({"annualized_return": 30.0,
                               "max_drawdown": -20.0}) == LOW_TRADE_SENTINEL


def test_the_hard_trade_floor_and_the_ramp_are_inherited():
    assert compute_fitness(OCT, _r(avg_trades_per_year=11.9)) == LOW_TRADE_SENTINEL
    assert compute_fitness(OCT, _r(avg_trades_per_year=12.0)) != LOW_TRADE_SENTINEL
    # ramp: 15/30 of full credit, applied to base x factor
    at15 = compute_fitness(OCT, _r(car=50.0, dd=30.0, avg_trades_per_year=15.0))
    full = compute_fitness(OCT, _r(car=50.0, dd=30.0))
    assert at15 == pytest.approx(full * 0.5)


def test_the_per_run_cadence_overrides_are_inherited():
    r = _r(car=50.0, dd=30.0, avg_trades_per_year=8.0,
           car_hard_min_trades_per_year=4.0, car_min_trades_per_year=16.0)
    assert compute_fitness(OCT, r) == pytest.approx(
        compute_fitness(OCT, _r(car=50.0, dd=30.0)) * 0.5)


def test_a_negative_base_is_returned_unfactored():
    """Every factor is <= 1.0 here, so multiplying a loss by one would IMPROVE it."""
    assert compute_fitness(OCT, _r(car=-17.5, dd=30.0)) == pytest.approx(-17.5)
    assert compute_fitness(OCT, _r(car=-17.5, dd=12.0)) == pytest.approx(-17.5)
    assert compute_fitness(OCT, _r(car=0.0, dd=30.0)) == pytest.approx(0.0)


def test_the_adjusted_base_is_used_only_when_a_cap_is_active():
    uncapped = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0)
    assert compute_fitness(OCT, uncapped) == pytest.approx(
        50.0 * _option_car_target_factor(50.0, 30.0))
    capped = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0, profit_cap_pct=25.0)
    assert compute_fitness(OCT, capped) == pytest.approx(
        20.0 * _option_car_target_factor(20.0, 30.0))
    share = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0, profit_share_cap_pct=10.0)
    assert compute_fitness(OCT, share) == pytest.approx(
        20.0 * _option_car_target_factor(20.0, 30.0))


def test_a_missing_equity_curve_with_trades_still_raises_loudly():
    """Re-scoring a stored Backtest whose ``equity_curve`` column was not restored silently
    inflates the consistency factor to 1.0 (measured ~4x overstatement)."""
    r = _r()
    del r["equity_curve"]
    r["trades"] = [{"symbol": "AAPL", "contract_symbol": None, "transaction_id": 1}]
    with pytest.raises(ValueError, match="equity_curve"):
        compute_fitness(OCT, r)


def test_the_consistency_factor_is_applied():
    """An uneven year profile must cost, exactly as it does for the rest of the family --
    otherwise the term is silently inert and the metric is only ever base x factor."""
    uneven = _r(car=50.0, dd=30.0, equity_curve=[
        {"date": "2020-01-02", "equity": 100_000.0},
        {"date": "2020-12-31", "equity": 150_000.0},
        {"date": "2021-12-31", "equity": 165_000.0},
    ])
    even = _r(car=50.0, dd=30.0, equity_curve=[
        {"date": "2020-01-02", "equity": 100_000.0},
        {"date": "2020-12-31", "equity": 130_000.0},
        {"date": "2021-12-31", "equity": 169_000.0},
    ])
    assert compute_fitness(OCT, uneven) < compute_fitness(OCT, even)


def test_the_trade_gate_counts_structures_not_legs():
    """Inherited from ``_trades_per_year``: an iron condor books FOUR rows and is ONE bet. Three
    condors a year is 12 legs -- exactly the hard floor -- and must still be disqualified."""
    trades = []
    for t in range(3):
        trades += [{"symbol": "AAPL", "contract_symbol": f"C{t}L{k}", "transaction_id": t,
                    "pnl": 10.0, "exit_time": "2020-06-01"} for k in range(4)]
    r = _r(avg_trades_per_year=12.0, trades=trades, total_trades=12)
    assert compute_fitness(OCT, r) == LOW_TRADE_SENTINEL


def test_the_optional_wrappers_engage():
    """Win-rate factor and spread stress are wired the same way as for the other two option
    metrics."""
    plain = compute_fitness(OCT, _r(car=50.0, dd=30.0))
    scaled = compute_fitness(OCT, _r(car=50.0, dd=30.0,
                                     fitness_win_rate_factor=True, win_rate=25.0))
    assert scaled == pytest.approx(plain * 0.5)
    # trade-scale stays a structural no-op, as for the rest of the CAR family
    noop = compute_fitness(OCT, _r(car=50.0, dd=30.0, fitness_trade_scale=True,
                                   fitness_trade_scale_cap=100.0))
    assert noop == pytest.approx(plain)


# --------------------------------------------------------------------------------------------
# 6. NEITHER OLDER OPTION METRIC MOVED
# --------------------------------------------------------------------------------------------
def test_option_car_over_risk_is_bit_identical():
    """Grids ranked under it are banked; its numbers must not have shifted by adding a metric
    beside it. Literals, not a re-derivation from the implementation."""
    assert compute_fitness(OCR, _r(car=50.0, dd=30.0)) == pytest.approx(9.1287, abs=1e-4)
    assert compute_fitness(OCR, _r(car=25.0, dd=10.0)) == pytest.approx(7.9057, abs=1e-4)
    assert compute_fitness(OCR, _r(car=80.0, dd=40.0)) == pytest.approx(12.6491, abs=1e-4)


def test_option_consistent_annual_return_is_bit_identical():
    # base 30%/yr at the 20% reference: penalty exactly 1.0.
    assert compute_fitness(OCAR, _r(car=30.0, dd=20.0)) == pytest.approx(30.0)
    # 40% drawdown: (20/40)**2 == 0.25.
    assert compute_fitness(OCAR, _r(car=30.0, dd=40.0)) == pytest.approx(7.5)
    # the stopped job's winner, which is where this whole exercise started.
    assert compute_fitness(OCAR, _r(car=10.6, dd=8.9)) == pytest.approx(53.5286, abs=1e-4)


def test_the_equity_metric_did_not_move_either():
    assert compute_fitness("consistent_annual_return", _r(car=30.0, dd=20.0)) == pytest.approx(30.0)
    assert compute_fitness("consistent_annual_return", _r(car=30.0, dd=40.0)) == pytest.approx(15.0)


def test_the_four_metrics_are_genuinely_different_functions():
    """Same input, four different numbers -- so a grid that names the wrong one cannot get away
    with it silently, and so nobody puts two of these in one table."""
    r = _r(car=50.0, dd=30.0)
    scores = {m: compute_fitness(m, dict(r))
              for m in (OCT, OCR, OCAR, "consistent_annual_return")}
    assert len(set(round(v, 6) for v in scores.values())) == 4, scores


def test_this_metric_prices_the_ratio_and_car_over_risk_does_not():
    """The one-comparison statement of why a THIRD metric exists. Under
    ``option_car_over_risk`` a 40%/40% genome (ratio 1.00, this metric's target met exactly) and
    a 20%/10% one (ratio 2.00, half the return) score the SAME; under this metric they do not,
    and the one meeting the CAR target wins."""
    on_target = _r(car=40.0, dd=40.0)
    half_return = _r(car=20.0, dd=10.0)
    assert compute_fitness(OCR, dict(on_target)) == pytest.approx(
        compute_fitness(OCR, dict(half_return)), abs=1e-6)
    assert math.isclose(compute_fitness(OCR, dict(on_target)), 6.3246, abs_tol=1e-4)
    assert compute_fitness(OCT, dict(on_target)) > compute_fitness(OCT, dict(half_return))
