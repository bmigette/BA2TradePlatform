"""M5: an older reader opening an index that holds rows of a NEWER schema lists what it can read per row,
and reports (never hides) the rows it skipped. Strict consumers (export, retention) keep refusing."""
import json
from datetime import datetime, timezone

import pytest

from ba2_common.core.replay.schemas import AnalysisRecord, ReplayStatus, SessionRecord
from ba2_common.core.replay.service import ReplayStore

NOW = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = ReplayStore(tmp_path / "store", writer="sync")
    s.begin_session(SessionRecord(
        session_id="s1", instance_id="i", started_at=NOW, exchange_tz="America/New_York",
        app_version="x", package_versions={}, source_revision="abc", dirty=False))
    for n in range(3):
        s.submit(AnalysisRecord(
            analysis_id=f"a{n}", attempt_id=f"t{n}", session_id="s1", expert_class="FMPRating",
            expert_instance_id=1, symbol="AAPL", use_case=ReplayStatus.USE_CASE_ENTER_MARKET,
            started_at=NOW, finished_at=NOW, outcome=ReplayStatus.OUTCOME_SKIP, skip_reason="t"))
    # a row written by a NEWER schema: one field this reader has never heard of
    conn = s.index._connection()
    row = conn.execute("SELECT payload FROM analyses WHERE analysis_id = 'a1'").fetchone()
    payload = json.loads(row["payload"])
    payload["field_from_schema_4"] = {"x": 1}
    conn.execute("UPDATE analyses SET payload = ? WHERE analysis_id = 'a1'", (json.dumps(payload),))
    yield s
    s.close(timeout=2.0)


def test_the_strict_listing_still_refuses_a_newer_row(store):
    with pytest.raises(ValueError, match="unknown field"):
        store.index.analyses("s1")


def test_the_tolerant_listing_skips_per_row_and_reports_each_skip(store):
    skipped = []
    records = store.index.analyses("s1", skipped=skipped)
    assert [r.analysis_id for r in records] == ["a0", "a2"]
    assert [s["analysis_id"] for s in skipped] == ["a1"]
    assert "field_from_schema_4" in skipped[0]["error"]


def test_export_stays_strict(store, tmp_path):
    with pytest.raises(ValueError, match="unknown field"):
        store.export_session("s1", tmp_path / "out")
