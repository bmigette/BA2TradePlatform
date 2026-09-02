"""FROZEN BASELINE + property pins for ``option_convex`` -- the convex-harvest fitness.

WHY THIS METRIC EXISTS (docs/superpowers/specs/2026-08-31-convex-harvest-grid-design.md §3).
A book of cheap far-OTM long-dated calls bleeds premium steadily and wins lumpily. The
CAR family (``consistent_annual_return`` / ``option_consistent_annual_return``) rewards
steady equity and punishes drawdown, so it would teach the GA to gut the convexity -- tight
stops, near-dated, low delta -- to fake smoothness. ``option_convex`` ranks on the one number
the thesis is about: did the winners beat the graveyard.

THE ORDER IS THE CONTRACT (F9(a) discipline). The metric evaluates, LITERALLY in this order:

    1. wipeout sentinel   (dd >= 100 -> WIPED_OUT_SENTINEL, before anything else is read)
    2. return term        (end-of-window total return, net of costs, UNCAPPED P&L based)
    3. drawdown penalty   (free below 50%, linear 50 -> 90, dead at 90)
    4. breadth floor      (>= 30 tickets/yr AND >= 20 distinct underlyings, else LOW_TRADE)
    5. telemetry          (hit rate, top-1/top-5 share -- RECORDED, never scored)

Every one of those steps has a mutation kill below; the ordering itself is pinned by
``wipeout_before_return_missing`` (swap steps 1 and 2 and it fails).

THE CAP-BINDING PIN (the 2026-09-01 amendment). Option grids run under ``equity_cap``. The
CAPPED (deployed) equity series -- ``min(cap, equity)`` -- reports ZERO P&L for every period
spent above the cap, so a 5x convex winner would read as nothing: exactly the outcome this
metric exists to find, masked. The return term therefore reads the run's uncapped P&L (via
``results["total_return"]``, which ``build_results`` derives from ``equity_cap.scoring_curve``)
and the drawdown reads the ``capped_drawdown_curve`` definition. See
``test_a_capped_run_ranks_on_uncapped_pnl_not_on_the_capped_equity_series``.

The GOLDEN literals are HAND-DERIVED from the formula (base return x drawdown factor, with the
constants below), not captured from the code under test. ``repr()`` round-trip equality is a
BIT comparison -- ``float(repr(x)) == x`` is exact in CPython.

If you change this metric ON PURPOSE, re-derive the literals in the same commit and say so in
the message. Do not loosen a comparison to make a case pass.
"""
import math

import pytest

from app.services.backtest.equity_cap import (
    capped_drawdown_curve, deployed_equity, scoring_curve,
)
from app.services.strategy_fitness import (
    LOW_TRADE_SENTINEL,
    WIPED_OUT_SENTINEL,
    ZERO_TRADE_SENTINEL,
    _CONVEX_ALIASES,
    _CONVEX_DD_DEAD_PCT,
    _CONVEX_DD_FREE_PCT,
    _CONVEX_MIN_TICKETS_PER_YEAR,
    _CONVEX_MIN_UNDERLYINGS,
    _CONVEX_WIPED_OUT_DD_PCT,
    _convex_dd_factor,
    _structure_count,
    _structure_pnls,
    compute_fitness,
)

CONVEX = "option_convex"


# ---------------------------------------------------------------------------------------------
# Fixtures: a convex-shaped ticket book
# ---------------------------------------------------------------------------------------------
#: index -> P&L for the WINNERS; every other ticket is a -100 premium write-off. Nine winners in
#: ninety tickets (10% hit rate) carrying 22,400 against 8,100 of graveyard -- net 14,300, of
#: which the single best ticket is 83.9% and the best five are 146.2%. That top-5 figure is over
#: 100% BY DESIGN (the book is net negative without those five), which is precisely the profile
#: the concentration screen would kill and this metric records instead of scoring.
_WINNERS = {0: 12_000.0, 7: 4_000.0, 23: 2_500.0, 41: 1_500.0, 58: 900.0,
            66: 600.0, 74: 400.0, 81: 300.0, 88: 200.0}


def _rows(n=90, n_underlyings=24, legs_per_structure=1):
    """``n`` trade ROWS over ``n_underlyings`` names, ``legs_per_structure`` rows per bet.

    With ``legs_per_structure`` > 1 the rows carry a shared ``transaction_id`` plus a
    ``contract_symbol``, which is exactly what ``_structure_count`` collapses into ONE ticket.
    """
    rows = []
    for i in range(n):
        struct = i // legs_per_structure
        u = f"U{struct % n_underlyings + 1:02d}"
        pnl = _WINNERS[i] if i in _WINNERS else -100.0
        rows.append({
            "symbol": u,
            "underlying_symbol": u,
            "pnl": pnl,
            "pnl_pct": pnl / 100.0,
            "exit_time": f"{2023 + i // 40}-{1 + (i % 12):02d}-15",
            "contract_symbol": (f"{u}250117C00100000" if legs_per_structure > 1 else None),
            "transaction_id": (struct if legs_per_structure > 1 else None),
        })
    return rows


_TRADES = _rows()                       # 90 single-leg tickets over 24 underlyings
_TRADES_20 = _rows(n_underlyings=20)    # exactly AT the underlying floor
_TRADES_19 = _rows(n_underlyings=19)    # one under it
_TRADES_25 = _rows(n_underlyings=25)
_TRADES_3 = _rows(n_underlyings=3)
_LEGGED = _rows(n=88, n_underlyings=22, legs_per_structure=4)   # 22 four-leg tickets


def _two_leg_rows():
    """30 TWO-LEG tickets, one per underlying, built so per-ticket and per-LEG telemetry differ
    in every figure. See ``test_telemetry_counts_tickets_not_legs`` for the arithmetic.

      ticket  0      legs +3,000 / +2,000   -> net +5,000   (a winner SPLIT across its legs)
      tickets 1-4    legs   +300 /   -100   -> net   +200   (a winner with a LOSING leg)
      tickets 5-29   legs   -100 /   -100   -> net   -200   (the graveyard)
    """
    rows = []
    for s in range(30):
        if s == 0:
            legs = (3_000.0, 2_000.0)
        elif s < 5:
            legs = (300.0, -100.0)
        else:
            legs = (-100.0, -100.0)
        u = f"V{s + 1:02d}"
        for k, pnl in enumerate(legs):
            rows.append({
                "symbol": u,
                "underlying_symbol": u,
                "pnl": pnl,
                "pnl_pct": pnl / 100.0,
                "exit_time": f"2024-{1 + s % 12:02d}-15",
                "contract_symbol": f"{u}250117{'C' if k == 0 else 'P'}00100000",
                "transaction_id": 1_000 + s,
            })
    return rows


_TWO_LEG = _two_leg_rows()   # 60 rows / 30 tickets / 30 underlyings

_CURVE = [{"date": "2023-01-03", "equity": 20_000.0},
          {"date": "2023-12-29", "equity": 22_000.0},
          {"date": "2024-12-31", "equity": 30_000.0},
          {"date": "2025-12-31", "equity": 44_000.0}]


def _base(**kw):
    """A run that CLEARS every floor: 30 tickets/yr, 24 underlyings, dd 20%, +120% return."""
    d = {"total_trades": 90,
         "avg_trades_per_year": 30.0,
         "total_return": 120.0,
         "max_drawdown": -20.0,
         "trades": _TRADES,
         "equity_curve": _CURVE,
         "win_rate": 10.0}
    d.update(kw)
    return d


def _without(key, **kw):
    return {k: v for k, v in _base(**kw).items() if k != key}


CORPUS = {
    # --- 1. wipeout sentinel, FIRST -----------------------------------------------------------
    "dd_100": _base(max_drawdown=-100.0),
    "dd_120": _base(max_drawdown=-120.0),
    "wipeout_beats_huge_return": _base(max_drawdown=-100.0, total_return=5_000.0),
    # ORDERING PIN: no return to read at all. Checking the wipeout AFTER the return term would
    # return ZERO_TRADE_SENTINEL (-1e9) here -- numerically ABOVE WIPED_OUT_SENTINEL (-2e9).
    "wipeout_before_return_missing": _without("total_return", max_drawdown=-100.0),
    # ORDERING PIN: a wipeout that also fails the breadth floor must still rank WORST, not at
    # LOW_TRADE_SENTINEL (-1e8), which outranks both other sentinels.
    "wipeout_before_breadth": _base(max_drawdown=-100.0, avg_trades_per_year=1.0,
                                    trades=_TRADES_3),
    "dd_none": _base(max_drawdown=None),
    "dd_absent": _without("max_drawdown"),
    "dd_nan": _base(max_drawdown=float("nan")),
    "dd_inf": _base(max_drawdown=float("inf")),
    "dd_text": _base(max_drawdown="not-a-number"),

    # --- 2. return term -----------------------------------------------------------------------
    "plain": _base(),
    "big_convex_winner": _base(total_return=480.0, max_drawdown=-45.0),
    "return_missing": _without("total_return"),
    "return_none": _base(total_return=None),
    "return_nan": _base(total_return=float("nan")),
    "return_inf": _base(total_return=float("inf")),
    # A negative return is returned UNFACTORED: multiplying it by a <1 drawdown factor would
    # IMPROVE a losing book (the classic sign-flip bug in penalty schemes).
    "negative_return_low_dd": _base(total_return=-40.0),
    "negative_return_high_dd": _base(total_return=-40.0, max_drawdown=-70.0),
    "zero_return": _base(total_return=0.0, max_drawdown=-70.0),

    # --- 3. drawdown penalty ------------------------------------------------------------------
    "dd_zero": _base(max_drawdown=0.0),
    "dd_49_9": _base(max_drawdown=-49.9),
    "dd_at_free_50": _base(max_drawdown=-50.0),
    "dd_60": _base(max_drawdown=-60.0),
    "dd_70": _base(max_drawdown=-70.0),
    "dd_80": _base(max_drawdown=-80.0),
    "dd_at_dead_90": _base(max_drawdown=-90.0),
    "dd_95": _base(max_drawdown=-95.0),
    "dd_99_99": _base(max_drawdown=-99.99),
    "dd_positive_sign": _base(max_drawdown=70.0),

    # --- 4. breadth floor (AND, not OR) -------------------------------------------------------
    "at_breadth_floor": _base(avg_trades_per_year=30.0, trades=_TRADES_20),
    "tickets_29_underlyings_25": _base(avg_trades_per_year=29.0, trades=_TRADES_25),
    "tickets_40_underlyings_19": _base(avg_trades_per_year=40.0, trades=_TRADES_19),
    "both_below_floor": _base(avg_trades_per_year=5.0, trades=_TRADES_3),
    "trades_empty": _base(trades=[]),
    "trades_key_absent": _without("trades"),
    "no_trade_frequency_data": {k: v for k, v in _base().items()
                                if k not in ("avg_trades_per_year", "equity_curve")},
    # STRUCTURES, not legs: 88 rows / 22 four-leg tickets. The LEG rate 120/yr is 30 tickets/yr.
    "structures_not_legs_pass": _base(avg_trades_per_year=120.0, trades=_LEGGED,
                                      total_trades=88),
    # The same book at a 100/yr LEG rate is 25 tickets/yr -- under the floor. Counting legs
    # would let it through.
    "structures_not_legs_fail": _base(avg_trades_per_year=100.0, trades=_LEGGED,
                                      total_trades=88),

    # --- 5. wrappers this metric deliberately does NOT apply ----------------------------------
    # Concentration/hit-rate/adjusted-return machinery all penalise the convex shape itself.
    "adjusted_ignored_basis_cap": _base(profit_cap_pct=2_000.0, adjusted_total_return=55.0),
    "adjusted_ignored_share_cap": _base(profit_share_cap_pct=25.0, adjusted_total_return=55.0),
    "win_rate_factor_ignored": _base(fitness_win_rate_factor=True, win_rate=20.0),
    "robust_fitness_ignored": _base(robust_fitness=True, initial_capital=20_000.0),
    "stress_spread_ignored": _base(stress_spread_bps=40.0, initial_capital=20_000.0),

    # --- compute_fitness entry guards ---------------------------------------------------------
    "zero_trades": _base(total_trades=0),
    "account_wiped": _base(account_wiped_out=True),
}

METRICS = ["option_convex", "Option_Convex", "OPTION_CONVEX"]

#: "<corpus case>|<metric>" -> repr() of the expected fitness, DERIVED BY HAND from
#: ``return x dd_factor`` with dd_factor = 1 below 50%, 1 - (dd-50)/40 on [50, 90], 0 above.
#: "RAISES:<ExceptionType>" records a case that must raise.
_EXPECTED = {
    "dd_100": "-2000000000.0",
    "dd_120": "-2000000000.0",
    "wipeout_beats_huge_return": "-2000000000.0",
    "wipeout_before_return_missing": "-2000000000.0",
    "wipeout_before_breadth": "-2000000000.0",
    "dd_none": "RAISES:ValueError",
    "dd_absent": "RAISES:ValueError",
    "dd_nan": "RAISES:ValueError",
    "dd_inf": "RAISES:ValueError",
    "dd_text": "RAISES:ValueError",

    "plain": "120.0",
    "big_convex_winner": "480.0",
    "return_missing": "-1000000000.0",
    "return_none": "-1000000000.0",
    "return_nan": "-1000000000.0",
    "return_inf": "-1000000000.0",
    "negative_return_low_dd": "-40.0",
    "negative_return_high_dd": "-40.0",
    "zero_return": "0.0",

    "dd_zero": "120.0",
    "dd_49_9": "120.0",
    "dd_at_free_50": "120.0",
    "dd_60": "90.0",          # 120 x 0.75
    "dd_70": "60.0",          # 120 x 0.50
    "dd_80": "30.0",          # 120 x 0.25
    "dd_at_dead_90": "0.0",
    "dd_95": "0.0",
    "dd_99_99": "0.0",
    "dd_positive_sign": "60.0",   # magnitude read, same as dd_70

    "at_breadth_floor": "120.0",
    "tickets_29_underlyings_25": "-100000000.0",
    "tickets_40_underlyings_19": "-100000000.0",
    "both_below_floor": "-100000000.0",
    "trades_empty": "-100000000.0",
    "trades_key_absent": "RAISES:ValueError",
    "no_trade_frequency_data": "-100000000.0",
    "structures_not_legs_pass": "120.0",
    "structures_not_legs_fail": "-100000000.0",

    "adjusted_ignored_basis_cap": "120.0",
    "adjusted_ignored_share_cap": "120.0",
    "win_rate_factor_ignored": "120.0",
    "robust_fitness_ignored": "120.0",
    "stress_spread_ignored": "120.0",

    "zero_trades": "-1000000000.0",
    "account_wiped": "-2000000000.0",
}

#: Every alias/spelling must produce the same number -- the metric name is case-insensitive.
GOLDEN = {f"{case}|{m}": expected
          for case, expected in _EXPECTED.items()
          for m in METRICS}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_the_convex_fitness_is_bit_identical_to_the_frozen_baseline(case):
    name, metric = case.split("|")
    try:
        got = repr(compute_fitness(metric, dict(CORPUS[name])))
    except Exception as e:  # noqa: BLE001 -- a raise is a frozen outcome too
        got = f"RAISES:{type(e).__name__}"
    assert got == GOLDEN[case], f"option_convex MOVED for {name} @ {metric}"


def test_the_corpus_covers_every_branch_of_the_metric():
    """A branch the corpus does not reach is a change this freeze cannot see."""
    required = {
        "dd_100",                        # step 1: the sentinel
        "wipeout_before_return_missing",  # step 1 BEFORE step 2
        "wipeout_before_breadth",        # step 1 BEFORE step 4
        "dd_none", "dd_nan", "dd_text",  # the unmeasurable-drawdown raises
        "return_missing", "return_nan",  # step 2's degenerate returns
        "negative_return_high_dd",       # the sign guard
        "dd_zero", "dd_at_free_50", "dd_70", "dd_at_dead_90", "dd_95",  # every dd region
        "dd_positive_sign",              # the magnitude read
        "at_breadth_floor",              # both floors exactly met
        "tickets_29_underlyings_25",     # the AND, ticket side
        "tickets_40_underlyings_19",     # the AND, underlying side
        "trades_key_absent",             # the half-restored-DB-row guard
        "no_trade_frequency_data",
        "structures_not_legs_pass", "structures_not_legs_fail",
        "adjusted_ignored_basis_cap", "win_rate_factor_ignored", "robust_fitness_ignored",
        "zero_trades", "account_wiped",
    }
    assert required <= set(CORPUS)


# ---------------------------------------------------------------------------------------------
# THE CAP-BINDING PIN (2026-09-01 amendment)
# ---------------------------------------------------------------------------------------------
_CAP = 20_000.0

#: A $100k account under a $20k cap: the cap binds from bar zero, so the DEPLOYED series
#: ``min(cap, equity)`` is flat at 20,000 for the whole run and reports zero P&L for both runs.
_BIG_WINNER = [("2023-01-03", 100_000.0), ("2023-12-29", 96_000.0),
               ("2024-12-31", 120_000.0), ("2025-12-31", 180_000.0)]   # +80,000 = 400% of cap
_SMALL_WINNER = [("2023-01-03", 100_000.0), ("2023-12-29", 96_000.0),
                 ("2024-12-31", 102_000.0), ("2025-12-31", 108_000.0)]  # +8,000 = 40% of cap


def _capped_results(real_points, **kw):
    """Build a results dict the way ``results.build_results`` does under an ``equity_cap``.

    The REAL recorded curve goes through ``equity_cap``'s own helpers -- ``scoring_curve`` for
    the scored series (period P&L / the FIXED cap, compounded) and ``capped_drawdown_curve``
    for the drawdown (peak-to-trough on cumulative P&L / the cap). Neither is the DEPLOYED
    series, which never reaches a results dict at all.
    """
    real = [{"date": d, "equity": e} for d, e in real_points]
    dd_curve = capped_drawdown_curve(real, cap=_CAP)
    scored = scoring_curve(real, cap=_CAP)
    d = {"total_trades": 90,
         "avg_trades_per_year": 30.0,
         "total_return": (scored[-1]["equity"] - _CAP) / _CAP * 100.0,
         "max_drawdown": min(p["drawdown"] for p in dd_curve),
         "trades": _TRADES,
         "equity_curve": scored,
         "drawdown_curve": dd_curve,
         "_real_curve": real}
    d.update(kw)
    return d


def test_the_cap_binding_fixture_really_binds():
    """Guard on the guard: if the cap did not bind, the mutation below could not be detected."""
    for pts in (_BIG_WINNER, _SMALL_WINNER):
        for _d, e in pts:
            assert deployed_equity(e, cap=_CAP) == _CAP, "cap must bind on every bar"
    # ...and the DEPLOYED series is therefore flat: read that way, BOTH runs made nothing.
    for pts in (_BIG_WINNER, _SMALL_WINNER):
        capped = [deployed_equity(e, cap=_CAP) for _d, e in pts]
        assert capped[-1] - capped[0] == 0.0


def test_a_capped_run_ranks_on_uncapped_pnl_not_on_the_capped_equity_series():
    """THE AMENDMENT'S PIN. Two runs under a binding equity cap: +400% of the cap in uncapped
    P&L against +40%. The convex winner MUST score above the modest one.

    Rank on the capped (deployed) equity series instead and both read as zero P&L, so this
    assertion fails -- which is the whole point: capped equity masks exactly the 5x outcomes
    this metric exists to find.
    """
    big = compute_fitness(CONVEX, _capped_results(_BIG_WINNER))
    small = compute_fitness(CONVEX, _capped_results(_SMALL_WINNER))
    assert big > small
    assert big > 100.0, "a 400%-of-cap winner must not read as a small number"


def test_the_drawdown_term_uses_the_capped_drawdown_curve_definition():
    """Peak-to-trough on cumulative P&L / cap -- NOT peak-to-trough on the capped equity
    series, which is flat here and would report a risk-free run."""
    r = _capped_results(_BIG_WINNER)
    assert r["max_drawdown"] == pytest.approx(-20.0)      # -4,000 trough on a 20,000 cap
    capped_series = [deployed_equity(p["equity"], cap=_CAP) for p in r["_real_curve"]]
    assert max(capped_series) == min(capped_series)       # flat -> would say dd == 0
    # The metric prices the real 20% dip (free, below the 50% threshold) and still ranks the
    # run on its uncapped return.
    assert compute_fitness(CONVEX, r) == pytest.approx(r["total_return"])


# ---------------------------------------------------------------------------------------------
# PROPERTY PINS
# ---------------------------------------------------------------------------------------------
def test_score_is_strictly_monotone_in_return_above_the_breadth_floor():
    scores = [compute_fitness(CONVEX, _base(total_return=r))
              for r in (1.0, 10.0, 50.0, 120.0, 400.0, 1_000.0, 5_000.0)]
    assert scores == sorted(scores)
    assert len(set(scores)) == len(scores)


def test_the_drawdown_penalty_is_zero_below_the_free_threshold():
    for dd in (0.0, 1.0, 10.0, 25.0, 49.0, 49.999, _CONVEX_DD_FREE_PCT):
        assert _convex_dd_factor(-dd) == 1.0
        assert compute_fitness(CONVEX, _base(max_drawdown=-dd)) == pytest.approx(120.0)


def test_the_drawdown_penalty_is_strictly_increasing_between_the_two_thresholds():
    dds = [50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 89.999]
    factors = [_convex_dd_factor(-d) for d in dds]
    assert factors == sorted(factors, reverse=True)
    assert len(set(factors)) == len(factors)
    assert factors[0] == 1.0                      # continuous with the free region at 50
    assert 0.0 < factors[-1] < 0.001              # ...and reaching 0 at the dead threshold


def test_the_90_to_100_band_is_fully_penalised_but_is_not_a_sentinel():
    """Explicit: between the dead threshold and the wipeout, a WINNING book keeps nothing of
    its return -- but it is still a scored run, ranked above every disqualification."""
    for dd in (_CONVEX_DD_DEAD_PCT, 92.0, 95.0, 99.999):
        s = compute_fitness(CONVEX, _base(max_drawdown=-dd))
        assert s == 0.0
        assert s > WIPED_OUT_SENTINEL and s > ZERO_TRADE_SENTINEL and s > LOW_TRADE_SENTINEL
    # and at exactly 100 it flips to the sentinel
    assert compute_fitness(CONVEX, _base(max_drawdown=-_CONVEX_WIPED_OUT_DD_PCT)) \
        == WIPED_OUT_SENTINEL


def test_the_wipeout_sentinel_dominates_any_return():
    for r in (-500.0, 0.0, 100.0, 5_000.0, 1e6):
        assert compute_fitness(CONVEX, _base(total_return=r, max_drawdown=-100.0)) \
            == WIPED_OUT_SENTINEL
    # ...and ranks below every other outcome the metric can produce.
    assert WIPED_OUT_SENTINEL < ZERO_TRADE_SENTINEL < LOW_TRADE_SENTINEL < 0.0
    assert WIPED_OUT_SENTINEL < compute_fitness(CONVEX, _base(total_return=-9_999.0))


def test_the_breadth_floor_is_an_AND_not_an_or():
    """Design §3.4: >= 30 tickets/yr AND >= 20 distinct underlyings. Either one alone is not
    enough -- a convex result built on a handful of names is a coin flip, not a strategy."""
    # tickets short, underlyings fine
    assert compute_fitness(CONVEX, _base(avg_trades_per_year=29.0, trades=_TRADES_25)) \
        == LOW_TRADE_SENTINEL
    # underlyings short, tickets fine
    assert compute_fitness(CONVEX, _base(avg_trades_per_year=40.0, trades=_TRADES_19)) \
        == LOW_TRADE_SENTINEL
    # both exactly at the floor -> scored
    assert compute_fitness(CONVEX, _base(avg_trades_per_year=_CONVEX_MIN_TICKETS_PER_YEAR,
                                         trades=_rows(n_underlyings=_CONVEX_MIN_UNDERLYINGS))) \
        == pytest.approx(120.0)


def test_a_missing_trade_list_raises_rather_than_silently_disqualifying():
    """Re-scoring a stored Backtest whose ``trades`` column was not restored would otherwise
    sentinel EVERY genome, which reads as 'the strategy never traded'."""
    with pytest.raises(ValueError, match="trades"):
        compute_fitness(CONVEX, _without("trades"))


# ---------------------------------------------------------------------------------------------
# TELEMETRY: recorded, never scored
# ---------------------------------------------------------------------------------------------
def test_telemetry_is_recorded_on_the_results_payload():
    """ARITHMETIC, by hand, on ``_TRADES`` (90 single-leg tickets, so tickets == rows here):

      winners   12,000 + 4,000 + 2,500 + 1,500 + 900 + 600 + 400 + 300 + 200 = 22,400 (9 of 90)
      graveyard 81 x -100                                                    = -8,100
      net                                                                    = 14,300
      hit rate  9 / 90                                                       = 10%
      top-1     12,000 / 14,300                                              = 83.916...%
      top-5     (12,000 + 4,000 + 2,500 + 1,500 + 900) / 14,300 = 20,900 / 14,300 = 146.153...%
    """
    r = _base()
    compute_fitness(CONVEX, r)
    t = r["convex_telemetry"]
    assert t["hit_rate_pct"] == pytest.approx(100.0 * 9 / 90)
    assert t["top1_pct"] == pytest.approx(100.0 * 12_000 / 14_300)
    assert t["top5_pct"] == pytest.approx(100.0 * 20_900 / 14_300)
    assert t["tickets_per_year"] == pytest.approx(30.0)
    assert t["distinct_underlyings"] == 24
    assert t["tickets_scored"] == 90


def test_telemetry_counts_tickets_not_legs():
    """THE UNIT IS THE TICKET (STRUCTURE), the same unit ``tickets_per_year`` is measured in.

    ``_TWO_LEG`` is 30 two-leg tickets = 60 rows, net +800. Counted correctly and counted per
    LEG the two readings disagree on every figure:

                        per TICKET (correct)              per LEG (wrong)
      tickets_scored    30                                60
      winners           5  (ticket 0 + tickets 1-4)       6  (+3,000, +2,000, 4 x +300)
      hit rate          5 / 30      = 16.666...%          6 / 60      = 10%
      largest bet       +5,000 (ticket 0, legs summed)    +3,000 (its bigger leg alone)
      top-1             5,000 / 800 = 625%                3,000 / 800 = 375%
      top-5             5,800 / 800 = 725%                5,900 / 800 = 737.5%

    Top-1 is where the defect bites and in which direction: splitting a winner across its legs
    UNDERSTATES how much of the book one bet was -- 375% instead of 625% -- so a concentration
    reading built on rows makes a dangerously skewed result look tamer than it is.
    """
    # leg rate 60/yr over 30 tickets = 30 tickets/yr, exactly at the breadth floor
    r = _base(avg_trades_per_year=60.0, trades=_TWO_LEG, total_trades=60)
    assert compute_fitness(CONVEX, r) == pytest.approx(120.0)   # clears both floors
    t = r["convex_telemetry"]
    assert t["tickets_scored"] == 30
    assert t["tickets_per_year"] == pytest.approx(30.0)
    assert t["distinct_underlyings"] == 30
    assert t["hit_rate_pct"] == pytest.approx(100.0 * 5 / 30)
    assert t["top1_pct"] == pytest.approx(625.0)
    assert t["top5_pct"] == pytest.approx(725.0)
    # ...and explicitly NOT the per-leg answers, so a regression to row counting is named.
    assert t["hit_rate_pct"] != pytest.approx(10.0)
    assert t["top1_pct"] != pytest.approx(375.0)
    assert t["top5_pct"] != pytest.approx(737.5)


def test_the_telemetry_unit_matches_the_breadth_floor_unit():
    """``tickets_scored`` x (years spanned) must be the same population ``tickets_per_year``
    rates. Both come from the structure partition; a telemetry block that counted legs while
    the floor counted structures would report two different books under one heading."""
    r = _base(avg_trades_per_year=120.0, trades=_LEGGED, total_trades=88)
    compute_fitness(CONVEX, r)
    t = r["convex_telemetry"]
    assert t["tickets_scored"] == 22            # 88 rows / 4 legs each
    assert t["tickets_per_year"] == pytest.approx(30.0)   # 120 leg-rate x 22/88


@pytest.mark.parametrize("trades", [_TRADES, _TRADES_20, _LEGGED, _TWO_LEG, [],
                                    [{"pnl": 1.0}, "not-a-dict", {"pnl": 2.0}]],
                         ids=["single_leg", "at_floor", "four_leg", "two_leg", "empty", "junk"])
def test_structure_pnls_partitions_exactly_as_structure_count(trades):
    """Drift guard: the P&L grouping and the ticket TALLY must not diverge -- they are the same
    partition, and the ticket rate is derived from the tally while the shares are derived from
    the grouping."""
    assert len(_structure_pnls(trades)) == _structure_count(trades)


def test_structure_pnls_sums_the_legs_of_one_ticket():
    assert sorted(_structure_pnls(_TWO_LEG), reverse=True)[:5] \
        == [5_000.0, 200.0, 200.0, 200.0, 200.0]
    assert sum(_structure_pnls(_TWO_LEG)) == pytest.approx(800.0)
    # an option leg with NO transaction id is UNKNOWN identity, never a shared one
    orphans = [{"pnl": 5.0, "contract_symbol": "X", "transaction_id": None},
               {"pnl": 7.0, "contract_symbol": "X", "transaction_id": None}]
    assert _structure_pnls(orphans) == [5.0, 7.0]


def test_telemetry_never_moves_the_score():
    """Two books with the SAME return, drawdown and breadth but opposite concentration must
    score identically. The 2026-08-06 standing decision: skew is a legitimate profile, and
    concentration stays a deploy-time check, not a GA signal."""
    flat = [{"symbol": f"U{i % 24 + 1:02d}", "underlying_symbol": f"U{i % 24 + 1:02d}",
             "pnl": 158.0, "pnl_pct": 1.58} for i in range(90)]
    skewed = _base()
    even = _base(trades=flat)
    assert compute_fitness(CONVEX, skewed) == compute_fitness(CONVEX, even)
    assert skewed["convex_telemetry"]["top1_pct"] > 80.0
    assert even["convex_telemetry"]["top1_pct"] < 5.0
    assert even["convex_telemetry"]["hit_rate_pct"] == pytest.approx(100.0)


def test_telemetry_shares_are_none_when_the_book_is_net_negative():
    """A share of a negative denominator is not a share. Same convention as
    ``robustness_metrics``."""
    losers = [{"symbol": f"U{i % 24 + 1:02d}", "underlying_symbol": f"U{i % 24 + 1:02d}",
               "pnl": -100.0, "pnl_pct": -1.0} for i in range(90)]
    r = _base(trades=losers, total_return=-40.0)
    compute_fitness(CONVEX, r)
    assert r["convex_telemetry"]["top1_pct"] is None
    assert r["convex_telemetry"]["top5_pct"] is None
    assert r["convex_telemetry"]["hit_rate_pct"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------------------------
# REGISTRATION + isolation from every other fitness
# ---------------------------------------------------------------------------------------------
def test_the_metric_is_registered_in_the_catalog_with_metadata():
    from app.services import strategy_fitness as sf

    sf.assert_catalog_complete()
    by_key = {m["key"]: m for m in sf.METRICS_CATALOG}
    entry = by_key[CONVEX]
    assert set(entry["aliases"]) == set(_CONVEX_ALIASES) - {CONVEX}
    # The breadth floor replaces the trade scale; hit rate and concentration must never be
    # scored, so neither the win-rate factor nor the adjusted-under-caps switch applies.
    assert entry["supports_trade_scale"] is False
    assert entry["supports_win_rate_factor"] is False
    assert entry["uses_adjusted_under_caps"] is False
    assert "convex" in entry["label"].lower()
    assert set(_CONVEX_ALIASES) <= sf.catalog_accepted_metrics()


def test_unknown_metric_error_advertises_the_convex_metric():
    with pytest.raises(ValueError) as ei:
        compute_fitness("not_a_metric", {"total_trades": 1})
    assert CONVEX in str(ei.value)


def test_no_equity_or_option_car_name_can_reach_the_convex_code():
    """No runtime cost, and no behaviour change, for a non-convex job: every other metric name
    is disjoint from the convex aliases, and the convex function is never entered."""
    from app.services import strategy_fitness as sf

    other = (set(sf._FITNESS_KEYS) | {"max_drawdown", "max_dd", "drawdown"}
             | set(sf._CAR_ALIASES) | set(sf._OCAR_ALIASES))
    assert other.isdisjoint(set(_CONVEX_ALIASES))


def test_the_convex_path_is_structurally_unreachable_from_every_other_metric(monkeypatch):
    from app.services import strategy_fitness as sf

    def _boom(_results):
        raise AssertionError("the convex metric was entered by a non-convex job")

    monkeypatch.setattr(sf, "_option_convex", _boom)
    r = {"total_trades": 300, "avg_trades_per_year": 100.0, "annualized_return": 30.0,
         "max_drawdown": -20.0, "total_return": 119.7, "calmar_ratio": 1.5, "sqn": 2.4,
         "sharpe_ratio": 1.4, "win_rate": 61.0, "profit_factor": 1.8, "sortino_ratio": 2.1}
    for metric in ("consistent_annual_return", "option_consistent_annual_return", "sharpe",
                   "calmar_ratio", "total_return", "max_drawdown", "sqn"):
        sf.compute_fitness(metric, dict(r))


def test_the_equity_and_option_car_metrics_are_unmoved_by_this_module():
    """Acceptance criterion: adding option_convex must not touch either existing metric."""
    r = {"total_trades": 300, "avg_trades_per_year": 100.0, "annualized_return": 30.0,
         "max_drawdown": -40.0, "total_return": 119.7}
    assert compute_fitness("consistent_annual_return", dict(r)) == pytest.approx(30.0 * 0.5)
    assert compute_fitness("option_consistent_annual_return", dict(r)) \
        == pytest.approx(30.0 * 0.25)


def test_the_constants_are_the_documented_config_block():
    """The thresholds are commented CONFIG, not genes -- pinned so a silent re-tune shows up."""
    assert _CONVEX_DD_FREE_PCT == 50.0
    assert _CONVEX_DD_DEAD_PCT == 90.0
    assert _CONVEX_WIPED_OUT_DD_PCT == 100.0
    assert _CONVEX_MIN_TICKETS_PER_YEAR == 30.0
    assert _CONVEX_MIN_UNDERLYINGS == 20
    assert math.isclose(_convex_dd_factor(-70.0), 0.5)
