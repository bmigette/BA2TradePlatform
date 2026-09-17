"""Does a pinned snapshot serve the SESSIONS a run will ask for? (review 2026-09-16, F1)

``missing_coverage`` answers "is the symbol in this snapshot"; it was the only question the
launcher preflight and the per-trial seam ever asked. The reviewer published a snapshot over
2024-03-01..2024-03-29, pinned it on a 2025-01-02..2025-12-31 backtest, and the preflight ACCEPTED
it and wrote the pin onto the run -- after which ``observe()`` returned None for every decision
date, every gate read ``missing_session``, and the GA would have scored that suppression as
strategy behaviour.

These tests pin the second question -- and, just as important, the answers that must stay YES:
a symbol that listed part-way through the window, the warm-up prefix, and a structure field with
no confirmed pivot yet are legitimately undefined observations, not a broken snapshot.
"""
from __future__ import annotations

from datetime import date

import pytest

from ba2_common.core.market_calendar import prior_regular_session, regular_session_dates
from ba2_common.core.market_condition_reader import (
    MappedMarketConditionReader,
    clear_window_coverage_cache,
    missing_coverage,
    window_coverage_problems,
)
from ba2_common.core.market_condition_store import MarketConditionStore, month_of
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
)

PROFILE = PROFILES["ohlcv-v1"]
FIELDS = [f.name for f in PROFILE.fields]

#: The reviewer's reproduction, to the day.
SNAP_FIRST, SNAP_LAST = date(2024, 3, 1), date(2024, 3, 29)
RUN_FIRST, RUN_LAST = date(2025, 1, 2), date(2025, 12, 31)


def _row(session, status, reason="published row"):
    valid = status == STATUS_VALID
    return {"session": session,
            "values": [1.0] * len(FIELDS) if valid else [None] * len(FIELDS),
            "status": [status] * len(FIELDS),
            "reasons": [reason] * len(FIELDS),
            "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
            "raw_row_lo": 0, "raw_row_hi": 0}


def _coverage(sessions, statuses):
    """The manifest's per-symbol record, built the way the warmup builds it: the exceptions are
    aggregated PER (field, status) with their row count and first/last session, which is exactly
    what lets a reader tell a contiguous leading run from an interior hole."""
    runs = {}
    for session, status in zip(sessions, statuses):
        if status == STATUS_VALID:
            continue
        for field in FIELDS:
            runs.setdefault((field, status), []).append(session)
    exceptions = [{"kind": "status", "field": field, "status": status, "rows": len(days),
                   "first_session": days[0].isoformat(), "last_session": days[-1].isoformat()}
                  for (field, status), days in sorted(runs.items())]
    return {"rows": len(sessions), "exceptions": exceptions,
            "first_session": sessions[0].isoformat() if sessions else None,
            "last_session": sessions[-1].isoformat() if sessions else None}


def _publish(root, *, decisions_from=SNAP_FIRST, decisions_to=SNAP_LAST,
             symbols=("AAA", "BBB"), statuses=None, drop=()):
    """A snapshot with a row for EVERY feature session of its decision window, as the warmup
    writes one (an uncomputable row is an explicit negative observation, never an absence).

    ``statuses``: ``{symbol: {session: status}}`` overriding the default ``valid``.
    ``drop``: ``{symbol: (session, ...)}`` -- rows that are genuinely ABSENT, which is what a
    hole in the objects looks like.
    """
    store = MarketConditionStore(root)
    decisions = regular_session_dates(decisions_from, decisions_to)
    sessions = regular_session_dates(prior_regular_session(decisions[0]),
                                     prior_regular_session(decisions[-1]))
    objects, coverage = [], {}
    for symbol in symbols:
        by_symbol = (statuses or {}).get(symbol, {})
        dropped = set((drop or {}).get(symbol, ()))
        kept = [s for s in sessions if s not in dropped]
        by_month = {}
        for session in kept:
            by_month.setdefault(month_of(session), []).append(session)
        for month_sessions in by_month.values():
            entry, _ = store.write_feature_object(
                PROFILE, symbol,
                [_row(s, by_symbol.get(s, STATUS_VALID)) for s in month_sessions])
            objects.append(entry)
        coverage[symbol] = _coverage(kept, [by_symbol.get(s, STATUS_VALID) for s in kept])
    manifest = store.make_manifest(
        PROFILE, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage=coverage, universe=list(symbols),
        sessions=sessions, window_start=decisions[0], window_end=decisions[-1])
    return store, store.write_manifest(manifest)


def _reader(store, digest):
    return MappedMarketConditionReader(store.cache_root, digest, "ohlcv-v1", store=store)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_window_coverage_cache()
    yield
    clear_window_coverage_cache()


# ------------------------------------------------------------------ the finding, reproduced
def test_a_snapshot_for_another_window_is_refused_although_every_symbol_is_present(tmp_path):
    """The reviewer's exact reproduction: symbol coverage says yes, the sessions say no."""
    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)

    # WHAT THE OLD CHECK SAW, pinned so the regression cannot come back as "it was always fine":
    # the 2024-03 snapshot covers every symbol of the 2025 run, and that was the whole preflight.
    assert missing_coverage(reader, ["AAA", "BBB"]) == []
    assert reader.observe("AAA", prior_regular_session(RUN_FIRST)) is None

    problems = window_coverage_problems(reader, ["AAA", "BBB"], RUN_FIRST, RUN_LAST)
    assert len(problems) == 1, problems
    snap = regular_session_dates(SNAP_FIRST, SNAP_LAST)      # 2024-03-29 is Good Friday
    assert f"{snap[0]}..{snap[-1]}" in problems[0]
    assert f"{RUN_FIRST}..{RUN_LAST}" in problems[0]
    assert digest in problems[0]


def test_the_window_that_the_snapshot_was_built_for_is_accepted(tmp_path):
    store, digest = _publish(tmp_path / "cache")
    assert window_coverage_problems(_reader(store, digest), ["AAA", "BBB"],
                                    SNAP_FIRST, SNAP_LAST) == []


def test_one_decision_date_earlier_needs_one_feature_session_earlier(tmp_path):
    """The timing policy is part of the question: a decision on D reads the session BEFORE D.

    A run starting one session earlier than the snapshot's own window needs a row the snapshot
    does not have, and an off-by-one pin is refused like any other wrong window.
    """
    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)
    earlier = regular_session_dates(date(2024, 2, 20), SNAP_LAST)[0]
    assert earlier < SNAP_FIRST
    assert window_coverage_problems(reader, ["AAA"], earlier, SNAP_LAST)
    assert window_coverage_problems(reader, ["AAA"], SNAP_FIRST, SNAP_LAST) == []


def test_a_hole_inside_the_window_is_refused_naming_the_month(tmp_path):
    """An absent row is not an observation. Nothing in the manifest explains it, and the
    decision dates it removes are invisible in the fitness."""
    gap = regular_session_dates(date(2024, 3, 13), date(2024, 3, 15))
    store, digest = _publish(tmp_path / "cache", drop={"BBB": gap})
    problems = window_coverage_problems(_reader(store, digest), ["AAA", "BBB"],
                                        SNAP_FIRST, SNAP_LAST)
    assert len(problems) == 1, problems
    assert problems[0].startswith("BBB:") and "2024-03" in problems[0]
    assert "no row at all" in problems[0]


def test_a_whole_month_missing_from_the_objects_is_refused(tmp_path):
    """The same fault at object granularity: a month whose feature object was never published."""
    march = regular_session_dates(date(2024, 3, 1), date(2024, 3, 31))
    store, digest = _publish(tmp_path / "cache", decisions_from=date(2024, 2, 1),
                             decisions_to=date(2024, 4, 30), drop={"BBB": march})
    problems = window_coverage_problems(_reader(store, digest), ["BBB"],
                                        date(2024, 2, 1), date(2024, 4, 30))
    assert len(problems) == 1 and "2024-03 has 0 of" in problems[0]


# ------------------------------------------------- the observations that are legitimately unknown
def test_a_symbol_that_lists_part_way_through_the_window_is_accepted(tmp_path):
    """PLTR/APP/ARM/GEV/SNDK in the published snapshots: a contiguous LEADING run of
    missing_session rows followed by the warm-up prefix. There is no price history to have
    computed anything from, and the strategy could not have traded the symbol then either --
    refusing the run would refuse the universe for telling the truth."""
    sessions = regular_session_dates(prior_regular_session(SNAP_FIRST),
                                     prior_regular_session(SNAP_LAST))
    listed_at = len(sessions) // 2
    statuses = {"BBB": {**{s: STATUS_MISSING_SESSION for s in sessions[:listed_at]},
                        **{s: STATUS_INSUFFICIENT_HISTORY for s in sessions[listed_at:-2]}}}
    store, digest = _publish(tmp_path / "cache", statuses=statuses)
    assert window_coverage_problems(_reader(store, digest), ["AAA", "BBB"],
                                    SNAP_FIRST, SNAP_LAST) == []


def test_an_insufficient_history_field_is_never_a_snapshot_fault(tmp_path):
    """``ta-structure-v1`` has 15,494 of these on resistance distance alone: no confirmed pivot
    yet, an undefined swing state. The gate reads unknown and refuses that entry -- the designed
    meaning. Downloading more history does not repair an undefined level, so a snapshot full of
    them is correct, not broken."""
    sessions = regular_session_dates(prior_regular_session(SNAP_FIRST),
                                     prior_regular_session(SNAP_LAST))
    statuses = {"AAA": {s: STATUS_INSUFFICIENT_HISTORY for s in sessions}}
    store, digest = _publish(tmp_path / "cache", statuses=statuses)
    assert window_coverage_problems(_reader(store, digest), ["AAA"], SNAP_FIRST, SNAP_LAST) == []


# ------------------------------------------------------------- unusable rows that are NOT unknown
def test_a_symbol_with_no_usable_row_anywhere_in_the_window_is_refused(tmp_path):
    """SPCX: 1,508 missing_session rows over 2020-2025 because its daily cache starts in 2026.
    That is not a listing date inside the window -- it is a symbol the snapshot cannot serve at
    all, and a gated run would silently never enter it."""
    sessions = regular_session_dates(prior_regular_session(SNAP_FIRST),
                                     prior_regular_session(SNAP_LAST))
    statuses = {"BBB": {s: STATUS_MISSING_SESSION for s in sessions}}
    store, digest = _publish(tmp_path / "cache", statuses=statuses)
    problems = window_coverage_problems(_reader(store, digest), ["AAA", "BBB"],
                                        SNAP_FIRST, SNAP_LAST)
    assert len(problems) == 1, problems
    assert problems[0].startswith("BBB:") and "EVERY session" in problems[0]
    # ONE message for the symbol, not one per field: the run is identical on all of them.
    for field in FIELDS:
        assert field in problems[0]


def test_a_trailing_run_of_missing_sessions_is_refused(tmp_path):
    """A delisting, or a source cache that stops before the window does. The last decision dates
    would read missing_session, and nothing about the symbol's listing explains it."""
    sessions = regular_session_dates(prior_regular_session(SNAP_FIRST),
                                     prior_regular_session(SNAP_LAST))
    statuses = {"BBB": {s: STATUS_MISSING_SESSION for s in sessions[-5:]}}
    store, digest = _publish(tmp_path / "cache", statuses=statuses)
    problems = window_coverage_problems(_reader(store, digest), ["BBB"], SNAP_FIRST, SNAP_LAST)
    assert len(problems) == 1 and "unexplained hole" in problems[0]


def test_an_interior_run_of_missing_sessions_is_refused(tmp_path):
    """Rows exist and say "no data" in the MIDDLE of the window: neither a listing date nor an
    undefined level explains it, so it is a source problem to fix, not a strategy result."""
    sessions = regular_session_dates(prior_regular_session(SNAP_FIRST),
                                     prior_regular_session(SNAP_LAST))
    statuses = {"BBB": {s: STATUS_MISSING_SESSION for s in sessions[5:8]}}
    store, digest = _publish(tmp_path / "cache", statuses=statuses)
    problems = window_coverage_problems(_reader(store, digest), ["BBB"], SNAP_FIRST, SNAP_LAST)
    assert len(problems) == 1 and "unexplained hole" in problems[0]


def test_a_missing_run_outside_the_requested_window_is_not_this_run_s_problem(tmp_path):
    """The question is always "does it serve THIS experiment": a symbol whose data starts inside
    the snapshot but before the requested window is fully served for that window."""
    store, digest = _publish(tmp_path / "cache", decisions_from=date(2024, 2, 1),
                             decisions_to=date(2024, 4, 30),
                             statuses={"BBB": {s: STATUS_MISSING_SESSION
                                               for s in regular_session_dates(date(2024, 1, 25),
                                                                              date(2024, 2, 15))}})
    reader = _reader(store, digest)
    assert window_coverage_problems(reader, ["BBB"], date(2024, 3, 1), date(2024, 4, 30)) == []
    assert window_coverage_problems(reader, ["BBB"], date(2024, 2, 1), date(2024, 4, 30)) == []


# --------------------------------------------------------------------------------- mechanics
def test_a_symbol_the_snapshot_does_not_carry_is_left_to_the_presence_check(tmp_path):
    """Two refusals naming the same symbol for the same reason is one refusal too many."""
    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)
    assert missing_coverage(reader, ["AAA", "ZZZ"]) == ["ZZZ"]
    assert window_coverage_problems(reader, ["AAA", "ZZZ"], SNAP_FIRST, SNAP_LAST) == []


def test_the_validation_is_paid_once_per_digest_universe_and_window(tmp_path, monkeypatch):
    """A batch launches many jobs against one pin and a GA runs thousands of trials against it.
    The answer depends on (snapshot, universe, window) and nothing else, so it is computed once."""
    import ba2_common.core.market_condition_reader as mcr

    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)
    calls = []
    real = mcr._window_coverage_problems
    monkeypatch.setattr(mcr, "_window_coverage_problems",
                        lambda *a, **kw: calls.append(a[1:]) or real(*a, **kw))

    for _ in range(5):
        assert window_coverage_problems(reader, ["AAA", "BBB"], SNAP_FIRST, SNAP_LAST) == []
    assert len(calls) == 1
    # A different universe, or a different window, is a different question.
    window_coverage_problems(reader, ["AAA"], SNAP_FIRST, SNAP_LAST)
    window_coverage_problems(reader, ["AAA", "BBB"], SNAP_FIRST, date(2024, 3, 28))
    assert len(calls) == 3
    # ... and the result is a COPY: a caller that sorts or extends its list cannot poison the memo.
    problems = window_coverage_problems(reader, ["AAA", "BBB"], SNAP_FIRST, SNAP_LAST)
    problems.append("mutated")
    assert window_coverage_problems(reader, ["AAA", "BBB"], SNAP_FIRST, SNAP_LAST) == []


def test_no_pin_no_universe_and_an_empty_window_are_answered_without_a_manifest(tmp_path):
    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)
    assert window_coverage_problems(None, ["AAA"], SNAP_FIRST, SNAP_LAST) == []
    assert window_coverage_problems(reader, [], SNAP_FIRST, SNAP_LAST) == []
    # A window with no session at all (a weekend) is not a run: no observation is defined in it.
    weekend = date(2024, 3, 23)
    problems = window_coverage_problems(reader, ["AAA"], weekend, date(2024, 3, 24))
    assert len(problems) == 1 and "no regular NYSE session" in problems[0]


def test_dates_are_accepted_in_the_shapes_a_stored_config_carries(tmp_path):
    from datetime import datetime

    store, digest = _publish(tmp_path / "cache")
    reader = _reader(store, digest)
    for start, end in ((SNAP_FIRST.isoformat(), SNAP_LAST.isoformat()),
                       (datetime(2024, 3, 1, 9, 30), datetime(2024, 3, 29, 16, 0)),
                       ("2024-03-01T00:00:00", "2024-03-29T00:00:00")):
        assert window_coverage_problems(reader, ["AAA"], start, end) == []


def test_a_reader_that_cannot_report_its_manifest_is_refused_not_assumed_good(tmp_path):
    class _NoManifest:
        manifest_digest = "d" * 64
        profile = "ohlcv-v1"
        cache_root = "/nowhere"

        def symbols(self):
            return ("AAA",)

        def coverage(self):
            return {}

    problems = window_coverage_problems(_NoManifest(), ["AAA"], SNAP_FIRST, SNAP_LAST)
    assert len(problems) == 1 and "cannot report the sessions it covers" in problems[0]
