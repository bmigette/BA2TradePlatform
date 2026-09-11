"""Historical comparison: the same decision, rebuilt from a pinned cache root.

Spec step 5 / section 2 row 2: "Normal ``analyze_as_of`` path against a pinned,
warmed historical cache; capture its normalized bundle and compare to live.
Measures input and recommendation differences caused by historical
reconstruction, coverage, timing or revisions."

The session under test is a REAL capture (the same
``_analysis_capture`` + ``_gather_and_process`` pair every live ``run_analysis``
drives), of a REAL ``FMPEarningsDrift`` over the hermetic provider fixtures --
and the pinned root it is compared against holds THE SAME payloads, in the
on-disk layouts the production readers resolve
(``fmp_history/<ns>__<SYM>.json`` and ``<OhlcvProviderClass>/<SYM>_1d.parquet``).
That is what makes ``match`` mean something here: two different code paths (the
live calendar/detail gather against the API, the ``analyze_as_of``
reconstruction against disk) consuming equal payloads, compared field by field.

``FMPEarningsDrift`` is the subject because it is the one recorded expert that
reads the evaluation clock in ``_process`` -- so the recorded evaluation time
(the first ``process``-phase clock read) is a real recorded value here and not
the ``started_at`` fallback.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

from ba2_common.core.replay import ReplayStatus, load_bundle

from app.services.replay import historical

from tests.backtest.fixtures.hermetic_providers import (
    FixtureFundamentalsDetailsProvider,
    FixtureOHLCVProvider,
)

SYMBOL = "AAPL"
ANALYSIS_ID = "2001"
SESSION_ID = "S-HISTORICAL-TEST"

#: The instant the live analysis evaluates at. Inside the hermetic fixture's
#: price window (2024-01-02 + 90 business days) and 31 days after its planted
#: +20% earnings surprise, so the live decision is a live BUY and the historical
#: reconstruction has bars to price it with.
EVAL = datetime(2024, 2, 15, 14, 30, tzinfo=timezone.utc)

#: What the expert instance's resolved settings are. ``max_days_since_report``
#: is wide enough that the 2024-01-15 report is still fresh at EVAL.
SETTINGS: Dict[str, Any] = {
    "surprise_min_pct": 5.0,
    "max_days_since_report": 60,
    "expected_profit_percent": 8.0,
    "expected_profit_mode": "static",
    "dynamic_scale": 0.0,
    "max_expected_profit_percent": 100.0,
    "model_target_method": "forward",
}


# --------------------------------------------------------------------------- #
# The payloads, taken from the hermetic fixtures ONCE
# --------------------------------------------------------------------------- #
def _fixture_price_frame() -> pd.DataFrame:
    """The fixture's AAPL bars, in the parquet shape the as_of cache stores."""
    frame = FixtureOHLCVProvider().get_ohlcv_data(SYMBOL, interval="1d")
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True)
    # effective_date == the bar's own Date: a read sliced to effective_date<=as_of
    # is no-lookahead by construction (ba2_common.core.native_cache).
    frame["effective_date"] = frame["Date"]
    return frame


def _fixture_raw_earnings() -> List[Dict[str, Any]]:
    """The fixture's planted earnings, in FMP's RAW per-symbol history shape.

    The pinned root holds what FMP returns, not what the provider returns: the
    whole point is that the historical run goes through the SAME
    ``FMPCompanyDetailsProvider.get_past_earnings`` mapping the live run went
    through, so only the payload is shared, never the mapped result.
    """
    served = FixtureFundamentalsDetailsProvider().get_past_earnings(
        SYMBOL, frequency="quarterly", end_date=datetime(2030, 1, 1),
        lookback_periods=10, format_type="dict")
    return [
        {"symbol": SYMBOL, "date": row["report_date"], "eps": row["reported_eps"],
         "epsEstimated": row["estimated_eps"], "time": "amc"}
        for row in served["earnings"]
    ]


def _close_at(frame: pd.DataFrame, as_of: datetime) -> float:
    visible = frame[frame["Date"] <= pd.Timestamp(as_of)]
    return float(visible["Close"].iloc[-1])


# --------------------------------------------------------------------------- #
# Seeding a cache root and pinning it
# --------------------------------------------------------------------------- #
def seed_cache_root(root: Path, *, earnings: List[Dict[str, Any]] | None,
                    price: pd.DataFrame | None) -> Path:
    """Write the fixture payloads into ``root`` in the production cache layouts.

    ``None`` for either payload leaves that artifact ABSENT -- which is what a
    ``missing_history`` row has to be produced by.
    """
    root.mkdir(parents=True, exist_ok=True)
    if earnings is not None:
        history_dir = root / "fmp_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / f"past_earnings_quarterly__{SYMBOL}.json").write_text(
            json.dumps(earnings), encoding="utf-8")
    if price is not None:
        from ba2_providers import OHLCV_PROVIDERS

        provider_dir = root / OHLCV_PROVIDERS["fmp"].__name__
        provider_dir.mkdir(parents=True, exist_ok=True)
        price.to_parquet(provider_dir / f"{SYMBOL}_1d.parquet", index=False)
    return root


def pin_cache_root(source: Path, dest: Path, *, warmed: bool = True) -> Path:
    """Pin ``source`` into ``dest`` through the REAL warm planner + materializer.

    Not a hand-written manifest: the provenance a historical comparison reads is
    the provenance ``ba2_providers.warm.roots`` writes, and a second spelling of
    it here would drift from the one production produces.

    ``warmed=False`` pins the same bytes with no requirement claimed as fetched
    by this run, so every file carries ``legacy_history_unknown_revision``.
    """
    import ba2_experts.replay_dependencies  # noqa: F401 - registers the adapters
    from ba2_common.core.replay.dependencies import Window, expert_replay_inputs
    from ba2_providers.warm import planner, roots

    requirements = expert_replay_inputs(
        "FMPEarningsDrift", SETTINGS, [SYMBOL], Window(start=None, end=EVAL))
    plan = planner.plan(requirements, [str(source)], as_of_now=EVAL)
    keys = {entry.requirement.key for entry in plan.entries} if warmed else set()
    roots.materialize_pinned_root(plan, [str(source)], str(dest), warmed=keys)
    return dest


# --------------------------------------------------------------------------- #
# The live capture
# --------------------------------------------------------------------------- #
def capture_live_session(store_root, export_dir) -> Path:
    """Record ONE live FMPEarningsDrift analysis over the fixture payloads.

    Every fake sits at a REAL boundary: the market-wide calendar shortcut and the
    per-symbol earnings history are faked at FMP's own call, and the quote at the
    account. The provider, its mapping, the capture scope and the recorded pair
    are all production code.
    """
    import importlib
    import logging
    from contextlib import ExitStack
    from unittest import mock

    from ba2_common.core.backtest_context import LiveProviderBundle
    from ba2_common.core.replay import ReplayStore, SessionRecord, set_replay_store
    from ba2_common.core.types import AnalysisUseCase

    drift = importlib.import_module("ba2_experts.FMPEarningsDrift")
    details_module = importlib.import_module(
        "ba2_providers.fundamentals.details.FMPCompanyDetailsProvider")
    from ba2_providers.fmp_common import TTLCache

    frame = _fixture_price_frame()
    raw_earnings = _fixture_raw_earnings()
    quote = _close_at(frame, EVAL)

    # The market-wide calendar carries the report but NOT the analyst estimate --
    # FMP's documented ~70% case -- so the live gather falls through to the
    # per-symbol history, which is the payload the pinned root holds.
    calendar_rows = [{"symbol": SYMBOL, "date": "2024-01-15", "eps": 1.20,
                      "epsEstimated": None}]

    details = details_module.FMPCompanyDetailsProvider.__new__(
        details_module.FMPCompanyDetailsProvider)
    details.api_key = "TEST-API-KEY"

    expert = drift.FMPEarningsDrift.__new__(drift.FMPEarningsDrift)
    expert.id = 21
    expert.logger = logging.getLogger("historicalfixture.FMPEarningsDrift")
    expert._get_current_price = lambda symbol: quote
    expert._live_providers = lambda: LiveProviderBundle(
        lambda category, name=None, **kw: details)
    expert._gather_symbol = SYMBOL
    expert._gather_max_days_since_report = SETTINGS["max_days_since_report"]
    expert._gather_expected_profit_mode = SETTINGS["expected_profit_mode"]

    market_analysis = _FakeMarketAnalysis(ANALYSIS_ID, SYMBOL)

    store = ReplayStore(store_root, writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="historical-test-instance",
        started_at=EVAL, exchange_tz="America/New_York",
        app_version="test", package_versions={"ba2_common": "test"},
        source_revision="0" * 40, dirty=False))
    set_replay_store(store)
    try:
        with ExitStack() as stack:
            stack.enter_context(_pinned_clock())
            stack.enter_context(mock.patch.object(
                drift.fmpsdk, "earning_calendar",
                lambda apikey=None, from_date=None, to_date=None, **kw: list(calendar_rows)))
            stack.enter_context(mock.patch.object(
                drift, "_CALENDAR_CACHE", TTLCache(drift._CALENDAR_CACHE_TTL_SECONDS)))
            stack.enter_context(mock.patch.object(
                details_module.fmpsdk, "historical_earning_calendar",
                lambda apikey=None, symbol=None, limit=None, **kw:
                [dict(row) for row in raw_earnings]))
            with expert._analysis_capture(market_analysis, SETTINGS,
                                          AnalysisUseCase.ENTER_MARKET.value):
                expert._gather_and_process(
                    expert._live_providers(), SETTINGS,
                    market_analysis=market_analysis,
                    use_case=AnalysisUseCase.ENTER_MARKET.value)
    finally:
        set_replay_store(None)
        exported = store.export_session(SESSION_ID, export_dir)
        store.close(timeout=5.0)
    return exported


class _FakeMarketAnalysis:
    """Only what the capture scope reads off the live row."""

    def __init__(self, analysis_id: str, symbol: str):
        from ba2_common.core.types import AnalysisUseCase

        self.id = analysis_id
        self.symbol = symbol
        self.subtype = AnalysisUseCase.ENTER_MARKET
        self.created_at = EVAL


def _pinned_clock():
    """Pin every ``replay_now()`` read to :data:`EVAL`.

    Constant rather than stepping: this fixture's subject is the HISTORICAL
    reconstruction, which is handed ONE evaluation time, so the two phases must
    read the same instant for the comparison to be about the data instead of
    about the clock. (The phase-tagging behaviour itself is pinned by
    ``test_expert_replay.py``, whose fixture steps.)
    """
    import importlib
    from unittest import mock

    clock = importlib.import_module("ba2_common.core.replay.clock")

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return EVAL if tz is not None else EVAL.replace(tzinfo=None)

    return mock.patch.object(clock, "datetime", _Pinned)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("historical-capture")
    return capture_live_session(root / "store", root / "export")


@pytest.fixture
def bundle(session, tmp_path) -> Path:
    """A private copy: every run writes coverage (and objects) into the bundle."""
    target = tmp_path / "bundle"
    shutil.copytree(session, target)
    return target


@pytest.fixture
def pinned_root(tmp_path) -> Path:
    source = seed_cache_root(tmp_path / "source", earnings=_fixture_raw_earnings(),
                             price=_fixture_price_frame())
    return pin_cache_root(source, tmp_path / "pinned")


def _only(report):
    assert report.total == 1, [(r.analysis_id, r.status, r.detail) for r in report.results]
    return report.results[0]


# --------------------------------------------------------------------------- #
# 1. Equal payloads reproduce the live decision
# --------------------------------------------------------------------------- #
def test_a_pinned_root_holding_the_same_payloads_reproduces_the_analysis(bundle, pinned_root,
                                                                        tmp_path):
    report = historical.run(bundle, pinned_root, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_MATCH, (result.status, result.detail,
                                                          list(result.field_diffs))
    assert result.expert_class == "FMPEarningsDrift"
    assert result.symbol == SYMBOL
    assert report.capability == ReplayStatus.CAPABILITY_HISTORICAL


def test_the_recorded_evaluation_time_is_the_first_process_phase_clock_read(bundle):
    record = load_bundle(bundle).analyses[0]

    process_reads = [value for value, phase
                     in zip(record.clock_reads, record.clock_read_phases)
                     if phase == ReplayStatus.PHASE_PROCESS]
    assert process_reads, "the fixture must record a process-phase read"
    assert historical.evaluation_time(record) == datetime.fromisoformat(process_reads[0])


def test_without_a_process_read_the_evaluation_time_falls_back_to_started_at(bundle):
    from tests.replay import edit_analysis

    edit_analysis(bundle, ANALYSIS_ID, clock_reads=[], clock_read_phases=[])
    record = load_bundle(bundle).analyses[0]

    assert historical.evaluation_time(record) == record.started_at


# --------------------------------------------------------------------------- #
# 2. A changed input is reported, with the decision it changed
# --------------------------------------------------------------------------- #
def test_altered_history_reports_the_input_field_and_the_decision_change(bundle, tmp_path):
    altered = [dict(row) for row in _fixture_raw_earnings()]
    altered[0]["eps"] = altered[0]["epsEstimated"]  # the +20% beat becomes a flat print
    source = seed_cache_root(tmp_path / "altered-source", earnings=altered,
                             price=_fixture_price_frame())
    pinned = pin_cache_root(source, tmp_path / "altered-pinned")

    report = historical.run(bundle, pinned, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_DIFFERENCE, (result.status, result.detail)
    fields = [name for name, _recorded, _produced in result.field_diffs]
    assert any("reported_eps" in name for name in fields), fields
    assert any(name.startswith("recommendation.") for name in fields), fields
    assert any(name == "recommendation.signal" for name in fields), fields


# --------------------------------------------------------------------------- #
# 3. A dependency the pinned root does not hold is NAMED
# --------------------------------------------------------------------------- #
def test_a_missing_required_artifact_is_missing_history_naming_the_requirement(bundle,
                                                                              tmp_path):
    source = seed_cache_root(tmp_path / "gap-source", earnings=None,
                             price=_fixture_price_frame())
    pinned = pin_cache_root(source, tmp_path / "gap-pinned")

    report = historical.run(bundle, pinned, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_MISSING_HISTORY, (result.status,
                                                                    result.detail)
    assert "past_earnings_quarterly" in result.detail
    assert SYMBOL in result.detail


# --------------------------------------------------------------------------- #
# 4. Legacy provenance cannot be called a match
# --------------------------------------------------------------------------- #
def test_an_estimates_payload_written_after_the_live_read_is_revision_unknown(tmp_path):
    """The second ``revision_unknown`` trigger, on the requirement resolver's own terms.

    Reached through the notes builder rather than the end-to-end fixture because
    the analyst-estimates namespace is only declared in ``model`` mode, and a
    model-mode capture would change what the other cases in this file compare.
    The rule it pins is the one spec section 5 names: the estimates endpoint
    filters fiscal periods, NOT historical revisions, so a payload WARMED after
    the live analysis read it is today's revision of an older number -- and
    ``warmed`` provenance must not launder that into a match.
    """
    import ba2_experts.replay_dependencies  # noqa: F401 - registers the adapters
    from ba2_common.core.replay.dependencies import (
        FMP_PROVIDER, KIND_HISTORY, Requirement, Window,
    )
    from ba2_experts.replay_dependencies import ESTIMATOR_ESTIMATES_NAMESPACE
    from ba2_providers.warm import planner, roots

    source = tmp_path / "estimates-source"
    (source / "fmp_history").mkdir(parents=True)
    (source / "fmp_history" / f"{ESTIMATOR_ESTIMATES_NAMESPACE}__{SYMBOL}.json").write_text(
        json.dumps([{"symbol": SYMBOL, "date": "2024-12-31", "estimatedEpsAvg": 1.4}]),
        encoding="utf-8")

    requirement = Requirement(
        provider=FMP_PROVIDER, namespace=ESTIMATOR_ESTIMATES_NAMESPACE, symbol=SYMBOL,
        window=Window(start=None, end=EVAL), interval=None, kind=KIND_HISTORY,
        optional=False, reason="the price-target model reads forward EPS estimates")
    plan = planner.plan([requirement], [str(source)], as_of_now=EVAL)
    pinned_dir = tmp_path / "estimates-pinned"
    roots.materialize_pinned_root(plan, [str(source)], str(pinned_dir),
                                  warmed={requirement.key})
    pinned = historical.PinnedRoot(pinned_dir)
    # the pin is of THIS run's fetch, so its recorded mtime postdates EVAL (2024)
    pinned_plan = planner.plan([requirement], [pinned.root], as_of_now=EVAL)

    notes = historical._revision_notes(pinned_plan, pinned, EVAL)

    assert notes, "a warmed estimates payload newer than the live read must be flagged"
    assert ESTIMATOR_ESTIMATES_NAMESPACE in notes[0]
    assert "filters fiscal periods, not revisions" in notes[0]


def test_legacy_provenance_is_revision_unknown_not_match(bundle, tmp_path):
    source = seed_cache_root(tmp_path / "legacy-source", earnings=_fixture_raw_earnings(),
                             price=_fixture_price_frame())
    pinned = pin_cache_root(source, tmp_path / "legacy-pinned", warmed=False)

    report = historical.run(bundle, pinned, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_REVISION_UNKNOWN, (result.status,
                                                                     result.detail)
    assert "legacy_history_unknown_revision" in result.detail


# --------------------------------------------------------------------------- #
# 5. Isolation: the pinned root lives in a CHILD process, offline
# --------------------------------------------------------------------------- #
def test_the_run_happens_in_a_child_and_leaves_the_parent_cache_root_alone(bundle,
                                                                          pinned_root,
                                                                          tmp_path,
                                                                          monkeypatch):
    import socket

    import ba2_common.config as config

    before_env = os.environ.get("CACHE_FOLDER")
    before_config = config.CACHE_FOLDER

    attempts: List[Any] = []
    original = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect",
                        lambda self, address, *a, **kw: attempts.append(address))

    report = historical.run(bundle, pinned_root, tmp_path / "report")

    monkeypatch.setattr(socket.socket, "connect", original)
    assert attempts == [], attempts
    assert os.environ.get("CACHE_FOLDER") == before_env
    assert config.CACHE_FOLDER == before_config
    assert _only(report).status == ReplayStatus.COVERAGE_MATCH

    payload = json.loads(
        (tmp_path / "report" / f"{ReplayStatus.CAPABILITY_HISTORICAL}.json").read_text(
            encoding="utf-8"))
    assert payload["historical"]["cache_root"] == str(Path(pinned_root).resolve())
    assert payload["historical"]["isolation"] == {
        "network_attempts": [], "instance_resolutions": [], "provider_resolutions": []}


# --------------------------------------------------------------------------- #
# 6. The report says what ran
# --------------------------------------------------------------------------- #
def test_the_report_fills_the_expert_input_and_recommendation_stages(bundle, pinned_root,
                                                                     tmp_path):
    out = tmp_path / "report"
    report = historical.run(bundle, pinned_root, out)
    capability = ReplayStatus.CAPABILITY_HISTORICAL

    markdown = (out / f"{capability}.md").read_text(encoding="utf-8")
    payload = json.loads((out / f"{capability}.json").read_text(encoding="utf-8"))

    filled = {row["stage"]: row["status"] for row in payload["stages"]}
    assert filled["Expert inputs"] != ReplayStatus.COVERAGE_NOT_RUN, filled
    assert filled["Recommendation"] != ReplayStatus.COVERAGE_NOT_RUN, filled
    for stage in ("Selection", "Rules and sizing", "Execution"):
        assert filled[stage] == ReplayStatus.COVERAGE_NOT_RUN, filled
    # the markdown and the JSON read the same rendering
    for row in payload["stages"]:
        line = next(l for l in markdown.splitlines()
                    if l.startswith(f"| {row['stage']} |"))
        assert line.rstrip().endswith(f"| {row['status']} |"), (row, line)
    assert set(payload["capabilities_not_run"]) == {
        ReplayStatus.CAPABILITY_RECORDED_EXPERT,
        ReplayStatus.CAPABILITY_GATHER_TAPE,
        ReplayStatus.CAPABILITY_DECISION,
    }


def test_running_writes_the_historical_coverage_back_into_the_bundle(bundle, pinned_root,
                                                                    tmp_path):
    historical.run(bundle, pinned_root, tmp_path / "report")

    rows = [entry for entry in load_bundle(bundle).coverage
            if entry.capability == ReplayStatus.CAPABILITY_HISTORICAL]
    assert [(row.analysis_id, row.status) for row in rows] == [
        (ANALYSIS_ID, ReplayStatus.COVERAGE_MATCH)]


def test_running_twice_is_a_second_session_not_a_collision(bundle, pinned_root, tmp_path):
    """Two runs over one bundle: two historical sessions, one coverage row.

    The store keys analyses on ``(analysis_id, attempt_id)`` GLOBALLY, so a second
    reconstruction has to be a second session with its own attempt -- and the
    coverage row for the analysis is REPLACED, not duplicated.
    """
    first = historical.run(bundle, pinned_root, tmp_path / "first")
    second = historical.run(bundle, pinned_root, tmp_path / "second")

    assert _only(first).status == ReplayStatus.COVERAGE_MATCH
    assert _only(second).status == ReplayStatus.COVERAGE_MATCH
    assert (first.extra[ReplayStatus.CAPABILITY_HISTORICAL]["historical_session_id"]
            != second.extra[ReplayStatus.CAPABILITY_HISTORICAL]["historical_session_id"])
    rows = [entry for entry in load_bundle(bundle).coverage
            if entry.capability == ReplayStatus.CAPABILITY_HISTORICAL]
    assert len(rows) == 1, rows


# --------------------------------------------------------------------------- #
# 7. Totals are never shrunk
# --------------------------------------------------------------------------- #
def test_an_unsupported_expert_is_a_row_not_a_dropped_analysis(bundle, pinned_root,
                                                               tmp_path):
    from tests.replay import edit_analysis

    edit_analysis(bundle, ANALYSIS_ID, expert_class="FMPSenateTraderWeight")

    report = historical.run(bundle, pinned_root, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_UNSUPPORTED
    assert sum(report.counts().values()) == report.total == 1


def test_an_analysis_without_a_recorded_bundle_is_missing_capture(bundle, pinned_root,
                                                                  tmp_path):
    from tests.replay import edit_analysis

    edit_analysis(bundle, ANALYSIS_ID,
                  bundle_capture_status=ReplayStatus.CAPTURE_FAILED)

    report = historical.run(bundle, pinned_root, tmp_path / "report")

    assert _only(report).status == ReplayStatus.COVERAGE_MISSING_CAPTURE
