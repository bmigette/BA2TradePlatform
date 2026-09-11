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

#: The same analysis in ``model`` mode. This is the ONLY configuration that
#: declares the analyst-estimates namespace (``_estimator_inputs``), so it is the
#: only one whose reconstruction can reach the estimates revision rule.
MODEL_SETTINGS: Dict[str, Any] = {**SETTINGS, "expected_profit_mode": "model"}

MODEL_SESSION_ID = "S-HISTORICAL-TEST-MODEL"

#: FMP's raw analyst-estimates rows (the endpoint always answers ANNUAL rows, see
#: ``FMPCompanyDetailsProvider.get_earnings_estimates``). The same payload is
#: served to the live capture and written into the pinned root.
RAW_ESTIMATES: List[Dict[str, Any]] = [
    {"symbol": SYMBOL, "date": "2024-12-31", "estimatedEpsAvg": 5.2,
     "estimatedEpsHigh": 5.6, "estimatedEpsLow": 4.8, "numberAnalystsEstimatedEps": 12},
    {"symbol": SYMBOL, "date": "2025-12-31", "estimatedEpsAvg": 5.9,
     "estimatedEpsHigh": 6.4, "estimatedEpsLow": 5.4, "numberAnalystsEstimatedEps": 11},
]


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
                    price: pd.DataFrame | None,
                    estimates: List[Dict[str, Any]] | None = None) -> Path:
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
    if estimates is not None:
        history_dir = root / "fmp_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / f"earnings_estimates_quarterly__{SYMBOL}.json").write_text(
            json.dumps(estimates), encoding="utf-8")
    if price is not None:
        from ba2_providers import OHLCV_PROVIDERS

        provider_dir = root / OHLCV_PROVIDERS["fmp"].__name__
        provider_dir.mkdir(parents=True, exist_ok=True)
        price.to_parquet(provider_dir / f"{SYMBOL}_1d.parquet", index=False)
    return root


def pin_cache_root(source: Path, dest: Path, *, warmed: bool = True,
                   settings: Dict[str, Any] | None = None) -> Path:
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
        "FMPEarningsDrift", settings if settings is not None else SETTINGS,
        [SYMBOL], Window(start=None, end=EVAL))
    plan = planner.plan(requirements, [str(source)], as_of_now=EVAL)
    keys = {entry.requirement.key for entry in plan.entries} if warmed else set()
    roots.materialize_pinned_root(plan, [str(source)], str(dest), warmed=keys)
    return dest


# --------------------------------------------------------------------------- #
# The live capture
# --------------------------------------------------------------------------- #
def capture_live_session(store_root, export_dir, *,
                         settings: Dict[str, Any] | None = None,
                         session_id: str = SESSION_ID) -> Path:
    """Record ONE live FMPEarningsDrift analysis over the fixture payloads.

    Every fake sits at a REAL boundary: the market-wide calendar shortcut, the
    per-symbol earnings history and the analyst-estimates endpoint are faked at
    FMP's own call, and the quote at the account. The provider, its mapping, the
    capture scope and the recorded pair are all production code.

    ``settings`` defaults to :data:`SETTINGS` (static profit mode). Passing
    :data:`MODEL_SETTINGS` runs the same analysis in ``model`` mode, which is the
    only configuration that DECLARES the analyst-estimates namespace -- and hence
    the only one whose reconstruction can hit the estimates revision rule.
    """
    import importlib
    import logging
    from contextlib import ExitStack
    from unittest import mock

    from ba2_common.core.backtest_context import LiveProviderBundle
    from ba2_common.core.replay import ReplayStore, SessionRecord, set_replay_store
    from ba2_common.core.types import AnalysisUseCase

    settings = dict(settings if settings is not None else SETTINGS)
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
    expert._gather_max_days_since_report = settings["max_days_since_report"]
    expert._gather_expected_profit_mode = settings["expected_profit_mode"]

    market_analysis = _FakeMarketAnalysis(ANALYSIS_ID, SYMBOL)

    store = ReplayStore(store_root, writer="sync")
    store.begin_session(SessionRecord(
        session_id=session_id, instance_id="historical-test-instance",
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
            # The analyst-estimates endpoint is a direct HTTP call inside the
            # provider, so it is faked there -- the SAME rows the pinned root holds.
            stack.enter_context(mock.patch.object(
                details_module, "fmp_http_get",
                lambda url, params=None, **kw: _FakeResponse(
                    [dict(row) for row in RAW_ESTIMATES])))
            with expert._analysis_capture(market_analysis, settings,
                                          AnalysisUseCase.ENTER_MARKET.value):
                expert._gather_and_process(
                    expert._live_providers(), settings,
                    market_analysis=market_analysis,
                    use_case=AnalysisUseCase.ENTER_MARKET.value)
    finally:
        set_replay_store(None)
        exported = store.export_session(session_id, export_dir)
        store.close(timeout=5.0)
    return exported


class _FakeResponse:
    """What ``fmp_http_get`` returns: an object with ``.json()``."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


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
    fields = [diff.field for diff in result.field_diffs]
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
    """The second ``revision_unknown`` trigger, at the notes builder.

    The rule it pins is the one spec section 5 names: the estimates endpoint
    filters fiscal periods, NOT historical revisions, so a payload WARMED after
    the live analysis read it is today's revision of an older number -- and
    ``warmed`` provenance must not launder that into a match. The same rule is
    pinned END TO END, through a real model-mode capture, by
    ``test_a_warmed_estimates_payload_newer_than_the_live_read_is_revision_unknown``;
    this one keeps the rule itself pinned when that fixture changes.
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
# 5. The report says what ran
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


# --------------------------------------------------------------------------- #
# 8. Numeric differences carry their distance (spec section 8)
# --------------------------------------------------------------------------- #
def test_a_numeric_difference_reports_its_absolute_and_relative_distance(bundle, tmp_path):
    """Spec section 8 asks for absolute/relative differences; reprs are not distances.

    The pinned report drops the EPS beat from 1.20 to 1.10, so the input moved by
    0.10 (8.33% of the recorded value) and the decision held. Both numbers have to
    reach the row detail, the markdown and the JSON.
    """
    altered = [dict(row) for row in _fixture_raw_earnings()]
    altered[0]["eps"] = 1.10
    source = seed_cache_root(tmp_path / "delta-source", earnings=altered,
                             price=_fixture_price_frame())
    pinned = pin_cache_root(source, tmp_path / "delta-pinned")
    out = tmp_path / "report"

    report = historical.run(bundle, pinned, out)

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_DIFFERENCE
    eps = next(d for d in result.field_diffs if d.field.endswith("['reported_eps']"))
    assert eps.abs_delta == pytest.approx(0.1)
    assert eps.rel_delta == pytest.approx(0.1 / 1.2)
    assert eps.rel_delta_undefined is False
    # the one-line detail carries it too, not only the table
    assert "abs 0.1" in result.detail

    payload = json.loads(
        (out / f"{ReplayStatus.CAPABILITY_HISTORICAL}.json").read_text(encoding="utf-8"))
    row = next(d for d in payload["results"][0]["field_diffs"]
               if d["field"].endswith("['reported_eps']"))
    assert row["abs_delta"] == pytest.approx(0.1)
    assert row["rel_delta"] == pytest.approx(0.1 / 1.2)
    markdown = (out / f"{ReplayStatus.CAPABILITY_HISTORICAL}.md").read_text(encoding="utf-8")
    assert "| Field | Recorded | Produced | Delta |" in markdown
    assert "abs 0.1" in markdown


def test_a_zero_baseline_reports_an_undefined_relative_delta_not_a_number():
    """Relative to nothing is not 0.0 and not infinity -- it is undefined, and says so."""
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"x": 0.0}, {"x": 2.5}, "inputs", with_deltas=True)

    assert len(diffs) == 1
    assert diffs[0].abs_delta == pytest.approx(2.5)
    assert diffs[0].rel_delta is None
    assert diffs[0].rel_delta_undefined is True
    assert "undefined" in diffs[0].delta_text()


def test_deltas_are_off_for_the_other_capabilities():
    """recorded_expert / gather_tape keep their exact previous field_diff shape."""
    from app.services.replay.expert_replay import compare_values

    plain = compare_values({"x": 1.0}, {"x": 2.0}, "value")
    assert plain == [("value['x']", "1.0", "2.0")], plain


def test_a_frame_difference_carries_the_largest_cell_distance():
    """A 600-bar series that shifted reports ONE row with the worst cell, not 600."""
    from app.services.replay.expert_replay import compare_values

    left = pd.DataFrame({"Close": [10.0, 20.0, 30.0]})
    right = pd.DataFrame({"Close": [10.0, 22.0, 30.0]})

    diffs = compare_values(left, right, "inputs['frame']", with_deltas=True)

    assert len(diffs) == 1
    assert "1 differing numeric cell" in diffs[0].field
    assert diffs[0].abs_delta == pytest.approx(2.0)
    assert diffs[0].rel_delta == pytest.approx(0.1)


# --------------------------------------------------------------------------- #
# 9. Coverage that is present but does not COVER the window
# --------------------------------------------------------------------------- #
def test_a_price_series_ending_before_the_as_of_is_missing_history(bundle, tmp_path):
    """A parquet the reader can open is not coverage if it stops before the as_of.

    ``planner._judge_timeseries`` calls this ``stale``; for a SERIES that word means
    a coverage shortfall (a missing prefix, a short tail, holes) and a
    reconstruction priced off it is built from a gap.
    """
    frame = _fixture_price_frame()
    truncated = frame[frame["Date"] <= pd.Timestamp("2024-01-31", tz="UTC")]
    source = seed_cache_root(tmp_path / "short-source", earnings=_fixture_raw_earnings(),
                             price=truncated)
    pinned = pin_cache_root(source, tmp_path / "short-pinned")

    report = historical.run(bundle, pinned, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_MISSING_HISTORY, (result.status,
                                                                    result.detail)
    assert "timeseries|fmp|ohlcv|AAPL|1d" in result.detail
    assert "tail stops at" in result.detail


def test_a_stale_fmp_history_is_a_note_not_a_gap(bundle, pinned_root, tmp_path, monkeypatch):
    """Age is not absence: the hermetic reader ignores fmp_history age by design.

    ``_inspect_history`` calls a file older than the 7-day window ``stale``. Reporting
    that as ``missing_history`` would describe a perfectly readable artifact as
    absent -- so it is carried as a coverage NOTE on whatever row results.
    """
    from ba2_providers.warm import planner

    original = planner._inspect_history

    def _always_stale(req, roots, as_of_now, measured):
        entry = original(req, roots, as_of_now, measured)
        if entry.status != planner.STATUS_PRESENT:
            return entry
        return planner.PlanEntry(
            requirement=entry.requirement, status=planner.STATUS_STALE,
            action=planner.ACTION_REFRESH, source_root=entry.source_root,
            path=entry.path, size_bytes=entry.size_bytes, detail="age 400.0d (test)")

    monkeypatch.setattr(planner, "_inspect_history", _always_stale)

    report = historical.run(bundle, pinned_root, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_MATCH, (result.status, result.detail)
    assert "is stale on the pin" in result.detail


# --------------------------------------------------------------------------- #
# 10. The recorded branch decides what the reconstruction runs
# --------------------------------------------------------------------------- #
def test_the_fmp_key_accessor_comes_from_the_recorded_branch_flag():
    """DeterministicScorer only fetches analyst rows when the LIVE gather had a key."""
    entry = {"analysis_id": "1", "expert_class": "DeterministicScorer",
             "branch_flags": {historical.DS_ANALYST_KEY_FLAG: True}}
    assert historical._api_key_from_record(entry)() == historical.OFFLINE_API_KEY

    entry["branch_flags"] = {historical.DS_ANALYST_KEY_FLAG: False}
    assert historical._api_key_from_record(entry)() is None


def test_a_record_without_the_key_flag_refuses_rather_than_deciding():
    from ba2_common.core.replay import ReplayMiss

    entry = {"analysis_id": "1", "expert_class": "DeterministicScorer", "branch_flags": {}}
    accessor = historical._api_key_from_record(entry)

    with pytest.raises(ReplayMiss) as caught:
        accessor()
    assert historical.DS_ANALYST_KEY_FLAG in str(caught.value)


def test_the_job_carries_the_recorded_branch_flags_to_the_child(bundle, pinned_root,
                                                                tmp_path):
    out = tmp_path / "report"
    historical.run(bundle, pinned_root, out)

    job_path = next(out.glob(f"{historical.JOB_STEM}_*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    flags = job["analyses"][0]["branch_flags"]
    assert flags["earnings_calendar_branch"] is True
    assert flags["as_of_is_none"] is True


def test_a_data_branch_that_moved_is_a_finding_and_a_path_branch_is_a_note():
    """The two kinds of branch flag are not the same finding."""
    from ba2_common.core.replay import AnalysisRecord

    def record(flags):
        return AnalysisRecord(
            analysis_id="1", attempt_id="a", session_id="s",
            expert_class="DeterministicScorer", symbol="AAPL", use_case="enter_market",
            started_at=EVAL, outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
            branch_flags=flags)

    findings, notes = historical._branch_diffs(
        record({"ds_analyst_key_present": True, "fmp_rating_branch": "live_snapshot"}),
        record({"ds_analyst_key_present": False,
                "fmp_rating_branch": "as_of_reconstruction"}))

    assert [d.field for d in findings] == ["branch.ds_analyst_key_present"]
    assert any("fmp_rating_branch" in note for note in notes)


# --------------------------------------------------------------------------- #
# 11. Hermetic misses are a gap, never data
# --------------------------------------------------------------------------- #
def test_a_hermetic_cache_miss_makes_the_analysis_missing_history(bundle):
    """``[]`` from a hermetic miss is not an empty payload -- it is an absent one.

    ``_fmp_history_disk_read_or_fetch`` answers the first few missing symbols with
    an empty list and a log line, so an UNDECLARED read can build a reconstruction
    out of nothing and have it compare as a plausible difference. The child reports
    the miss registry per analysis and the row becomes ``missing_history``.
    """
    session_bundle = load_bundle(bundle)
    analysis = session_bundle.analyses[0]
    job = historical._Job(analysis=analysis, as_of=EVAL, revision_notes=(),
                          coverage_notes=())

    result = historical._compare(
        session_bundle, job, produced=analysis, decode_object=lambda h: None,
        child_note={"ran": True, "hermetic_misses": ["past_earnings_quarterly/AAPL"]},
        timed_out=False)

    assert result.status == ReplayStatus.COVERAGE_MISSING_HISTORY
    assert "past_earnings_quarterly/AAPL" in result.detail


def test_the_child_reports_its_hermetic_miss_registry(bundle, pinned_root, tmp_path):
    """The real child always answers with the (normally empty) registry."""
    out = tmp_path / "report"
    historical.run(bundle, pinned_root, out)

    result_path = next(out.glob(f"{historical.CHILD_RESULT_STEM}_*.json"))
    child = json.loads(result_path.read_text(encoding="utf-8"))
    assert all("hermetic_misses" in entry for entry in child["analyses"].values())
    assert all(entry["hermetic_misses"] == [] for entry in child["analyses"].values())


# --------------------------------------------------------------------------- #
# 12. The estimates revision rule, end to end
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def model_session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("historical-model-capture")
    return capture_live_session(root / "store", root / "export",
                                settings=MODEL_SETTINGS, session_id=MODEL_SESSION_ID)


@pytest.fixture
def model_bundle(model_session, tmp_path) -> Path:
    target = tmp_path / "model-bundle"
    shutil.copytree(model_session, target)
    return target


def test_a_warmed_estimates_payload_newer_than_the_live_read_is_revision_unknown(
        model_bundle, tmp_path):
    """End to end: everything reproduces, and it still cannot be called a match.

    The reconstruction is what live consumed field for field, but the estimates
    payload behind it was WARMED after the live analysis read its estimates. The
    endpoint filters fiscal periods, not revisions, so what is on disk is today's
    revision of a number live read earlier -- and spec section 3 forbids using a
    later-observed response as proof of what was available before.
    """
    source = seed_cache_root(tmp_path / "model-source", earnings=_fixture_raw_earnings(),
                             price=_fixture_price_frame(), estimates=RAW_ESTIMATES)
    pinned = pin_cache_root(source, tmp_path / "model-pinned", settings=MODEL_SETTINGS)

    report = historical.run(model_bundle, pinned, tmp_path / "report")

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_REVISION_UNKNOWN, (result.status,
                                                                     result.detail)
    assert "earnings_estimates_quarterly" in result.detail
    assert "filters fiscal periods, not revisions" in result.detail


# --------------------------------------------------------------------------- #
# 13. Per-stage counts: one analysis, two stages, two answers
# --------------------------------------------------------------------------- #
def test_a_recommendation_only_difference_leaves_expert_inputs_as_a_match():
    """Rolling ONE status into both rows points the reader at the wrong stage."""
    from app.services.replay.report import AnalysisResult, FieldDiff, ReplayReport

    result = AnalysisResult(
        analysis_id="1", expert_class="FMPEarningsDrift", symbol=SYMBOL,
        use_case="enter_market", recorded_outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
        status=ReplayStatus.COVERAGE_DIFFERENCE, detail="",
        field_diffs=[FieldDiff(field="recommendation.confidence", recorded="80.0",
                               produced="70.0", abs_delta=10.0, rel_delta=0.125)])

    report = ReplayReport(
        session_id="s", bundle_dir="b",
        capability=ReplayStatus.CAPABILITY_HISTORICAL, results=[result],
        stage_results=historical._stage_results([result]))
    rows = {stage: status for stage, _fields, status, _caps in report.stage_rows()}

    assert rows[historical.STAGE_EXPERT_INPUTS] == "match 1"
    assert rows[historical.STAGE_RECOMMENDATION] == "difference 1"


def test_an_input_only_difference_leaves_the_recommendation_as_a_match():
    from app.services.replay.report import AnalysisResult, FieldDiff, ReplayReport

    result = AnalysisResult(
        analysis_id="1", expert_class="FMPEarningsDrift", symbol=SYMBOL,
        use_case="enter_market", recorded_outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
        status=ReplayStatus.COVERAGE_DIFFERENCE, detail="",
        field_diffs=[FieldDiff(field="inputs['latest_earnings']['time']",
                               recorded="'amc'", produced="'bmo'")])

    report = ReplayReport(
        session_id="s", bundle_dir="b",
        capability=ReplayStatus.CAPABILITY_HISTORICAL, results=[result],
        stage_results=historical._stage_results([result]))
    rows = {stage: status for stage, _fields, status, _caps in report.stage_rows()}

    assert rows[historical.STAGE_EXPERT_INPUTS] == "difference 1"
    assert rows[historical.STAGE_RECOMMENDATION] == "match 1"


def test_a_stage_that_answers_for_fewer_analyses_than_the_report_is_refused():
    """A stage is not allowed to shrink the totals (spec section 8)."""
    from app.services.replay.report import AnalysisResult, ReplayReport

    results = [
        AnalysisResult(analysis_id=str(i), expert_class="FMPEarningsDrift", symbol=SYMBOL,
                       use_case="enter_market",
                       recorded_outcome=ReplayStatus.OUTCOME_RECOMMENDATION,
                       status=ReplayStatus.COVERAGE_MATCH)
        for i in (1, 2)
    ]
    report = ReplayReport(
        session_id="s", bundle_dir="b", capability=ReplayStatus.CAPABILITY_HISTORICAL,
        results=results,
        stage_results={historical.STAGE_EXPERT_INPUTS: {"1": ReplayStatus.COVERAGE_MATCH}})

    with pytest.raises(ValueError, match="every analysis must be represented"):
        report.stage_rows()


def test_extra_evidence_may_not_overwrite_a_standard_report_key():
    from app.services.replay.report import ReplayReport

    report = ReplayReport(session_id="s", bundle_dir="b",
                          capability=ReplayStatus.CAPABILITY_HISTORICAL,
                          extra={"total": 99})

    with pytest.raises(ValueError, match="would overwrite the standard report key"):
        report.to_mapping()


# --------------------------------------------------------------------------- #
# 14. The historical session is FINALIZED, not left open
# --------------------------------------------------------------------------- #
def test_the_historical_session_is_finalized(bundle, pinned_root, tmp_path):
    """An open session is what ``mark_interrupted_sessions`` later relabels a crash."""
    from ba2_common.core.replay.store import ReplayIndex

    report = historical.run(bundle, pinned_root, tmp_path / "report")
    session_id = report.extra[ReplayStatus.CAPABILITY_HISTORICAL]["historical_session_id"]

    index = ReplayIndex(Path(bundle) / "index.sqlite")
    try:
        record = index.get_session(session_id)
        assert record.status == ReplayStatus.SESSION_FINALIZED
        assert record.ended_at is not None
    finally:
        index.close()


# --------------------------------------------------------------------------- #
# 15. Offline credentials: a key, or a loud refusal
# --------------------------------------------------------------------------- #
def test_a_credential_lookup_is_answered_and_anything_else_refuses():
    from ba2_common.core.replay import ReplayMiss
    import ba2_common.config as config

    with historical.offline_credentials():
        assert config.get_app_setting("FMP_API_KEY") == historical.OFFLINE_API_KEY
        assert config.get_app_setting("alpaca_market_api_secret") == historical.OFFLINE_API_KEY
        with pytest.raises(ReplayMiss, match="min_tp_sl_percent"):
            config.get_app_setting("min_tp_sl_percent")


def test_a_key_shaped_substring_is_not_a_credential():
    """Segment matching, not ``"key" in name``: ``monkey_mode`` is not an API key."""
    assert historical.is_credential_setting("FMP_API_KEY")
    assert historical.is_credential_setting("fred_api_key")
    assert historical.is_credential_setting("alpaca_market_api_secret")
    assert not historical.is_credential_setting("monkey_mode")
    assert not historical.is_credential_setting("keyword_weights")


def test_every_binding_is_patched_by_identity_and_restored(monkeypatch):
    """``from ba2_common.config import get_app_setting`` binds by VALUE, in many modules."""
    import sys
    import types

    import ba2_common.config as config

    original = config.get_app_setting
    borrower = types.ModuleType("ba2_fake_binding_holder")
    borrower.get_app_setting = original          # the same object, as a real importer holds it
    impostor = types.ModuleType("ba2_fake_impostor")
    impostor.get_app_setting = lambda key, default=None: "untouched"
    monkeypatch.setitem(sys.modules, borrower.__name__, borrower)
    monkeypatch.setitem(sys.modules, impostor.__name__, impostor)

    with historical.offline_credentials() as patched:
        assert borrower.get_app_setting("FMP_API_KEY") == historical.OFFLINE_API_KEY
        assert impostor.get_app_setting("FMP_API_KEY") == "untouched"
        assert borrower.__name__ in patched

    assert config.get_app_setting is original
    assert borrower.get_app_setting is original


# --------------------------------------------------------------------------- #
# 16. Failure surfaces: loud, and never a lost report
# --------------------------------------------------------------------------- #
def _fake_child(monkeypatch, behaviour):
    """Replace the child launch; ``behaviour(job, result_path)`` decides what happens."""
    import subprocess

    def _run(argv, **kwargs):
        job = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        return behaviour(job, Path(job["result_path"]))

    monkeypatch.setattr(historical.subprocess, "run", _run)
    return subprocess


def test_a_child_that_writes_no_result_is_a_loud_failure(bundle, pinned_root, tmp_path,
                                                         monkeypatch):
    import subprocess

    _fake_child(monkeypatch, lambda job, result: subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="boom"))

    with pytest.raises(historical.HistoricalRunError, match="wrote no result"):
        historical.run(bundle, pinned_root, tmp_path / "report")


def test_a_child_that_reports_failure_is_a_loud_failure(bundle, pinned_root, tmp_path,
                                                        monkeypatch):
    import subprocess

    def behaviour(job, result):
        result.write_text(json.dumps({"ok": False, "error": "the pin moved"}),
                          encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="")

    _fake_child(monkeypatch, behaviour)

    with pytest.raises(historical.HistoricalRunError, match="the pin moved"):
        historical.run(bundle, pinned_root, tmp_path / "report")


def test_an_unreadable_child_result_is_a_loud_failure(bundle, pinned_root, tmp_path,
                                                      monkeypatch):
    import subprocess

    def behaviour(job, result):
        result.write_text("{not json", encoding="utf-8")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    _fake_child(monkeypatch, behaviour)

    with pytest.raises(historical.HistoricalRunError, match="could not be read"):
        historical.run(bundle, pinned_root, tmp_path / "report")


def test_a_timed_out_child_still_reports_every_analysis(bundle, pinned_root, tmp_path,
                                                        monkeypatch):
    """A timeout must not discard the report -- least of all the rows already decided."""
    import subprocess

    def behaviour(job, result):
        raise subprocess.TimeoutExpired(cmd="child", timeout=1.0, stderr=b"slow")

    _fake_child(monkeypatch, behaviour)

    report = historical.run(bundle, pinned_root, tmp_path / "report", timeout=1.0)

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert "ran out of time" in result.detail
    assert report.extra[ReplayStatus.CAPABILITY_HISTORICAL]["child_timed_out"] is True


def test_the_default_child_budget_scales_with_the_number_of_analyses():
    assert historical.child_timeout(1) < historical.child_timeout(50)
    assert historical.child_timeout(0) == historical.CHILD_TIMEOUT_BASE_S


def test_a_historical_record_without_a_live_attempt_cannot_be_attributed(tmp_path):
    """Dropping it would let a reconstruction that DID run report as 'recorded nothing'."""
    from ba2_common.core.replay import AnalysisRecord, SessionRecord
    from ba2_common.core.replay.store import ReplayIndex

    index = ReplayIndex(tmp_path / "index.sqlite")
    try:
        index.begin_session(SessionRecord(
            session_id="orphan", instance_id="i", started_at=EVAL,
            exchange_tz="America/New_York", app_version="test", dirty=False))
        index.commit_analysis(AnalysisRecord(
            analysis_id="9", attempt_id="a", session_id="orphan",
            expert_class="FMPEarningsDrift", symbol=SYMBOL, use_case="enter_market",
            started_at=EVAL, outcome=ReplayStatus.OUTCOME_RECOMMENDATION))
    finally:
        index.close()

    with pytest.raises(historical.HistoricalRunError, match="no live_attempt_id"):
        historical._produced_records(tmp_path, "orphan")


# --------------------------------------------------------------------------- #
# 17. Isolation, proved rather than asserted
# --------------------------------------------------------------------------- #
def test_the_parent_refuses_while_the_hermetic_escape_hatch_is_open(bundle, pinned_root,
                                                                    tmp_path, monkeypatch):
    """The parent enters ``replay_isolation`` too -- which is what makes this fire.

    ``BA2_HERMETIC_ALLOW_NETWORK=1`` reopens the FMP network lock. A run that only
    stripped it from the CHILD's environment would still have done its own bundle
    and cache-root work with that lock open while calling itself offline.
    """
    from app.services.replay.isolation import HERMETIC_ESCAPE_HATCH, ReplayIsolationBreach

    monkeypatch.setenv(HERMETIC_ESCAPE_HATCH, "1")

    with pytest.raises(ReplayIsolationBreach, match=HERMETIC_ESCAPE_HATCH):
        historical.run(bundle, pinned_root, tmp_path / "report")


def test_the_reconstruction_runs_in_a_child_that_reached_nothing(bundle, pinned_root,
                                                                 tmp_path):
    """The CHILD's probe is the evidence -- a parent-side socket patch proves nothing.

    ``replay_isolation`` replaces ``socket.socket.connect`` for the duration of the
    run, so a recorder installed in the parent beforehand is not even in place while
    the work happens. What the child actually reached is recorded by the probe
    inside it and travels back in the report; the parent's job is to show its own
    ``CACHE_FOLDER`` was never touched and its socket layer was restored.
    """
    import socket

    import ba2_common.config as config

    before_env = os.environ.get("CACHE_FOLDER")
    before_config = config.CACHE_FOLDER
    before_connect = socket.socket.connect

    report = historical.run(bundle, pinned_root, tmp_path / "report")

    assert os.environ.get("CACHE_FOLDER") == before_env
    assert config.CACHE_FOLDER == before_config
    assert socket.socket.connect is before_connect, "the isolation did not restore the transport"
    assert _only(report).status == ReplayStatus.COVERAGE_MATCH

    payload = json.loads(
        (tmp_path / "report" / f"{ReplayStatus.CAPABILITY_HISTORICAL}.json").read_text(
            encoding="utf-8"))
    assert payload["historical"]["cache_root"] == str(Path(pinned_root).resolve())
    assert payload["historical"]["isolation"] == {
        "network_attempts": [], "instance_resolutions": [], "provider_resolutions": []}


# --------------------------------------------------------------------------- #
# 18. The pin is verified, not trusted
# --------------------------------------------------------------------------- #
def test_a_drifted_pinned_file_cannot_be_called_a_match(bundle, pinned_root, tmp_path):
    """The pin promised those BYTES; something else is on disk now.

    Re-serialized with different whitespace on purpose: the payload still decodes
    to the same rows, so the reconstruction still reproduces the analysis exactly
    -- and it STILL cannot be called a match, because the artifact behind it is
    not the one the pin hashed. A drift check that only fired when the result
    changed would be measuring the result, not the pin.
    """
    target = pinned_root / "fmp_history" / f"past_earnings_quarterly__{SYMBOL}.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    # A hardlinked pin shares the inode with its source, so REPLACE the file rather
    # than editing it -- exactly what a writer does, and what breaks the link.
    target.unlink()
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    out = tmp_path / "report"
    report = historical.run(bundle, pinned_root, out)

    result = _only(report)
    assert result.status == ReplayStatus.COVERAGE_REVISION_UNKNOWN, (result.status,
                                                                     result.detail)
    assert "no longer matches the hash the pin recorded" in result.detail
    payload = json.loads(
        (out / f"{ReplayStatus.CAPABILITY_HISTORICAL}.json").read_text(encoding="utf-8"))
    assert payload["historical"]["pin_drifted_files"] == [
        f"fmp_history/past_earnings_quarterly__{SYMBOL}.json"]


def test_a_pin_manifest_from_another_version_is_refused(bundle, pinned_root, tmp_path):
    from ba2_providers.warm import roots

    manifest_path = pinned_root / roots.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = roots.MANIFEST_VERSION + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(historical.HistoricalRunError, match="manifest version"):
        historical.run(bundle, pinned_root, tmp_path / "report")


# --------------------------------------------------------------------------- #
# 19. The CLI
# --------------------------------------------------------------------------- #
def test_the_cli_refuses_a_cache_root_that_is_not_a_directory(bundle, tmp_path):
    """A bad --cache-root exits like a bad --bundle, not with a traceback."""
    from argparse import Namespace

    import ba2test_launcher

    args = Namespace(replay_cmd="historical", bundle=str(bundle),
                     cache_root=str(tmp_path / "nope"), out=None, timeout=None)

    with pytest.raises(SystemExit) as caught:
        ba2test_launcher._cmd_replay(args)
    assert "is not a directory" in str(caught.value)


def test_the_cli_runs_the_historical_capability(bundle, pinned_root, tmp_path):
    from argparse import Namespace

    import ba2test_launcher

    args = Namespace(replay_cmd="historical", bundle=str(bundle),
                     cache_root=str(pinned_root), out=str(tmp_path / "cli-report"),
                     timeout=None)

    assert ba2test_launcher._cmd_replay(args) == 0
    assert (tmp_path / "cli-report" /
            f"{ReplayStatus.CAPABILITY_HISTORICAL}.md").exists()


def test_the_warm_plan_is_built_once_per_distinct_configuration(bundle, pinned_root):
    """``planner.plan`` rescans the whole root; a session is one config over many symbols."""
    session_bundle = load_bundle(bundle)
    analysis = session_bundle.analyses[0]
    settings = session_bundle.decode(analysis.settings_object)
    plans = historical._PlanCache(historical.PinnedRoot(pinned_root))

    first = plans.plan_for(analysis, settings, EVAL)
    again = plans.plan_for(analysis, settings, EVAL)
    other_day = plans.plan_for(analysis, settings, EVAL + timedelta(days=1))

    assert again is first
    assert other_day is not first


# --------------------------------------------------------------------------- #
# 20. Every diff a delta-aware comparison produces has to SURVIVE the report
# --------------------------------------------------------------------------- #
def _report_for(bundle_dir, result_diffs, tmp_path):
    """Render one synthetic row through the full report machinery."""
    from app.services.replay.report import AnalysisResult, ReplayReport

    analysis = load_bundle(bundle_dir).analyses[0]
    result = AnalysisResult.for_analysis(
        analysis, ReplayStatus.COVERAGE_DIFFERENCE,
        historical._detail_for(result_diffs), result_diffs)
    report = ReplayReport(
        session_id="s", bundle_dir=str(bundle_dir),
        capability=ReplayStatus.CAPABILITY_HISTORICAL, results=[result],
        stage_results=historical._stage_results([result]))
    report.write(tmp_path)
    return report


def test_a_bundle_key_the_reconstruction_did_not_produce_reaches_the_report(bundle,
                                                                            tmp_path):
    """A key only one side has yields a diff with NO numeric distance -- and must not
    be a different TYPE from the ones that have one.

    ``compare_values(with_deltas=True)`` used to fall back to a bare triple for an
    absent key, a length mismatch and a frame type mismatch. The row detail then
    asked every diff for its ``delta_text()`` and an ``AttributeError`` escaped the
    per-analysis containment, taking the whole report with it AFTER the child had
    already done all the work.
    """
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"a": 1}, {"a": 1, "b": 2}, "inputs", with_deltas=True)

    assert [d.field for d in diffs] == ["inputs['b']"]
    assert diffs[0].abs_delta is None
    assert diffs[0].delta_text() == ""
    report = _report_for(bundle, diffs, tmp_path)
    assert "inputs['b']" in report.to_markdown()


def test_a_length_mismatch_reaches_the_report(bundle, tmp_path):
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"a": [1]}, {"a": [1, 2]}, "inputs", with_deltas=True)

    assert [d.field for d in diffs] == ["inputs['a'] (length)"]
    report = _report_for(bundle, diffs, tmp_path)
    assert "(length)" in report.to_markdown()


def test_a_frame_type_mismatch_reaches_the_report(bundle, tmp_path):
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"a": pd.DataFrame({"x": [1]})}, {"a": pd.Series([1])},
                           "inputs", with_deltas=True)

    assert [d.field for d in diffs] == ["inputs['a']"]
    assert diffs[0].abs_delta is None
    _report_for(bundle, diffs, tmp_path)


def test_a_recommendation_compared_against_the_wrong_type_reaches_the_report(bundle,
                                                                             tmp_path):
    from app.services.replay.expert_replay import compare_recommendations

    recorded = load_bundle(bundle).decode(
        load_bundle(bundle).analyses[0].recommendation_object)
    diffs = compare_recommendations(recorded, "not a recommendation", with_deltas=True)

    assert [d.field for d in diffs] == ["type"]
    _report_for(bundle, diffs, tmp_path)


# --------------------------------------------------------------------------- #
# 21. A distance that is not a number is not a distance
# --------------------------------------------------------------------------- #
def test_a_nan_leaf_reports_no_delta_and_still_serializes(bundle, tmp_path):
    """``report.write`` dumps with ``allow_nan=False``: a NaN delta kills the file.

    And it would be meaningless anyway -- "how far did it move" has no answer when
    one side is not a number. The row says so explicitly instead of carrying NaN.
    """
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"x": float("nan")}, {"x": 5.0}, "inputs", with_deltas=True)

    assert diffs[0].abs_delta is None
    assert diffs[0].rel_delta is None
    assert diffs[0].delta_not_finite is True
    assert "not finite" in diffs[0].delta_text()

    report = _report_for(bundle, diffs, tmp_path)
    payload = json.loads(
        (tmp_path / f"{ReplayStatus.CAPABILITY_HISTORICAL}.json").read_text(
            encoding="utf-8"))
    assert payload["results"][0]["field_diffs"][0]["delta_not_finite"] is True


def test_a_nan_cell_does_not_poison_a_frames_largest_distance(bundle, tmp_path):
    """One NaN cell used to make the whole frame's absolute distance NaN."""
    from app.services.replay.expert_replay import compare_values

    left = pd.DataFrame({"Close": [10.0, float("nan"), 30.0]})
    right = pd.DataFrame({"Close": [10.0, 22.0, 31.0]})

    diffs = compare_values(left, right, "inputs", with_deltas=True)

    assert diffs[0].abs_delta == pytest.approx(1.0)
    assert diffs[0].rel_delta == pytest.approx(1.0 / 30.0)
    _report_for(bundle, diffs, tmp_path)


def test_an_all_nan_frame_column_reports_no_distance_at_all(bundle, tmp_path):
    from app.services.replay.expert_replay import compare_values

    left = pd.DataFrame({"Close": [float("nan"), float("nan")]})
    right = pd.DataFrame({"Close": [1.0, 2.0]})

    diffs = compare_values(left, right, "inputs", with_deltas=True)

    assert diffs[0].abs_delta is None
    _report_for(bundle, diffs, tmp_path)


# --------------------------------------------------------------------------- #
# 22. Two attempts of one analysis are two rows, at every stage
# --------------------------------------------------------------------------- #
def duplicate_attempt(bundle_dir, analysis_id: str, attempt_id: str) -> None:
    """Add a SECOND recorded attempt of one analysis to the manifest.

    A re-run of a live analysis is a second attempt (the index keys analyses on
    ``(analysis_id, attempt_id)``), and both are rows the report has to carry.
    """
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = next(a for a in manifest["analyses"] if a["analysis_id"] == analysis_id)
    manifest["analyses"].append({**entry, "attempt_id": attempt_id})
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, ensure_ascii=False),
        encoding="utf-8")


def test_two_attempts_of_one_analysis_are_two_rows_at_every_stage(bundle, pinned_root,
                                                                  tmp_path):
    """Per-stage counts keyed on the analysis id silently collapse a retry.

    The report then holds two rows while a stage holds one, and the totals guard
    turns that into a ValueError out of ``to_markdown`` -- i.e. a retried analysis
    made the report unrenderable.
    """
    duplicate_attempt(bundle, ANALYSIS_ID, "second-attempt")
    out = tmp_path / "report"

    report = historical.run(bundle, pinned_root, out)

    assert report.total == 2
    assert [r.status for r in report.results] == [ReplayStatus.COVERAGE_MATCH] * 2
    rows = {stage: status for stage, _f, status, _c in report.stage_rows()}
    assert rows[historical.STAGE_EXPERT_INPUTS] == "match 2"
    assert rows[historical.STAGE_RECOMMENDATION] == "match 2"
    assert (out / f"{ReplayStatus.CAPABILITY_HISTORICAL}.md").exists()


# --------------------------------------------------------------------------- #
# 23. The report is printed to a console, and consoles are not all UTF-8
# --------------------------------------------------------------------------- #
def test_the_delta_text_is_ascii(bundle, tmp_path):
    """``ba2-test replay historical`` prints the markdown; cp1252 cannot encode U+0394."""
    from app.services.replay.expert_replay import compare_values

    diffs = compare_values({"x": 1.2}, {"x": 1.1}, "inputs", with_deltas=True)
    text = diffs[0].delta_text()

    assert text.encode("cp1252")
    assert text.startswith("abs ")
    markdown = _report_for(bundle, diffs, tmp_path).to_markdown()
    assert markdown.encode("cp1252")


def test_a_module_imported_inside_the_context_is_restored_too(monkeypatch):
    """A reconstruction imports provider modules lazily, INSIDE the context.

    Restoring only what was bound at entry would leave such a module holding the
    offline closure for the rest of the process -- refusing every settings read
    long after the replay finished.
    """
    import sys
    import types

    import ba2_common.config as config

    original = config.get_app_setting
    monkeypatch.setitem(sys.modules, "ba2_fake_late_import", None)

    with historical.offline_credentials():
        late = types.ModuleType("ba2_fake_late_import")
        # what ``from ba2_common.config import get_app_setting`` binds right now
        late.get_app_setting = config.get_app_setting
        sys.modules["ba2_fake_late_import"] = late
        assert late.get_app_setting("FMP_API_KEY") == historical.OFFLINE_API_KEY

    assert late.get_app_setting is original
    assert config.get_app_setting is original
