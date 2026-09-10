"""A persisted top-N row that contradicts the score it was chosen for must SAY SO.

WHY THIS EXISTS. `_persist_top_backtests` saves a job's best genomes as Backtests. A genome from
the GA's FINAL generation is persisted from the buffered results the GA itself computed, so the
row and the fitness agree by construction. Any EARLIER genome has to be RE-RUN, and the re-run is
not fed the GA's live state -- it rebuilds `bt_block` from the STORED `optimization_config` and
RE-DERIVES the screener hoisted state at re-run time. Either can have moved since the run.

That is not hypothetical. The handler already documents one instance: without re-applying the
screener hoisted state "the persisted top-N silently diverge from their fitness". The failure mode
is the dangerous kind -- the row looks finished, carries plausible metrics, and is what a human
reads when choosing what to deploy. Nothing anywhere says it no longer matches the search.

Chasing every field that could drift is open-ended. Detecting the drift is not: the re-run knows
the GA's score (`ga_fitness`, migration 030) and computes its own from its own results, so the two
can simply be compared. This turns an invisible disagreement into a loud one that names the rank,
both numbers and the gap -- and, per "no silent failure anywhere", refuses to let a contradictory
row pass as clean.
"""
import pytest

from app.services import strategy_optimization_handler as H


# --------------------------------------------------------------------------------------------
# Agreement -> nothing to report
# --------------------------------------------------------------------------------------------

def test_an_exact_match_is_not_a_divergence():
    assert H.rerun_fitness_divergence(5.574103998587804, 5.574103998587804) is None


def test_float_noise_is_not_a_divergence():
    """Re-running is deterministic, but summation order across a pool can move the last bits.
    The gate must not cry wolf on that -- a gate that fires on noise gets ignored."""
    assert H.rerun_fitness_divergence(5.5741039985, 5.5741039986) is None


def test_a_sentinel_that_reproduces_is_not_a_divergence():
    """A disqualified genome (LOW_TRADE_SENTINEL) re-running as disqualified is agreement."""
    assert H.rerun_fitness_divergence(-1e8, -1e8) is None


# --------------------------------------------------------------------------------------------
# Disagreement -> report it, with the numbers
# --------------------------------------------------------------------------------------------

def test_a_real_gap_is_reported_with_both_numbers():
    d = H.rerun_fitness_divergence(5.44, 4.98)

    assert d is not None
    assert d["ga_fitness"] == 5.44
    assert d["rerun_fitness"] == 4.98
    assert d["delta"] == pytest.approx(-0.46)
    assert d["pct"] == pytest.approx(-100 * 0.46 / 5.44, rel=1e-6)


def test_the_screener_hoisted_state_case_is_caught():
    """The documented real instance: re-derived screener state selects a different universe, so
    the re-run scores materially lower than the genome the GA ranked."""
    assert H.rerun_fitness_divergence(6.1688, 3.9) is not None


def test_a_genome_that_DISQUALIFIES_only_on_re_run_is_caught():
    """The loudest case worth catching: the GA scored it 5.2, the re-run produced too few trades
    and returned the sentinel. Persisted silently, this row reads as a healthy strategy."""
    d = H.rerun_fitness_divergence(5.2, -1e8)

    assert d is not None
    assert d["rerun_fitness"] == -1e8


def test_divergence_is_symmetric_a_re_run_scoring_HIGHER_is_equally_wrong():
    """A re-run beating its own GA score is not good news -- it means the two ran different
    strategies, and the recorded ranking is wrong either way."""
    assert H.rerun_fitness_divergence(4.0, 4.9) is not None


# --------------------------------------------------------------------------------------------
# Never break the persist over the check itself
# --------------------------------------------------------------------------------------------

def test_no_ga_fitness_means_no_verdict_not_a_crash():
    """`ranked` falls back to `best_params` with no trial key, and very old rows predate
    migration 030. Unknown is not the same as divergent."""
    assert H.rerun_fitness_divergence(None, 5.0) is None
    assert H.rerun_fitness_divergence(5.0, None) is None
    assert H.rerun_fitness_divergence(None, None) is None


def test_non_numeric_inputs_are_ignored_rather_than_raising():
    for bad in ("5.44", {}, [], object()):
        assert H.rerun_fitness_divergence(bad, 5.0) is None
        assert H.rerun_fitness_divergence(5.0, bad) is None


def test_a_zero_ga_fitness_does_not_divide_by_zero():
    d = H.rerun_fitness_divergence(0.0, 1.5)

    assert d is not None and d["delta"] == pytest.approx(1.5)
    assert d["pct"] is None       # undefined against a zero base, reported as such


# --------------------------------------------------------------------------------------------
# The tolerance itself
# --------------------------------------------------------------------------------------------

def test_tolerance_is_RELATIVE_so_it_scales_with_the_metric():
    """Fitness runs ~4-6 on these grids but the same code ranks other metrics on other scales."""
    assert H.rerun_fitness_divergence(1000.0, 1000.5) is None      # 0.05% -- noise
    assert H.rerun_fitness_divergence(1.0, 1.5) is not None        # 50%  -- real


def test_the_tolerance_is_overridable_for_a_caller_that_wants_it_stricter():
    assert H.rerun_fitness_divergence(5.0, 5.001, tol_rel=1e-9) is not None
    assert H.rerun_fitness_divergence(5.0, 5.001, tol_rel=1.0) is None
