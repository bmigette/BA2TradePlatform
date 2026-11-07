"""A symbol whose estimate-based analysis has no estimate FAILS (owner, 2026-11-07) but is counted apart from
other analysis failures: it never feeds the failed-share refusal, and it is reported in the trial's ``af``."""
import pytest

from ba2_common.core.estimates_pit import EstimatesNotPrewarmed, EstimatesUnavailable

from app.services.backtest.daily_engine import (
    MIN_ANALYSIS_PASSES_FOR_REFUSAL, AnalysisFailureRefusal, DailyBacktestEngine,
)
from app.services.strategy_optimization_handler import (
    _analysis_failures_field, summarise_analysis_failures,
)


def _engine():
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.analysis_failures = {}
    return eng


def test_missing_estimates_are_counted_apart_and_never_refuse_the_run():
    eng = _engine()
    n = MIN_ANALYSIS_PASSES_FOR_REFUSAL * 2
    for i in range(n):
        eng._analysis_pass(7)
        if i % 2:                                    # 50% of passes lack an estimate
            eng._analysis_failed(7, f"S{i} @ 2022-03-01: none", EstimatesUnavailable("none"))
    rec = eng.analysis_failures_record()
    assert rec["failed"] == 0 and rec["estimates_unavailable"] == n // 2
    eng.refuse_if_analysis_failing()                 # does not raise
    assert _analysis_failures_field({"analysis_failures": rec})["af"] == {"passes": n, "failed": 0, "eu": n // 2}


def test_a_prewarm_gap_is_an_ordinary_failure_and_still_refuses():
    eng = _engine()
    for i in range(MIN_ANALYSIS_PASSES_FOR_REFUSAL):
        eng._analysis_pass(7)
        eng._analysis_failed(7, f"S{i}: gap", EstimatesNotPrewarmed("never downloaded"))
    with pytest.raises(AnalysisFailureRefusal):
        eng.refuse_if_analysis_failing()


def test_the_field_is_unchanged_without_missing_estimates():
    rec = {"passes": 10, "failed": 1}
    assert _analysis_failures_field({"analysis_failures": rec}) == {"af": {"passes": 10, "failed": 1}}


def test_job_summary_carries_the_missing_estimate_count():
    agg = summarise_analysis_failures("job", [{"af": {"passes": 100, "failed": 0, "eu": 20}, "key": "k"}])
    assert agg["estimates_unavailable"] == 20 and agg["failed"] == 0
