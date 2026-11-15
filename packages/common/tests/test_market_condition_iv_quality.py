"""Chain quality of iv-v1 snapshots (review 2026-11-15): the holes insufficient_history would hide."""
from __future__ import annotations

from datetime import date, timedelta

from ba2_common.core.market_condition_iv_quality import (
    MAX_RATE, MAX_RUN, quality_from_manifest, quality_from_statuses, quality_problems, worst_symbols,
)
from ba2_common.core.market_conditions import (
    FIELD_ATM_IV_30, FIELD_ATM_IV_60, FIELD_REALIZED_VOL_20, STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION, STATUS_VALID,
)

V, IH, MS = STATUS_VALID, STATUS_INSUFFICIENT_HISTORY, STATUS_MISSING_SESSION


def _days(n):
    return [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]


def test_defaults():
    assert (MAX_RUN, MAX_RATE) == (20, 0.40)


def test_runs_rates_and_eligibility():
    s = _days(10)
    st = {FIELD_REALIZED_VOL_20: [MS, MS, V, V, V, V, V, V, V, V],      # first two: not trading yet
          FIELD_ATM_IV_30: [MS, MS, V, IH, IH, IH, V, IH, V, V],
          FIELD_ATM_IV_60: [MS, MS] + [V] * 8}
    q = quality_from_statuses(s, st)
    q30 = q[FIELD_ATM_IV_30]
    assert (q30["eligible"], q30["unknown"], q30["longest_run"]) == (8, 4, 3)
    assert (q30["run_first"], q30["run_last"]) == (s[3].isoformat(), s[5].isoformat())
    assert q30["rate"] == 0.5
    assert q[FIELD_ATM_IV_60]["unknown"] == 0 and q[FIELD_ATM_IV_60]["run_first"] is None


def test_a_chain_that_starts_after_the_bars_is_a_run_not_pre_listing():
    """A leading missing_session run while the stock already trades counts in full."""
    s = _days(60)
    st = {FIELD_REALIZED_VOL_20: [V] * 60, FIELD_ATM_IV_30: [MS] * 30 + [V] * 30,
          FIELD_ATM_IV_60: [MS] * 30 + [V] * 30}
    q = quality_from_statuses(s, st)
    assert q[FIELD_ATM_IV_30]["longest_run"] == 30 and q[FIELD_ATM_IV_30]["run_first"] == s[0].isoformat()
    refused, accepted = quality_problems({"LATE": q})
    assert len(refused) == 1 and refused[0].startswith("LATE:") and accepted == []


def test_thresholds_and_explicit_acceptance():
    s = _days(100)
    gappy = {FIELD_REALIZED_VOL_20: [V] * 100,
             FIELD_ATM_IV_30: ([IH] * 5 + [V] * 5) * 10,                    # 50% unknown, runs of 5
             FIELD_ATM_IV_60: [V] * 100}
    clean = {FIELD_REALIZED_VOL_20: [V] * 100, FIELD_ATM_IV_30: [V] * 100, FIELD_ATM_IV_60: [V] * 100}
    by = {"GAP": quality_from_statuses(s, gappy), "OK": quality_from_statuses(s, clean)}
    refused, accepted = quality_problems(by)
    assert [r.split(":")[0] for r in refused] == ["GAP"] and "50%" in refused[0]
    refused, accepted = quality_problems(by, accept=["gap"])
    assert refused == [] and [a.split(":")[0] for a in accepted] == ["GAP"]
    assert quality_problems(by, max_rate=0.6)[0] == []
    assert worst_symbols(by)[0][0] == "GAP"


def test_the_manifest_record_round_trips():
    s = _days(5)
    q = quality_from_statuses(s, {FIELD_REALIZED_VOL_20: [V] * 5, FIELD_ATM_IV_30: [V, IH, V, V, V],
                                  FIELD_ATM_IV_60: [V] * 5})
    manifest = {"coverage": {"AAA": {"exceptions": [{"kind": "status", "field": "x"}] + [
        {"kind": "iv_quality", "field": f, **v} for f, v in q.items()]}, "BBB": {"exceptions": []}}}
    assert quality_from_manifest(manifest) == {"AAA": q}
