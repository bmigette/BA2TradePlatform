"""``option_car_over_risk``: the ~50%-CAR-with-a-drawdown-tolerance option metric.

WHY IT EXISTS. The operator's option objective is ~50% annualised return, WITH a real tolerance
for a larger drawdown. ``option_consistent_annual_return`` cannot express that: its drawdown term
is ``(20/max(dd,5))**2``, which pays up to 16x for a tiny drawdown, so it ranked a 10.6%-CAR /
8.9%-DD grinder about 2.4x ABOVE a 50%-CAR / 30%-DD genome. The first gated stage-1 job converged
on exactly those grinders (fitness 13.5) and was stopped. A search cannot find what its objective
punishes.

THE SHAPE:

    score = base / sqrt(max(|dd|, 10)) x high_dd_penalty(|dd|) x consistency x trade_gate
    high_dd_penalty(dd) = 1.0 if dd <= 40 else (40/dd) ** 1.5

Everything else -- the drawdown read-and-disqualify guard, the profit-cap base switch, the
STRUCTURES-per-year trade gate, the unfactored negative base, the missing-curve guard, the
calendar-year consistency factor -- is ``_option_consistent_annual_return``'s behaviour, copied
term for term. Section 5 pins each of those; section 6 pins that the OLD metric did not move.

THE TABLE IN SECTION 1 IS THE APPROVED RANKING (simulated and signed off 2026-09-17). It is the
specification, not an observation of the implementation: if a future change moves one of these
numbers, the change is wrong until the operator re-approves the table.
"""
import math

import pytest

from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _OCR_ALIASES,
    _OCR_DD_EXPONENT,
    _OCR_DD_FLOOR,
    _OCR_DD_TOLERANCE,
    _option_car_over_risk,
    _option_car_over_risk_dd_factor,
    compute_fitness,
)

OCR = "option_car_over_risk"
OCAR = "option_consistent_annual_return"

# ONE calendar year, so ``_calendar_year_returns`` yields fewer than two measurable years and
# ``consistency`` is exactly 1.0. Combined with 100 structures/yr (>= the 30/yr ramp target,
# gate exactly 1.0) every score below is ``base x dd_factor`` and nothing else -- which is what
# lets the table be asserted as absolute numbers rather than as ratios.
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
    score = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    assert score == pytest.approx(50.0 * _option_car_over_risk_dd_factor(30.0))


# --------------------------------------------------------------------------------------------
# 1. THE APPROVED TABLE
# --------------------------------------------------------------------------------------------
_APPROVED = [
    ("50% but safer",        50.0, 12.0, 14.43),
    ("interesting",          80.0, 40.0, 12.65),
    ("aggressive",          100.0, 50.0, 10.12),
    ("TARGET",               50.0, 30.0,  9.13),
    ("same, less return",    50.0, 40.0,  7.91),
    ("safe-moderate trap",   25.0, 10.0,  7.91),
    ("reckless",            120.0, 70.0,  6.20),
    ("solid low-DD",         20.0, 15.0,  5.16),
    ("today's winner",       10.6,  8.9,  3.35),
    ("micro-grinder",         5.0,  2.0,  1.58),
]


@pytest.mark.parametrize("label,car,dd,expected", _APPROVED,
                         ids=[row[0] for row in _APPROVED])
def test_the_approved_scores(label, car, dd, expected):
    assert compute_fitness(OCR, _r(car=car, dd=dd)) == pytest.approx(expected, abs=0.005)


def test_the_approved_table_is_in_descending_order():
    """The table is a RANKING, and the ranking is the thing that was approved. Asserting the
    order separately from the numbers catches a change that shifts every score by a constant
    (which the per-row assertions would also catch) as well as one that reorders two rows
    while leaving both inside their tolerance (which they would not)."""
    scores = [compute_fitness(OCR, _r(car=car, dd=dd)) for _, car, dd, _ in _APPROVED]
    assert scores == sorted(scores, reverse=True), list(zip([r[0] for r in _APPROVED], scores))


# --------------------------------------------------------------------------------------------
# 2. THE FOUR PROPERTIES THE OPERATOR ASKED FOR -- as ORDERINGS, not numbers
# --------------------------------------------------------------------------------------------
def test_more_return_inside_the_tolerance_outranks_the_target():
    """80% CAR at 40% DD is "interesting", not a disqualification: a drawdown at the tolerance
    is fully paid for, so the extra return must win."""
    interesting = compute_fitness(OCR, _r(car=80.0, dd=40.0))
    target = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    assert interesting > target


def test_the_reckless_genome_is_demoted_below_the_target_but_still_scores():
    """120% CAR at 70% DD earns 2.4x the target's return and must still rank BELOW it -- and
    must keep a real, positive score, so the GA has a gradient leading out of that region
    rather than a cliff it cannot see past."""
    reckless = compute_fitness(OCR, _r(car=120.0, dd=70.0))
    target = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    assert reckless < target
    assert reckless > 0.0
    assert reckless not in (LOW_TRADE_SENTINEL, ZERO_TRADE_SENTINEL, WIPED_OUT_SENTINEL)


def test_the_safe_moderate_trap_does_not_outrank_the_target():
    """25% CAR at 10% DD. THE reason the metric divides by sqrt(dd) rather than by dd: a pure
    ratio is scale-free, so Calmar ties these two exactly (2.5 vs 1.67 -- in fact it prefers
    the trap) and the GA would have no reason to climb toward 50%."""
    trap = compute_fitness(OCR, _r(car=25.0, dd=10.0))
    target = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    assert trap < target
    # And the discriminator: on a pure ratio the trap WINS, which is what this metric rejects.
    assert (25.0 / 10.0) > (50.0 / 30.0)


def test_todays_winner_ranks_near_last():
    """The 10.6% CAR / 8.9% DD genome the stopped job converged on. It must sit below every
    real candidate in the table -- everything except the micro-grinder."""
    winner = compute_fitness(OCR, _r(car=10.6, dd=8.9))
    others = {label: compute_fitness(OCR, _r(car=car, dd=dd))
              for label, car, dd, _ in _APPROVED if label not in ("today's winner",
                                                                  "micro-grinder")}
    assert all(v > winner for v in others.values()), others
    assert compute_fitness(OCR, _r(car=5.0, dd=2.0)) < winner


def test_the_old_metric_ranks_the_grinder_above_the_target_and_this_one_does_not():
    """The whole point, stated as one comparison. Under ``option_consistent_annual_return`` the
    grinder beats the 50%/30% target (which is why the job was stopped); under this metric it
    does not."""
    grinder = _r(car=10.6, dd=8.9)
    target = _r(car=50.0, dd=30.0)
    assert compute_fitness(OCAR, grinder) > compute_fitness(OCAR, target)
    assert compute_fitness(OCR, grinder) < compute_fitness(OCR, target)


# --------------------------------------------------------------------------------------------
# 3. THE SHAPE OF THE DRAWDOWN FACTOR, on its own
# --------------------------------------------------------------------------------------------
def test_factor_is_non_increasing_across_the_whole_drawdown_range():
    prev = None
    for tenth in range(0, 1001):
        dd = tenth / 10.0
        f = _option_car_over_risk_dd_factor(dd)
        if prev is not None:
            assert f <= prev + 1e-15, dd
        prev = f


def test_factor_is_exactly_flat_below_the_floor():
    """Below 10% a smaller drawdown buys NOTHING. Without this the factor is unbounded as
    dd -> 0 and a 5%-CAR micro-grinder buys its way to the top on thinness."""
    flat = 1.0 / math.sqrt(_OCR_DD_FLOOR)
    for dd in (0.0, 0.01, 1.0, 2.0, 5.0, 8.9, 9.999, _OCR_DD_FLOOR):
        assert _option_car_over_risk_dd_factor(dd) == pytest.approx(flat)
    assert _option_car_over_risk_dd_factor(_OCR_DD_FLOOR + 0.001) < flat


def test_factor_is_strictly_decreasing_above_the_floor():
    for dd in (10.0, 15.0, 20.0, 30.0, 39.9, 40.0, 50.0, 70.0, 90.0):
        assert _option_car_over_risk_dd_factor(dd + 0.5) < _option_car_over_risk_dd_factor(dd), dd


def test_the_penalty_is_exactly_one_at_the_tolerance_and_the_factor_is_continuous_there():
    """The knee must not be a step: at exactly 40% the penalty is (40/40)**1.5 == 1.0, so the
    factor equals the unpenalised 1/sqrt(40) and the two branches meet."""
    at = _option_car_over_risk_dd_factor(_OCR_DD_TOLERANCE)
    assert at == pytest.approx(1.0 / math.sqrt(_OCR_DD_TOLERANCE))
    just_below = _option_car_over_risk_dd_factor(_OCR_DD_TOLERANCE - 1e-6)
    just_above = _option_car_over_risk_dd_factor(_OCR_DD_TOLERANCE + 1e-6)
    assert just_below == pytest.approx(at, rel=1e-6)
    assert just_above == pytest.approx(at, rel=1e-6)


def test_the_penalty_bites_only_above_the_tolerance():
    """Below/at 40% the factor is exactly 1/sqrt(dd) -- full credit. Above it, strictly less."""
    for dd in (10.0, 20.0, 30.0, 40.0):
        assert _option_car_over_risk_dd_factor(dd) == pytest.approx(1.0 / math.sqrt(dd))
    for dd in (41.0, 50.0, 70.0, 99.0):
        assert _option_car_over_risk_dd_factor(dd) < 1.0 / math.sqrt(dd)
        assert _option_car_over_risk_dd_factor(dd) == pytest.approx(
            (1.0 / math.sqrt(dd)) * (_OCR_DD_TOLERANCE / dd) ** _OCR_DD_EXPONENT)


def test_the_factor_reads_the_magnitude_not_the_sign():
    """``max_drawdown`` is recorded negative; a positive spelling must score identically rather
    than inverting the shape."""
    for dd in (2.0, 12.0, 30.0, 50.0, 70.0):
        assert _option_car_over_risk_dd_factor(-dd) == _option_car_over_risk_dd_factor(dd)
    assert compute_fitness(OCR, _r(car=50.0, dd=30.0)) == pytest.approx(
        compute_fitness(OCR, _r(car=50.0, max_drawdown=30.0)))


def test_leverage_pays_up_to_the_tolerance_and_stops_paying_past_it():
    """WHAT THIS METRIC DOES WITH SIZE, stated honestly and pinned, because it is the OPPOSITE
    of what ``option_car`` does and is easy to mistake for the defect that metric exists to fix.

    Doubling contract count doubles BOTH the annualised return and the drawdown. Under
    ``base/sqrt(dd)`` the score then grows as sqrt(size) -- leverage PAYS -- and that is
    deliberate: it is the same property as "100% CAR at 50% DD ranks above the 50%/30% target".
    What bounds it is the tolerance, not the sqrt: past 40% the ``(40/dd)**1.5`` penalty decays
    faster than the score grows, so the curve turns over. The maximum is exactly AT the
    tolerance -- the metric's own statement of how much risk it is willing to buy return with.
    """
    sweep = {dd: compute_fitness(OCR, _r(car=dd, dd=dd))  # Calmar pinned at 1.0 throughout
             for dd in (12.0, 24.0, 36.0, 40.0, 48.0, 60.0, 96.0)}
    assert sweep[12.0] < sweep[24.0] < sweep[36.0] < sweep[40.0]   # leverage pays, by design
    assert sweep[40.0] > sweep[48.0] > sweep[60.0] > sweep[96.0]   # and stops, at the tolerance
    assert sweep[40.0] == pytest.approx(40.0 / math.sqrt(_OCR_DD_TOLERANCE))


# --------------------------------------------------------------------------------------------
# 4. REGISTRATION
# --------------------------------------------------------------------------------------------
def test_the_name_resolves_and_is_case_insensitive():
    assert compute_fitness(OCR, _r()) == compute_fitness(OCR.upper(), _r())


def test_catalog_lists_the_metric_with_metadata():
    from app.services import strategy_fitness as sf

    sf.assert_catalog_complete()
    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    entry = by_key[OCR]
    assert set(entry["aliases"]) == set(_OCR_ALIASES) - {OCR}
    assert entry["supports_trade_scale"] is False    # the trade gate replaces it, as for CAR
    assert entry["supports_win_rate_factor"] is True
    assert entry["uses_adjusted_under_caps"] is True
    assert "option" in entry["label"].lower()
    assert set(_OCR_ALIASES) <= sf.catalog_accepted_metrics()


def test_unknown_metric_error_advertises_the_new_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert OCR in str(ei.value)


def test_the_new_name_is_disjoint_from_every_other_metrics_names():
    """A grid naming one metric must never reach another. Overlapping spellings are how that
    happens, so the alias sets are asserted disjoint rather than merely different."""
    from app.services.strategy_fitness import (
        _CAR_ALIASES, _CONVEX_ALIASES, _FITNESS_KEYS, _OCAR_ALIASES,
    )

    assert set(_OCR_ALIASES).isdisjoint(set(_CAR_ALIASES))
    assert set(_OCR_ALIASES).isdisjoint(set(_OCAR_ALIASES))
    assert set(_OCR_ALIASES).isdisjoint(set(_CONVEX_ALIASES))
    assert set(_OCR_ALIASES).isdisjoint(set(_FITNESS_KEYS))


# --------------------------------------------------------------------------------------------
# 5. EVERY INHERITED GUARD
# --------------------------------------------------------------------------------------------
def test_an_absent_drawdown_key_raises_rather_than_scoring_as_zero():
    r = _r()
    del r["max_drawdown"]
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCR, r)


def test_a_none_drawdown_raises():
    with pytest.raises(ValueError, match="max_drawdown"):
        compute_fitness(OCR, _r(max_drawdown=None))


def test_a_non_numeric_drawdown_raises():
    with pytest.raises(ValueError, match="not numeric"):
        compute_fitness(OCR, _r(max_drawdown="a lot"))


def test_a_non_finite_drawdown_raises():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="not finite"):
            compute_fitness(OCR, _r(max_drawdown=bad))


def test_a_measured_total_loss_is_disqualified_regardless_of_return():
    assert compute_fitness(OCR, _r(car=3189.0, max_drawdown=-100.0)) == WIPED_OUT_SENTINEL
    assert compute_fitness(OCR, _r(max_drawdown=100.0)) == WIPED_OUT_SENTINEL   # magnitude
    assert compute_fitness(OCR, _r(max_drawdown=-141.7)) == WIPED_OUT_SENTINEL
    # 99.9% is a survivable catastrophe, not a dead account: penalised, not disqualified.
    survived = compute_fitness(OCR, _r(max_drawdown=-99.9))
    assert survived != WIPED_OUT_SENTINEL and survived > 0.0


def test_the_engine_wipeout_flag_is_still_honoured():
    assert compute_fitness(OCR, _r(account_wiped_out=True)) == WIPED_OUT_SENTINEL


def test_the_wipeout_check_runs_before_the_trade_gate_and_before_base():
    """THE ordering invariant, inherited verbatim (F9(a), 2026-08-30). A dd=150 genome with 3
    structures/yr AND a negative base must return WIPED_OUT_SENTINEL -- not LOW_TRADE_SENTINEL
    (-1e8, which numerically OUTRANKS it), and not the small negative that `base <= 0` would
    hand back (which outranks both sentinels).

    Asserted on the metric FUNCTION: ``compute_fitness``'s own entry guards would answer first
    for some of these dicts and hide which branch fired."""
    r = _r(car=-20.0, dd=150.0, avg_trades_per_year=3.0)
    assert _option_car_over_risk(dict(r)) == WIPED_OUT_SENTINEL
    # each escape hatch on its own, too
    assert _option_car_over_risk(_r(car=-20.0, dd=150.0)) == WIPED_OUT_SENTINEL
    assert _option_car_over_risk(_r(dd=150.0, avg_trades_per_year=3.0)) == WIPED_OUT_SENTINEL
    # and ahead of the base derivation's own ZERO_TRADE_SENTINEL return
    assert _option_car_over_risk(_r(dd=150.0, annualized_return=None)) == WIPED_OUT_SENTINEL
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0


def test_an_absent_or_degenerate_base_is_a_zero_trade_sentinel():
    assert _option_car_over_risk(_r(annualized_return=None)) == ZERO_TRADE_SENTINEL
    assert _option_car_over_risk(_r(annualized_return=float("nan"))) == ZERO_TRADE_SENTINEL
    assert _option_car_over_risk(_r(annualized_return=float("inf"))) == ZERO_TRADE_SENTINEL


def test_an_underivable_trade_rate_disqualifies():
    """Asserted on the FUNCTION: ``compute_fitness`` rewrites a 0-trade dict to
    ZERO_TRADE_SENTINEL on the way in, which would hide which branch fired."""
    assert _option_car_over_risk({"annualized_return": 30.0,
                                  "max_drawdown": -20.0}) == LOW_TRADE_SENTINEL


def test_the_hard_trade_floor_and_the_ramp_are_inherited():
    assert compute_fitness(OCR, _r(avg_trades_per_year=11.9)) == LOW_TRADE_SENTINEL
    assert compute_fitness(OCR, _r(avg_trades_per_year=12.0)) != LOW_TRADE_SENTINEL
    # ramp: 15/30 of full credit, applied to base x dd_factor
    at15 = compute_fitness(OCR, _r(car=50.0, dd=30.0, avg_trades_per_year=15.0))
    full = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    assert at15 == pytest.approx(full * 0.5)


def test_the_per_run_cadence_overrides_are_inherited():
    r = _r(car=50.0, dd=30.0, avg_trades_per_year=8.0,
           car_hard_min_trades_per_year=4.0, car_min_trades_per_year=16.0)
    assert compute_fitness(OCR, r) == pytest.approx(
        compute_fitness(OCR, _r(car=50.0, dd=30.0)) * 0.5)


def test_a_negative_base_is_returned_unfactored():
    """Every factor is <= 1.0 here, so multiplying a loss by one would IMPROVE it."""
    assert compute_fitness(OCR, _r(car=-17.5, dd=30.0)) == pytest.approx(-17.5)
    assert compute_fitness(OCR, _r(car=-17.5, dd=12.0)) == pytest.approx(-17.5)
    assert compute_fitness(OCR, _r(car=0.0, dd=30.0)) == pytest.approx(0.0)


def test_the_adjusted_base_is_used_only_when_a_cap_is_active():
    uncapped = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0)
    assert compute_fitness(OCR, uncapped) == pytest.approx(
        50.0 * _option_car_over_risk_dd_factor(30.0))
    capped = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0, profit_cap_pct=25.0)
    assert compute_fitness(OCR, capped) == pytest.approx(
        20.0 * _option_car_over_risk_dd_factor(30.0))
    share = _r(car=50.0, dd=30.0, adjusted_annualized_return=20.0, profit_share_cap_pct=10.0)
    assert compute_fitness(OCR, share) == pytest.approx(
        20.0 * _option_car_over_risk_dd_factor(30.0))


def test_a_missing_equity_curve_with_trades_still_raises_loudly():
    """Re-scoring a stored Backtest whose ``equity_curve`` column was not restored silently
    inflates the consistency factor to 1.0 (measured ~4x overstatement)."""
    r = _r()
    del r["equity_curve"]
    r["trades"] = [{"symbol": "AAPL", "contract_symbol": None, "transaction_id": 1}]
    with pytest.raises(ValueError, match="equity_curve"):
        compute_fitness(OCR, r)


def test_the_consistency_factor_is_applied():
    """An uneven year profile must cost, exactly as it does for CAR/option_car -- otherwise the
    term is silently inert and the metric is only ever base x dd_factor."""
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
    assert compute_fitness(OCR, uneven) < compute_fitness(OCR, even)


def test_the_trade_gate_counts_structures_not_legs():
    """Inherited from ``_trades_per_year``, and the defect the previous copy shipped with: an
    iron condor books FOUR rows and is ONE bet. Three condors a year is 12 legs -- exactly the
    hard floor -- and must still be disqualified."""
    trades = []
    for t in range(3):
        trades += [{"symbol": "AAPL", "contract_symbol": f"C{t}L{k}", "transaction_id": t,
                    "pnl": 10.0, "exit_time": "2020-06-01"} for k in range(4)]
    r = _r(avg_trades_per_year=12.0, trades=trades, total_trades=12)
    assert compute_fitness(OCR, r) == LOW_TRADE_SENTINEL


def test_the_optional_wrappers_engage():
    """Win-rate factor and spread stress are wired the same way as for ``option_car``."""
    plain = compute_fitness(OCR, _r(car=50.0, dd=30.0))
    scaled = compute_fitness(OCR, _r(car=50.0, dd=30.0,
                                     fitness_win_rate_factor=True, win_rate=25.0))
    assert scaled == pytest.approx(plain * 0.5)
    # trade-scale stays a structural no-op, as for the rest of the CAR family
    noop = compute_fitness(OCR, _r(car=50.0, dd=30.0, fitness_trade_scale=True,
                                   fitness_trade_scale_cap=100.0))
    assert noop == pytest.approx(plain)


# --------------------------------------------------------------------------------------------
# 6. THE OLD METRIC DID NOT MOVE
# --------------------------------------------------------------------------------------------
def test_option_consistent_annual_return_is_bit_identical():
    """``option_car`` grids are banked; its numbers must not have shifted by adding a metric
    beside it. Literals, not a re-derivation from the implementation."""
    # base 30%/yr at the 20% reference: penalty exactly 1.0.
    assert compute_fitness(OCAR, _r(car=30.0, dd=20.0)) == pytest.approx(30.0)
    # 40% drawdown: (20/40)**2 == 0.25.
    assert compute_fitness(OCAR, _r(car=30.0, dd=40.0)) == pytest.approx(7.5)
    # at/below the 5% floor the reward is capped at (20/5)**2 == 16.
    assert compute_fitness(OCAR, _r(car=30.0, dd=5.0)) == pytest.approx(480.0)
    assert compute_fitness(OCAR, _r(car=30.0, dd=1.0)) == pytest.approx(480.0)
    # the stopped job's winner, which is where this whole exercise started.
    assert compute_fitness(OCAR, _r(car=10.6, dd=8.9)) == pytest.approx(53.5286, abs=1e-4)


def test_the_equity_metric_did_not_move_either():
    assert compute_fitness("consistent_annual_return", _r(car=30.0, dd=20.0)) == pytest.approx(30.0)
    assert compute_fitness("consistent_annual_return", _r(car=30.0, dd=40.0)) == pytest.approx(15.0)


def test_the_three_metrics_are_genuinely_different_functions():
    """Same input, three different numbers -- so a grid that names the wrong one cannot get
    away with it silently, and so nobody puts two of these in one table."""
    r = _r(car=50.0, dd=30.0)
    scores = {m: compute_fitness(m, dict(r)) for m in (OCR, OCAR, "consistent_annual_return")}
    assert len(set(round(v, 6) for v in scores.values())) == 3, scores
