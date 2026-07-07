"""Golden live<->backtest parity test (Phase 0 of the live<->backtest engine unification plan,
docs/plans/2026-07-02-live-backtest-engine-unification.md).

This is the missing EVIDENCE CHANNEL for the plan's core claim ("same engine in live and
backtest"): it replays a hermetic window of RECORDED live ExpertRecommendations (captured by
tools/capture_live_parity_fixture.py into a committed JSON fixture) through the backtest's exact
enter-decision path — the real TradeActionEvaluator.evaluate(...).execute(...) against a flat
BacktestAccount + the live expert's own enter ruleset — and pins the backtest decision against
what the live engine actually did.

Asserts (logic parity — a failure is a real shared-engine regression):
  * every live-FUNDED rec fires an order of the SAME side in the backtest;
  * every HOLD rec fires nothing.

Measures (reported, not asserted): BUY recs that passed the ruleset but were NOT funded live —
the live orchestration seam (dedup / equity / capital allocation) the unification plan targets.

The fixture is committed and consumed read-only; the test touches NO live DB and NO network.

Run:  ./venv/bin/python -m pytest tests/backtest/test_parity_golden.py -v -s
"""
from __future__ import annotations

import os

import pytest

from app.services.backtest.parity_harness import default_fixture, run_parity

_FIXTURE = default_fixture(13)  # live instance 13 = FMPRating, classic-RM, enter ruleset 10


@pytest.mark.skipif(not os.path.exists(_FIXTURE), reason="parity fixture not captured")
def test_golden_live_backtest_entry_parity():
    report = run_parity(_FIXTURE)
    print("\n" + report.summary())

    # There must be recorded live entries to prove parity against (guards a silently empty fixture).
    assert len(report.positive) >= 1, "fixture has no live-funded recs to prove parity against"
    assert len(report.negative) >= 1, "fixture has no HOLD recs for the negative control"

    # POSITIVE parity: every live-funded rec fires the SAME side in the backtest evaluator.
    assert report.positive_pass == len(report.positive), (
        "backtest did not reproduce a live-funded entry:\n" + report.summary())
    # NEGATIVE parity: every HOLD fires nothing.
    assert report.negative_pass == len(report.negative), (
        "backtest fired an order on a HOLD rec:\n" + report.summary())
    # No side/decision mismatches on the asserted sets.
    assert not report.mismatches, report.summary()
    assert report.ok


@pytest.mark.skipif(not os.path.exists(_FIXTURE), reason="parity fixture not captured")
def test_golden_expert_is_classic_rm_in_scope():
    # Sanity: the golden fixture is an in-scope (classic-RM) expert, per the locked parity scope.
    report = run_parity(_FIXTURE)
    assert report.expert  # a real expert name was captured
