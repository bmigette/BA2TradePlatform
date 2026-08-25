"""Age-based retention for ``trade_action_result``: BLANK the payload after a short
window, DELETE the row after a long one.

WHY THIS EXISTS. ``trade_action_result`` is the largest table on the user's live
database — 12,221 rows, 189.7 MB, 15.2 kB per row — and 188.2 MB of that is a single
``data`` JSON column. Nothing ever removed it: the Settings → Cleanup tool only drops a
``trade_action_result`` row when it is ORPHANED or when its parent ``ExpertRecommendation``
is deleted, and ``execute_cleanup`` refuses to delete an analysis whose transaction is
still open. One long-held position therefore pins its whole chain of records forever.

THE FOUR PROPERTIES THAT MATTER, each with tests rather than a comment:

1. A BLANKED PAYLOAD MUST NOT LOOK LIKE "NO PAYLOAD". This codebase's dominant bug class
   is an *unknown* read as a confident *zero*. Blanking ``data`` to ``{}`` or ``None``
   would make "this action recorded nothing" and "the payload was reclaimed for age"
   indistinguishable six months later. So blanking writes a self-describing sentinel, and
   every reader of ``data`` is required to tell the two apart. Both directions are
   asserted: a sentinel is never read as a real payload, and an empty payload is never
   reported as redacted.

2. THE TWO WINDOWS ARE INDEPENDENT AND NOT INTERCHANGEABLE. Swapping them deletes at 30
   days what should have been blanked, which is unrecoverable. Both boundaries are pinned
   to the microsecond on both sides, and a row that lands between the windows is asserted
   to be blanked *and still present*.

3. UNMEASURABLE IS NOT ZERO. A row with no ``created_at`` has an UNKNOWN age. It is
   neither ancient (deleted) nor brand new (silently never cleaned): it is left alone and
   COUNTED, so the operator can see it exists.

4. BULK, NOT ROW BY ROW. The previous activity-log purge materialised every matching row
   and deleted them one at a time (1.2 s, 143 MB heap). Both statements here are single
   ``UPDATE``/``DELETE``s, and the counts come from the driver's ``rowcount`` — not from a
   ``SELECT`` that would still report "12,221 blanked" if the ``UPDATE`` did nothing.

HOW IT RUNS. TIME IS FROZEN and deliberately not today (2025-03-17): this is date-window
logic, and a test measured against the system clock passes for the wrong reason. Never
``caplog`` — ``logger.py`` sets ``propagate = False``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, func, select

from ba2_trade_platform.core import cleanup as cleanup_mod
from ba2_trade_platform.core.cleanup import (
    DEFAULT_TRADE_ACTION_RESULT_BLANK_DAYS,
    DEFAULT_TRADE_ACTION_RESULT_DELETE_DAYS,
    REDACTED_AT_KEY,
    REDACTED_ORIGINAL_BYTES_KEY,
    TRADE_ACTION_RESULT_BLANK_DAYS_ENV,
    TRADE_ACTION_RESULT_DELETE_DAYS_ENV,
    describe_redaction,
    execute_trade_action_result_retention,
    is_redacted_payload,
    make_redaction_sentinel,
    payload_evaluation_details,
    preview_trade_action_result_retention,
    resolve_trade_action_result_blank_days,
    resolve_trade_action_result_delete_days,
)
from ba2_trade_platform.core.models import (
    ActivityLog,
    ExpertRecommendation,
    MarketAnalysis,
    TradeActionResult,
    TradingOrder,
    Transaction,
)
from ba2_trade_platform.core.types import (
    ActivityLogSeverity,
    ActivityLogType,
    MarketAnalysisStatus,
    OrderDirection,
    OrderRecommendation,
    OrderType,
    RiskLevel,
    TimeHorizon,
    TransactionStatus,
)

# Frozen, and deliberately not the system clock.
NOW = datetime(2025, 3, 17, 11, 45, 0, tzinfo=timezone.utc)

BLANK_DAYS = 30
DELETE_DAYS = 180

#: A payload big enough that "did the bytes actually go?" is a visible question.
REAL_PAYLOAD = {"evaluation_details": {"rule_evaluations": [{"name": "r1", "passed": True}]},
                "calculation_preview": {"tp": 1.25},
                "pad": "x" * 400}


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(test_engine):
    """The conftest in-memory engine, already wired in by ``patch_db_engine``."""
    return test_engine


#: The live schema is ``created_at DATETIME NOT NULL``, so a NULL timestamp cannot be
#: inserted through it TODAY. It can still arrive: a partial restore, a hand-run
#: migration, a column added by a future change with no backfill. "Unmeasurable" must
#: never silently resolve to "ancient" (deleted) or "brand new" (never cleaned), and a
#: property that is only true because a constraint currently forbids the input is a
#: property nobody notices losing. So these tests recreate the table WITHOUT the
#: constraint and assert the retention code's own behaviour. The next test's autouse
#: ``reset_test_db`` puts the strict schema back.
_RELAXED_TRADE_ACTION_RESULT_DDL = """
CREATE TABLE trade_action_result (
    id INTEGER NOT NULL,
    action_type VARCHAR NOT NULL,
    success BOOLEAN NOT NULL,
    message VARCHAR NOT NULL,
    data JSON,
    created_at DATETIME,
    expert_recommendation_id INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(expert_recommendation_id) REFERENCES expertrecommendation (id) ON DELETE CASCADE
)
"""


@pytest.fixture
def nullable_created_at(engine):
    """Recreate ``trade_action_result`` with a NULLABLE ``created_at``. See above."""
    with Session(engine) as s:
        s.execute(text("DROP TABLE trade_action_result"))
        s.execute(text(_RELAXED_TRADE_ACTION_RESULT_DDL))
        s.execute(text("CREATE INDEX ix_trade_action_result_created_at "
                       "ON trade_action_result (created_at)"))
        s.execute(text("CREATE INDEX ix_trade_action_result_expert_recommendation_id "
                       "ON trade_action_result (expert_recommendation_id)"))
        s.commit()
    yield


def _make_recommendation(session, *, analysis_id=None) -> ExpertRecommendation:
    rec = ExpertRecommendation(
        instance_id=1,
        market_analysis_id=analysis_id,
        symbol="AAPL",
        recommended_action=OrderRecommendation.BUY,
        expected_profit_percent=5.0,
        price_at_date=100.0,
        details="d",
        confidence=70.0,
        risk_level=RiskLevel.MEDIUM,
        time_horizon=TimeHorizon.SHORT_TERM,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


def _open_transaction_for(session, rec: ExpertRecommendation, status=TransactionStatus.OPENED):
    """Chain rec -> TradingOrder -> Transaction(status)."""
    txn = Transaction(symbol=rec.symbol, quantity=1.0, side=OrderDirection.BUY,
                      status=status, expert_id=1)
    session.add(txn)
    session.commit()
    session.refresh(txn)
    order = TradingOrder(account_id=1, symbol=rec.symbol, quantity=1.0,
                         side=OrderDirection.BUY, order_type=OrderType.MARKET,
                         good_for=1, filled_qty=0.0, comment="c",
                         transaction_id=txn.id, expert_recommendation_id=rec.id)
    session.add(order)
    session.commit()
    return txn


def _add_result(session, *, age_days=None, created_at="unset", data=REAL_PAYLOAD,
                rec=None, action_type="OPEN", success=True, message="msg"):
    """Insert one TradeActionResult. ``age_days`` is measured back from ``NOW``."""
    if created_at == "unset":
        created_at = None if age_days is None else NOW - timedelta(days=age_days)
    if rec is None:
        rec = _make_recommendation(session)
    row = TradeActionResult(action_type=action_type, success=success, message=message,
                            data=data, expert_recommendation_id=rec.id)
    session.add(row)
    session.commit()
    session.refresh(row)
    # created_at has a default_factory; set it explicitly afterwards (including to NULL,
    # which the ORM's non-nullable declaration will not do for us).
    session.execute(text("UPDATE trade_action_result SET created_at = :ts WHERE id = :i"),
                    {"ts": None if created_at is None
                     else created_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"),
                     "i": row.id})
    session.commit()
    return row.id


def _payload(engine, row_id):
    with Session(engine) as s:
        raw = s.execute(text("SELECT data FROM trade_action_result WHERE id = :i"),
                        {"i": row_id}).scalar()
    return None if raw is None else json.loads(raw)


def _ids(engine):
    with Session(engine) as s:
        return sorted(s.exec(select(TradeActionResult.id)).all())


def _count(engine, model):
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model)).one()


def _run(**kw):
    kw.setdefault("blank_days", BLANK_DAYS)
    kw.setdefault("delete_days", DELETE_DAYS)
    kw.setdefault("now", NOW)
    return execute_trade_action_result_retention(**kw)


def _preview(**kw):
    kw.setdefault("blank_days", BLANK_DAYS)
    kw.setdefault("delete_days", DELETE_DAYS)
    kw.setdefault("now", NOW)
    return preview_trade_action_result_retention(**kw)


def _capture(monkeypatch, level):
    messages = []
    monkeypatch.setattr(cleanup_mod.logger, level, lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ---------------------------------------------------------------------------
# 1. The sentinel: "reclaimed for age" must never collapse into "no data"
# ---------------------------------------------------------------------------

def test_sentinel_is_not_empty_and_empty_is_not_a_sentinel():
    """The whole point. ``{}``/``None`` must NOT read as redacted, and the sentinel must
    NOT read as an ordinary (or empty) payload."""
    sentinel = make_redaction_sentinel(original_bytes=1234, when=NOW)

    assert is_redacted_payload(sentinel) is True
    assert is_redacted_payload({}) is False
    assert is_redacted_payload(None) is False
    assert is_redacted_payload({"evaluation_details": {}}) is False
    # A sentinel is a dict, so a truthiness test alone would not tell them apart.
    assert sentinel != {}
    assert sentinel is not None


def test_sentinel_records_when_and_how_much_was_lost():
    """It is not enough to know a payload was dropped; the size and date are the facts a
    later reader needs to decide whether the loss mattered."""
    sentinel = make_redaction_sentinel(original_bytes=15_209, when=NOW)

    assert sentinel[REDACTED_AT_KEY] == NOW.isoformat()
    assert sentinel[REDACTED_ORIGINAL_BYTES_KEY] == 15_209
    # Self-describing on sight, and greppable.
    assert "_redacted_for_age" in json.dumps(sentinel)


def test_sentinel_never_reads_as_evaluation_details():
    """``payload_evaluation_details`` is the ONE accessor every reader uses. A sentinel
    must yield ``None`` even if it somehow also carries the key, or a UI would render a
    redaction marker as a rule evaluation."""
    real = {"evaluation_details": {"rule_evaluations": []}}
    assert payload_evaluation_details(real) == {"rule_evaluations": []}
    assert payload_evaluation_details({}) is None
    assert payload_evaluation_details(None) is None
    assert payload_evaluation_details(make_redaction_sentinel(1, NOW)) is None

    poisoned = dict(make_redaction_sentinel(1, NOW))
    poisoned["evaluation_details"] = {"rule_evaluations": ["nonsense"]}
    assert payload_evaluation_details(poisoned) is None


def test_describe_redaction_only_speaks_for_redacted_payloads():
    """A real payload and an empty one must produce NO redaction sentence — otherwise the
    UI tells the user data was reclaimed when nothing was."""
    described = describe_redaction(make_redaction_sentinel(2048, NOW))
    assert described is not None
    assert "2025-03-17" in described
    assert "2048" in described or "2,048" in described or "2.0 kB" in described

    assert describe_redaction({}) is None
    assert describe_redaction(None) is None
    assert describe_redaction({"evaluation_details": {}}) is None


def test_redaction_size_is_never_invented(engine):
    """``_original_bytes`` is a MEASUREMENT. A sentinel whose size cannot be measured must
    say so rather than claim zero bytes were lost."""
    sentinel = make_redaction_sentinel(original_bytes=None, when=NOW)
    assert sentinel[REDACTED_ORIGINAL_BYTES_KEY] is None
    assert describe_redaction(sentinel) is not None
    assert "unknown" in describe_redaction(sentinel).lower()


# ---------------------------------------------------------------------------
# 2. Boundaries, pinned to the microsecond
# ---------------------------------------------------------------------------

def test_row_exactly_blank_days_old_is_kept_intact(engine):
    """"Keep 30 days" is a CLOSED window: a row whose age is exactly 30 days is KEPT."""
    with Session(engine) as s:
        exact = _add_result(s, created_at=NOW - timedelta(days=BLANK_DAYS))
        older = _add_result(s, created_at=NOW - timedelta(days=BLANK_DAYS, microseconds=1))

    result = _run()

    assert result["rows_blanked"] == 1
    assert _payload(engine, exact) == REAL_PAYLOAD
    assert is_redacted_payload(_payload(engine, older))


def test_row_exactly_delete_days_old_is_kept(engine):
    """Same closed window on the delete side, one microsecond either way."""
    with Session(engine) as s:
        exact = _add_result(s, created_at=NOW - timedelta(days=DELETE_DAYS))
        older = _add_result(s, created_at=NOW - timedelta(days=DELETE_DAYS, microseconds=1))

    result = _run()

    assert result["rows_deleted"] == 1
    assert _ids(engine) == [exact]
    # ...and the survivor was blanked, not left whole: it is well past the blank window.
    assert is_redacted_payload(_payload(engine, exact))


# ---------------------------------------------------------------------------
# 3. The two windows are independent and NOT interchangeable
# ---------------------------------------------------------------------------

def test_windows_are_not_swapped(engine):
    """Swapping the windows would delete at 30 days and blank at 180 — unrecoverable.
    A row between the two windows must be BLANKED AND STILL PRESENT."""
    with Session(engine) as s:
        recent = _add_result(s, age_days=5)
        middle = _add_result(s, age_days=60)
        ancient = _add_result(s, age_days=400)

    result = _run()

    assert _ids(engine) == sorted([recent, middle])
    assert _payload(engine, recent) == REAL_PAYLOAD
    assert is_redacted_payload(_payload(engine, middle))
    assert result["rows_deleted"] == 1
    assert result["rows_blanked"] == 1
    assert ancient not in _ids(engine)


def test_rows_past_both_windows_are_deleted_not_merely_blanked(engine):
    """Delete wins. A 400-day-old row must not survive as a blanked husk."""
    with Session(engine) as s:
        ancient = _add_result(s, age_days=400)

    result = _run()

    assert _ids(engine) == []
    assert result["rows_deleted"] == 1
    # It was deleted outright, not blanked first and then deleted (which would double-count).
    assert result["rows_blanked"] == 0
    assert ancient not in _ids(engine)


def test_recent_rows_are_never_touched(engine):
    """The ``WHERE`` clause. Dropping it would blank or empty the whole table."""
    with Session(engine) as s:
        ids = [_add_result(s, age_days=age) for age in (0, 1, 7, 29)]

    result = _run()

    assert _ids(engine) == sorted(ids)
    for row_id in ids:
        assert _payload(engine, row_id) == REAL_PAYLOAD
    assert result["rows_blanked"] == 0
    assert result["rows_deleted"] == 0


def test_neighbouring_tables_are_untouched(engine):
    """A statement that names the wrong table passes a test that only counts what is
    gone from the right one."""
    with Session(engine) as s:
        analysis = MarketAnalysis(expert_instance_id=1, symbol="AAPL",
                                  status=MarketAnalysisStatus.COMPLETED)
        s.add(analysis)
        s.commit()
        s.refresh(analysis)
        rec = _make_recommendation(s, analysis_id=analysis.id)
        _open_transaction_for(s, rec)
        s.add(ActivityLog(created_at=NOW - timedelta(days=900),
                          severity=ActivityLogSeverity.INFO,
                          type=ActivityLogType.APPLICATION_STATUS_CHANGE,
                          description="keep me", data={}))
        s.commit()
        _add_result(s, age_days=400, rec=rec)

    _run()

    assert _count(engine, TradeActionResult) == 0
    assert _count(engine, ExpertRecommendation) == 1
    assert _count(engine, MarketAnalysis) == 1
    assert _count(engine, TradingOrder) == 1
    assert _count(engine, Transaction) == 1
    assert _count(engine, ActivityLog) == 1


# ---------------------------------------------------------------------------
# 4. Blanking keeps the row and every scalar fact about it
# ---------------------------------------------------------------------------

def test_blanking_keeps_the_row_and_all_its_summary_fields(engine):
    """The reclaim is worth having only because the SUMMARY survives: the id, the foreign
    key, the timestamp, and the three scalar columns that say what happened."""
    when = NOW - timedelta(days=90)
    with Session(engine) as s:
        rec = _make_recommendation(s)
        rec_id = rec.id
        row_id = _add_result(s, created_at=when, rec=rec, action_type="CLOSE_POSITION",
                             success=False, message="broker rejected the order")

    _run()

    with Session(engine) as s:
        row = s.get(TradeActionResult, row_id)
        assert row is not None
        assert row.action_type == "CLOSE_POSITION"
        assert row.success is False
        assert row.message == "broker rejected the order"
        assert row.expert_recommendation_id == rec_id
        assert row.created_at.replace(tzinfo=None) == when.replace(tzinfo=None)
        assert is_redacted_payload(row.data)


def test_blanking_actually_writes_the_sentinel_to_the_database(engine):
    """The count must come from the driver's ``rowcount``, not from the SELECT that
    chose the rows: an ``UPDATE`` that silently matched nothing would still report a
    healthy number. So the DATABASE is asserted, not the return value alone."""
    with Session(engine) as s:
        row_id = _add_result(s, age_days=90)

    result = _run()

    stored = _payload(engine, row_id)
    assert is_redacted_payload(stored)
    assert stored[REDACTED_ORIGINAL_BYTES_KEY] == len(json.dumps(REAL_PAYLOAD))
    assert stored[REDACTED_AT_KEY] == NOW.isoformat()
    assert result["rows_blanked"] == 1


def test_blanking_reclaims_the_payload_bytes(engine):
    """The saving is the point. Report it from a real measurement, and make it visible."""
    with Session(engine) as s:
        row_id = _add_result(s, age_days=90)

    before = len(json.dumps(REAL_PAYLOAD))
    result = _run()
    after = len(json.dumps(_payload(engine, row_id)))

    assert after < before
    assert result["payload_bytes_freed"] == before - after


def test_blanking_leaves_an_empty_payload_alone(engine):
    """"This action recorded no data" is a FACT. Overwriting it with a redaction sentinel
    would invent a loss that never happened — and reclaim nothing."""
    with Session(engine) as s:
        empty = _add_result(s, age_days=90, data={})
        nulled = _add_result(s, age_days=90, data=None)

    result = _run()

    assert _payload(engine, empty) == {}
    assert _payload(engine, nulled) is None
    assert result["rows_blanked"] == 0
    assert result["rows_empty_payload"] == 2


def test_blanking_is_idempotent_and_does_not_overwrite_the_recorded_size(engine):
    """A second pass must not re-measure the SENTINEL and claim the original payload was
    ~90 bytes. That would destroy the only surviving record of what was lost."""
    with Session(engine) as s:
        row_id = _add_result(s, age_days=90)

    first = _run()
    recorded = _payload(engine, row_id)[REDACTED_ORIGINAL_BYTES_KEY]
    second_preview = _preview(now=NOW + timedelta(days=1))
    second = _run(now=NOW + timedelta(days=1))

    assert first["rows_blanked"] == 1
    assert second["rows_blanked"] == 0
    assert second["rows_already_redacted"] == 1
    # The preview has to say the same thing, or a second Preview click reports 1 row of
    # work left to do forever and the operator keeps pressing Apply.
    assert second_preview["rows_to_blank"] == 0
    assert second_preview["rows_already_redacted"] == 1
    assert _payload(engine, row_id)[REDACTED_ORIGINAL_BYTES_KEY] == recorded
    assert _payload(engine, row_id)[REDACTED_AT_KEY] == NOW.isoformat()


# ---------------------------------------------------------------------------
# 5. Unmeasurable is not zero: rows with no timestamp
# ---------------------------------------------------------------------------

def test_undated_row_is_not_deleted_as_ancient(engine, nullable_created_at):
    with Session(engine) as s:
        row_id = _add_result(s, created_at=None)

    result = _run()

    assert _ids(engine) == [row_id]
    assert result["rows_deleted"] == 0


def test_undated_row_is_not_blanked_as_ancient(engine, nullable_created_at):
    with Session(engine) as s:
        row_id = _add_result(s, created_at=None)

    result = _run()

    assert _payload(engine, row_id) == REAL_PAYLOAD
    assert result["rows_blanked"] == 0


def test_undated_rows_are_counted_and_reported_not_silently_skipped(
        engine, nullable_created_at, monkeypatch):
    """Left alone, but never left INVISIBLE: a row whose age cannot be established is a
    row this feature can never reclaim, and the operator has to be able to see that."""
    warnings = _capture(monkeypatch, "warning")
    with Session(engine) as s:
        _add_result(s, created_at=None)
        _add_result(s, created_at=None)
        _add_result(s, age_days=90)

    result = _run()
    preview = _preview()

    assert result["rows_undated"] == 2
    assert preview["rows_undated"] == 2
    assert any("undated" in m.lower() or "created_at" in m.lower() for m in warnings), warnings


# ---------------------------------------------------------------------------
# 6. Configuration is honoured, and bad configuration is refused
# ---------------------------------------------------------------------------

def test_windows_come_from_the_caller_not_a_hardcoded_default(engine):
    """A hardcoded 30/180 would pass every test above. Drive it from the arguments and
    assert the behaviour moves with them."""
    with Session(engine) as s:
        row_id = _add_result(s, age_days=45)

    result = _run(blank_days=60, delete_days=365)

    assert _payload(engine, row_id) == REAL_PAYLOAD, "45d row must be untouched at 60/365"
    assert result["rows_blanked"] == 0
    assert result["blank_days"] == 60
    assert result["delete_days"] == 365


def test_windows_come_from_the_environment_when_not_given(engine, monkeypatch):
    monkeypatch.setenv(TRADE_ACTION_RESULT_BLANK_DAYS_ENV, "7")
    monkeypatch.setenv(TRADE_ACTION_RESULT_DELETE_DAYS_ENV, "14")
    assert resolve_trade_action_result_blank_days() == 7
    assert resolve_trade_action_result_delete_days() == 14

    with Session(engine) as s:
        middling = _add_result(s, age_days=10)
        ancient = _add_result(s, age_days=20)

    result = execute_trade_action_result_retention(now=NOW)

    assert result["blank_days"] == 7
    assert result["delete_days"] == 14
    assert _ids(engine) == [middling]
    assert is_redacted_payload(_payload(engine, middling))
    assert ancient not in _ids(engine)


def test_defaults_are_thirty_and_one_hundred_and_eighty(monkeypatch):
    monkeypatch.delenv(TRADE_ACTION_RESULT_BLANK_DAYS_ENV, raising=False)
    monkeypatch.delenv(TRADE_ACTION_RESULT_DELETE_DAYS_ENV, raising=False)
    assert DEFAULT_TRADE_ACTION_RESULT_BLANK_DAYS == 30
    assert DEFAULT_TRADE_ACTION_RESULT_DELETE_DAYS == 180
    assert resolve_trade_action_result_blank_days() == 30
    assert resolve_trade_action_result_delete_days() == 180


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-1", "2.5", None, True])
def test_bad_configuration_is_refused_never_defaulted(monkeypatch, bad):
    """Silently falling back to 30 is how an operator who asked for 365 quietly loses a
    year of history."""
    if bad is None:
        monkeypatch.setenv(TRADE_ACTION_RESULT_BLANK_DAYS_ENV, "None")
    else:
        monkeypatch.setenv(TRADE_ACTION_RESULT_BLANK_DAYS_ENV, str(bad))
    with pytest.raises(ValueError):
        resolve_trade_action_result_blank_days()


def test_inverted_windows_are_refused(engine):
    """Delete-before-blank is never what anyone meant, and the failure mode is data loss."""
    with Session(engine) as s:
        _add_result(s, age_days=90)

    with pytest.raises(ValueError, match="(?i)delete"):
        _run(blank_days=180, delete_days=30)
    with pytest.raises(ValueError, match="(?i)delete"):
        _preview(blank_days=180, delete_days=30)

    assert _count(engine, TradeActionResult) == 1, "a refused config must change nothing"


# ---------------------------------------------------------------------------
# 7. The preview must describe exactly what the run will do
# ---------------------------------------------------------------------------

def test_preview_changes_nothing(engine):
    with Session(engine) as s:
        blankable = _add_result(s, age_days=90)
        deletable = _add_result(s, age_days=400)

    _preview()

    assert _ids(engine) == sorted([blankable, deletable])
    assert _payload(engine, blankable) == REAL_PAYLOAD


def test_preview_matches_what_execute_then_does(engine, nullable_created_at):
    with Session(engine) as s:
        for age in (1, 10, 31, 90, 179, 181, 400):
            _add_result(s, age_days=age)
        _add_result(s, age_days=90, data={})
        _add_result(s, created_at=None)

    preview = _preview()
    result = _run()

    # total_rows is the denominator the operator reads the other numbers against: it must
    # be the WHOLE table, not the subset in scope, or "3 of 3 will be blanked" hides the
    # six rows the run is not going to touch.
    assert preview["total_rows"] == 9
    assert preview["rows_to_delete"] == result["rows_deleted"] == 2
    assert preview["rows_to_blank"] == result["rows_blanked"] == 3
    assert preview["rows_empty_payload"] == result["rows_empty_payload"] == 1
    assert preview["rows_undated"] == result["rows_undated"] == 1
    assert preview["payload_bytes_to_free"] == result["payload_bytes_freed"]


def test_preview_counts_rows_taken_from_chains_with_open_transactions(engine):
    """THE VISIBLE CONSEQUENCE. Age-based retention deliberately cuts through the
    open-transaction protection that ``execute_cleanup`` honours. That is the point — a
    180-day-old action record on a still-open position is exactly what the user wants
    reclaimed — but it must never be a silent consequence."""
    with Session(engine) as s:
        open_rec = _make_recommendation(s)
        _open_transaction_for(s, open_rec, TransactionStatus.OPENED)
        closed_rec = _make_recommendation(s)
        _open_transaction_for(s, closed_rec, TransactionStatus.CLOSED)

        _add_result(s, age_days=400, rec=open_rec)
        _add_result(s, age_days=400, rec=open_rec)
        _add_result(s, age_days=400, rec=closed_rec)
        _add_result(s, age_days=90, rec=open_rec)
        _add_result(s, age_days=90, rec=closed_rec)
        _add_result(s, age_days=90, rec=closed_rec)

    preview = _preview()

    assert preview["rows_to_delete"] == 3
    assert preview["rows_to_delete_with_open_transactions"] == 2
    assert preview["rows_to_blank"] == 3
    assert preview["rows_to_blank_with_open_transactions"] == 1


def test_open_transaction_rows_really_are_cleaned(engine):
    """Ungated, on purpose. Asserted so a future "protect open transactions" change is a
    deliberate decision with a failing test, not a quiet regression."""
    with Session(engine) as s:
        rec = _make_recommendation(s)
        _open_transaction_for(s, rec, TransactionStatus.OPENED)
        blankable = _add_result(s, age_days=90, rec=rec)
        _add_result(s, age_days=400, rec=rec)

    result = _run()

    assert result["rows_deleted"] == 1
    assert result["rows_blanked"] == 1
    assert is_redacted_payload(_payload(engine, blankable))


def test_preview_reports_the_windows_and_cutoffs_it_used(engine):
    preview = _preview()
    assert preview["blank_days"] == BLANK_DAYS
    assert preview["delete_days"] == DELETE_DAYS
    assert preview["blank_cutoff"] == (NOW - timedelta(days=BLANK_DAYS)).isoformat()
    assert preview["delete_cutoff"] == (NOW - timedelta(days=DELETE_DAYS)).isoformat()


# ---------------------------------------------------------------------------
# 8. Bulk statements, and an index rather than a scan
# ---------------------------------------------------------------------------

def test_retention_runs_a_constant_number_of_statements(engine):
    """Row-by-row is the mistake the activity-log purge already made once: 24,435 ORM
    objects and 24,435 statements. Whatever the row count, the statement count must not
    move."""
    def statements_for(row_count):
        with Session(engine) as s:
            s.execute(text("DELETE FROM trade_action_result"))
            s.commit()
            rec = _make_recommendation(s)
            for i in range(row_count):
                _add_result(s, age_days=90 + i, rec=rec)

        seen = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
        try:
            _run(delete_days=1000)
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        return [s for s in seen
                if s.lstrip().upper().startswith(("UPDATE", "DELETE"))]

    few = statements_for(2)
    many = statements_for(40)

    assert len(few) == len(many), f"statement count grew with row count: {few} vs {many}"
    assert len(many) <= 2, many


def test_the_age_filter_uses_the_created_at_index(engine):
    """12k rows is small; 12k rows scanned on every preview refresh is not. There IS an
    index (``ix_trade_action_result_created_at``) — use it."""
    with Session(engine) as s:
        plan = s.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM trade_action_result "
            "WHERE created_at IS NOT NULL AND created_at < '2020-01-01 00:00:00.000000'"
        )).all()
    rendered = " ".join(str(r) for r in plan)
    assert "ix_trade_action_result_created_at" in rendered, rendered


# ---------------------------------------------------------------------------
# 9. Failure is reported, never swallowed into a happy zero
# ---------------------------------------------------------------------------

def test_a_database_failure_is_reported_not_counted_as_zero(engine, monkeypatch):
    """``{'rows_blanked': 0}`` with no error reads as "nothing needed doing". A failed run
    must be distinguishable from a clean one."""
    with Session(engine) as s:
        _add_result(s, age_days=90)

    def _explode(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cleanup_mod, "_blank_trade_action_result_payloads", _explode)
    errors = _capture(monkeypatch, "error")

    result = _run()

    assert result["error"] is not None
    assert "disk on fire" in result["error"]
    assert errors


# ---------------------------------------------------------------------------
# 10. EVERY reader of ``data`` must tell a sentinel from a real payload
#
# There are four of them, all in ``ui/pages/marketanalysis.py``, and all four used to
# ask ``'evaluation_details' in result.data``. Against a sentinel that is False —
# which happens to be the right ANSWER and the wrong REASON: the UI would say "no
# evaluation details found", the same words it uses for an action that never recorded
# any. The shared summariser below is what all four now call, so this is where the
# distinction is pinned.
# ---------------------------------------------------------------------------

def test_summariser_finds_real_evaluation_details():
    from ba2_trade_platform.core.cleanup import summarize_action_result_payloads

    summary = summarize_action_result_payloads([{}, {"evaluation_details": {"a": 1}}])

    assert summary["has_evaluation_details"] is True
    assert summary["evaluation_details"] == {"a": 1}
    assert summary["redaction_note"] is None


def test_summariser_reports_a_sentinel_as_reclaimed_not_as_absent():
    """The load-bearing case. "Never had any" and "reclaimed for age" must produce
    DIFFERENT answers, or the sentinel was pointless."""
    from ba2_trade_platform.core.cleanup import summarize_action_result_payloads

    absent = summarize_action_result_payloads([{}, {"other": 1}])
    reclaimed = summarize_action_result_payloads([make_redaction_sentinel(15_209, NOW)])

    assert absent["has_evaluation_details"] is False
    assert absent["redaction_note"] is None

    assert reclaimed["has_evaluation_details"] is False
    assert reclaimed["redaction_note"] is not None
    assert "15,209" in reclaimed["redaction_note"]
    assert absent["redaction_note"] != reclaimed["redaction_note"]


def test_summariser_never_renders_a_sentinel_as_evaluation_details():
    from ba2_trade_platform.core.cleanup import summarize_action_result_payloads

    summary = summarize_action_result_payloads([make_redaction_sentinel(10, NOW)])

    assert summary["evaluation_details"] is None


def test_summariser_prefers_surviving_details_over_a_sibling_redaction():
    """One action's payload going does not make the analysis undiagnosable. Show the
    details that DID survive, and do not cry redaction over them."""
    from ba2_trade_platform.core.cleanup import summarize_action_result_payloads

    summary = summarize_action_result_payloads(
        [make_redaction_sentinel(10, NOW), {"evaluation_details": {"a": 1}}])

    assert summary["evaluation_details"] == {"a": 1}
    assert summary["redaction_note"] is None


def test_summariser_on_nothing_at_all():
    from ba2_trade_platform.core.cleanup import summarize_action_result_payloads

    summary = summarize_action_result_payloads([])

    assert summary["has_evaluation_details"] is False
    assert summary["evaluation_details"] is None
    assert summary["redaction_note"] is None


def test_every_reader_of_the_data_column_goes_through_the_shared_accessor():
    """A NEW reader that hand-rolls ``'evaluation_details' in data`` reintroduces the
    bug in a place these tests do not reach. Assert on the source: no production module
    may test membership of the key directly."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    # Subscripting or testing membership on something called ``data`` — i.e. on the
    # payload itself. Reading ``summary['evaluation_details']`` off the accessor's own
    # return value is the CORRECT thing and is not an offence.
    patterns = (
        re.compile(r"""\bdata\s*\[\s*['"]evaluation_details['"]"""),
        re.compile(r"""['"]evaluation_details['"]\s+(?:not\s+)?in\s+\S*\bdata\b"""),
        re.compile(r"""\bdata\s*\.\s*get\s*\(\s*['"]evaluation_details['"]"""),
    )
    offenders = []
    for path in (root / "ba2_trade_platform").rglob("*.py"):
        if path.name == "cleanup.py":
            continue  # the accessor's own home
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in patterns):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "these read trade_action_result.data['evaluation_details'] directly and will "
        "read a redaction sentinel as 'no data':\n" + "\n".join(offenders))


def test_the_analysis_table_flag_distinguishes_reclaimed_from_absent(engine):
    """``_populate_evaluation_data_flags`` decides whether the magnifying glass shows.
    A redacted payload must NOT light it (there is nothing to render) but MUST set the
    separate 'reclaimed' flag, or the user is told the analysis never had a rule
    evaluation."""
    from ba2_trade_platform.ui.pages.marketanalysis import JobMonitoringTab

    cases = {
        "real": [{"evaluation_details": {"a": 1}}],
        "redacted": [make_redaction_sentinel(999, NOW)],
        "empty": [{}],
        # An analysis usually has SEVERAL action results. One of them going does not
        # make the analysis undiagnosable, and must not put a "reclaimed" marker next to
        # detail the user can still open.
        "both": [make_redaction_sentinel(999, NOW), {"evaluation_details": {"a": 1}}],
    }
    with Session(engine) as s:
        analyses = {}
        for key, payloads in cases.items():
            analysis = MarketAnalysis(expert_instance_id=1, symbol=key.upper(),
                                      status=MarketAnalysisStatus.COMPLETED)
            s.add(analysis)
            s.commit()
            s.refresh(analysis)
            rec = _make_recommendation(s, analysis_id=analysis.id)
            for payload in payloads:
                _add_result(s, age_days=1, rec=rec, data=payload)
            analyses[key] = analysis.id

    rows = [{'id': analyses[k], 'has_evaluation_data': False} for k in cases]
    tab = object.__new__(JobMonitoringTab)   # the method touches only the DB
    tab._populate_evaluation_data_flags(rows)
    by_id = {r['id']: r for r in rows}

    assert by_id[analyses["real"]]['has_evaluation_data'] is True
    assert by_id[analyses["real"]]['evaluation_redacted'] is False

    assert by_id[analyses["redacted"]]['has_evaluation_data'] is False
    assert by_id[analyses["redacted"]]['evaluation_redacted'] is True

    assert by_id[analyses["empty"]]['has_evaluation_data'] is False
    assert by_id[analyses["empty"]]['evaluation_redacted'] is False

    assert by_id[analyses["both"]]['has_evaluation_data'] is True
    assert by_id[analyses["both"]]['evaluation_redacted'] is False


def test_the_recommendations_table_flag_distinguishes_reclaimed_from_absent(engine):
    """Same decision on the Order Recommendations tab, which builds its own flags."""
    from ba2_trade_platform.ui.pages.marketanalysis import OrderRecommendationsTab
    from tests.factories import create_account_definition, create_expert_instance

    account = create_account_definition()
    expert = create_expert_instance(account_id=account.id, expert="MockExpert")

    with Session(engine) as s:
        analysis = MarketAnalysis(expert_instance_id=expert.id, symbol="AAPL",
                                  status=MarketAnalysisStatus.COMPLETED)
        s.add(analysis)
        s.commit()
        s.refresh(analysis)
        rec = ExpertRecommendation(
            instance_id=expert.id, market_analysis_id=analysis.id, symbol="AAPL",
            recommended_action=OrderRecommendation.BUY, expected_profit_percent=5.0,
            price_at_date=100.0, details="d", confidence=70.0,
            risk_level=RiskLevel.MEDIUM, time_horizon=TimeHorizon.SHORT_TERM)
        s.add(rec)
        s.commit()
        s.refresh(rec)
        _add_result(s, age_days=1, rec=rec, data=make_redaction_sentinel(4096, NOW))

    tab = object.__new__(OrderRecommendationsTab)
    tab.expert_filter = 'all'
    rows = tab._get_symbol_recommendations("AAPL")

    assert len(rows) == 1
    assert rows[0]['has_evaluation_data'] is False
    assert rows[0]['evaluation_redacted'] is True


def test_the_evaluation_dialogs_say_reclaimed_rather_than_never_existed(engine, monkeypatch):
    """Both dialogs used to notify "No evaluation details found" for a sentinel — the
    same sentence they use when an action genuinely recorded nothing."""
    from ba2_trade_platform.ui.pages import marketanalysis as ma

    notes = []
    monkeypatch.setattr(ma.ui, "notify", lambda msg, **kw: notes.append(str(msg)))

    with Session(engine) as s:
        analysis = MarketAnalysis(expert_instance_id=1, symbol="AAPL",
                                  status=MarketAnalysisStatus.COMPLETED)
        s.add(analysis)
        s.commit()
        s.refresh(analysis)
        analysis_id = analysis.id
        rec = _make_recommendation(s, analysis_id=analysis_id)
        rec_id = rec.id
        _add_result(s, age_days=1, rec=rec, data=make_redaction_sentinel(15_209, NOW))

    object.__new__(ma.JobMonitoringTab).view_rule_evaluation(analysis_id)
    object.__new__(ma.OrderRecommendationsTab)._handle_view_evaluation(rec_id)

    assert len(notes) == 2, notes
    for note in notes:
        assert "15,209" in note, note
        assert "retention" in note.lower(), note


def test_the_evaluation_dialogs_still_say_nothing_found_when_nothing_existed(engine, monkeypatch):
    """The other direction: an action that recorded no data must NOT be reported as
    reclaimed for age."""
    from ba2_trade_platform.ui.pages import marketanalysis as ma

    notes = []
    monkeypatch.setattr(ma.ui, "notify", lambda msg, **kw: notes.append(str(msg)))

    with Session(engine) as s:
        analysis = MarketAnalysis(expert_instance_id=1, symbol="AAPL",
                                  status=MarketAnalysisStatus.COMPLETED)
        s.add(analysis)
        s.commit()
        s.refresh(analysis)
        analysis_id = analysis.id
        rec = _make_recommendation(s, analysis_id=analysis_id)
        rec_id = rec.id
        _add_result(s, age_days=1, rec=rec, data={})

    object.__new__(ma.JobMonitoringTab).view_rule_evaluation(analysis_id)
    object.__new__(ma.OrderRecommendationsTab)._handle_view_evaluation(rec_id)

    assert len(notes) == 2, notes
    for note in notes:
        assert "retention" not in note.lower(), note
        assert "redact" not in note.lower(), note


# ---------------------------------------------------------------------------
# 11. Wired into the EXISTING Batch Database Cleanup tool, preview included
# ---------------------------------------------------------------------------

@pytest.fixture
def nicegui_client():
    """A slot stack, so ``ui.*`` calls have somewhere to draw."""
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page('/test-batch-cleanup-retention'), request=None)
    yield client
    client.remove_elements(client.elements.values())


def _cleanup_tab(nicegui_client):
    from ba2_trade_platform.ui.pages.settings import BatchCleanupTab
    with nicegui_client:
        return BatchCleanupTab()


def _all_text(nicegui_client):
    return " ".join(el._text for el in nicegui_client.layout.descendants(include_self=True)
                    if el._text)


def test_the_cleanup_tab_offers_the_two_retention_windows(nicegui_client):
    """Extended, not duplicated: the windows live in the tool the user already knows,
    seeded from the same resolvers the core uses."""
    tab = _cleanup_tab(nicegui_client)

    assert tab.tar_blank_days_input.value == DEFAULT_TRADE_ACTION_RESULT_BLANK_DAYS
    assert tab.tar_delete_days_input.value == DEFAULT_TRADE_ACTION_RESULT_DELETE_DAYS
    text_shown = _all_text(nicegui_client)
    assert "Trade Action Result" in text_shown


def test_the_cleanup_tab_seeds_its_windows_from_the_configuration(nicegui_client, monkeypatch):
    """Seeded from the resolvers, not from two literals that happen to equal today's
    defaults. An operator who set the env to 90/730 and is shown 30/180 will press the
    button believing it will do something it will not."""
    monkeypatch.setenv(TRADE_ACTION_RESULT_BLANK_DAYS_ENV, "90")
    monkeypatch.setenv(TRADE_ACTION_RESULT_DELETE_DAYS_ENV, "730")

    tab = _cleanup_tab(nicegui_client)

    assert tab.tar_blank_days_input.value == 90
    assert tab.tar_delete_days_input.value == 730


def test_the_cleanup_tab_previews_before_it_acts(nicegui_client, monkeypatch):
    """The preview must show BOTH windows and the open-transaction consequence, and it
    must run BEFORE anything is changed."""
    from ba2_trade_platform.ui.pages import settings as settings_mod

    canned = {
        'blank_days': 30, 'delete_days': 180,
        'blank_cutoff': '2025-02-15T11:45:00+00:00',
        'delete_cutoff': '2024-09-18T11:45:00+00:00',
        'total_rows': 12221, 'rows_to_delete': 41, 'rows_to_blank': 12180,
        'rows_already_redacted': 0, 'rows_empty_payload': 0, 'rows_undated': 3,
        'rows_to_delete_with_open_transactions': 7,
        'rows_to_blank_with_open_transactions': 908,
        'payload_bytes_to_free': 185_000_000, 'error': None,
    }
    seen = {}

    def _fake_preview(**kwargs):
        seen.update(kwargs)
        return canned

    monkeypatch.setattr(settings_mod, "preview_trade_action_result_retention", _fake_preview)

    tab = _cleanup_tab(nicegui_client)
    tab.tar_blank_days_input.value = 45
    tab.tar_delete_days_input.value = 200
    tab._preview_trade_action_result_retention()

    assert seen == {'blank_days': 45, 'delete_days': 200}
    shown = _all_text(nicegui_client)
    assert "12180" in shown or "12,180" in shown
    assert "41" in shown
    assert "908" in shown, "the preview must surface the open-transaction blank count"
    assert "7" in shown, "the preview must surface the open-transaction delete count"
    assert "3" in shown, "the preview must surface the undated rows it cannot age"


def test_the_cleanup_tab_execute_uses_the_configured_windows(nicegui_client, monkeypatch):
    from ba2_trade_platform.ui.pages import settings as settings_mod

    seen = {}

    def _fake_execute(**kwargs):
        seen.update(kwargs)
        return {'rows_deleted': 41, 'rows_blanked': 12180, 'rows_undated': 3,
                'rows_already_redacted': 0, 'rows_empty_payload': 0,
                'payload_bytes_freed': 185_000_000, 'blank_days': 45, 'delete_days': 200,
                'seconds': 1.2, 'error': None}

    monkeypatch.setattr(settings_mod, "execute_trade_action_result_retention", _fake_execute)
    monkeypatch.setattr(settings_mod.ui, "notify", lambda *a, **k: None)

    tab = _cleanup_tab(nicegui_client)
    tab.tar_blank_days_input.value = 45
    tab.tar_delete_days_input.value = 200
    with nicegui_client:
        tab._perform_trade_action_result_retention(_ClosableDialog())

    assert seen == {'blank_days': 45, 'delete_days': 200}


def test_the_cleanup_tab_reports_a_failed_retention_run(nicegui_client, monkeypatch):
    """``rows_blanked: 0`` with an error must not be announced as a clean run."""
    from ba2_trade_platform.ui.pages import settings as settings_mod

    notes = []
    monkeypatch.setattr(settings_mod, "execute_trade_action_result_retention",
                        lambda **kw: {'rows_deleted': 0, 'rows_blanked': 0,
                                      'rows_undated': 0, 'rows_already_redacted': 0,
                                      'rows_empty_payload': 0, 'payload_bytes_freed': 0,
                                      'blank_days': 30, 'delete_days': 180,
                                      'seconds': 0.0, 'error': 'database is locked'})
    monkeypatch.setattr(settings_mod.ui, "notify",
                        lambda msg, **kw: notes.append((str(msg), kw.get('type'))))

    tab = _cleanup_tab(nicegui_client)
    with nicegui_client:
        tab._perform_trade_action_result_retention(_ClosableDialog())

    assert notes, "a failed run must say so"
    assert any('database is locked' in msg for msg, _ in notes), notes
    assert any(kind == 'negative' for _, kind in notes), notes


class _ClosableDialog:
    def close(self):
        pass


# ---------------------------------------------------------------------------
# 12. Gaps found by mutation testing, closed
# ---------------------------------------------------------------------------

#: The full contract of a preview result. Callers index these keys directly; a preview
#: that omits one on its FAILURE path swaps a reported error for a KeyError in the UI.
PREVIEW_KEYS = {
    'blank_days', 'delete_days', 'blank_cutoff', 'delete_cutoff', 'total_rows',
    'rows_to_delete', 'rows_to_blank', 'rows_already_redacted', 'rows_empty_payload',
    'rows_undated', 'rows_to_delete_with_open_transactions',
    'rows_to_blank_with_open_transactions', 'payload_bytes_to_free', 'error',
}

RESULT_KEYS = {
    'blank_days', 'delete_days', 'blank_cutoff', 'delete_cutoff', 'rows_deleted',
    'rows_blanked', 'rows_already_redacted', 'rows_empty_payload', 'rows_undated',
    'payload_bytes_freed', 'seconds', 'error',
}


def test_a_failed_preview_still_returns_every_key(engine, monkeypatch):
    """A half-built dict is worse than no dict: the caller's ``preview['rows_to_delete']``
    raises KeyError and the ERROR it was told to display is never shown."""
    def _explode(*a, **k):
        raise RuntimeError("index is corrupt")

    monkeypatch.setattr(cleanup_mod, "_count_with_open_transactions", _explode)
    _capture(monkeypatch, "error")

    preview = _preview()

    assert preview["error"] is not None
    assert set(preview) == PREVIEW_KEYS, set(preview) ^ PREVIEW_KEYS


def test_a_failed_run_still_returns_every_key(engine, monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("disk is full")

    monkeypatch.setattr(cleanup_mod, "_delete_aged_trade_action_results", _explode)
    _capture(monkeypatch, "error")

    result = _run()

    assert result["error"] is not None
    assert set(result) == RESULT_KEYS, set(result) ^ RESULT_KEYS


def test_a_successful_preview_returns_every_key(engine):
    assert set(_preview()) == PREVIEW_KEYS


def test_a_successful_run_returns_every_key(engine):
    assert set(_run()) == RESULT_KEYS


def test_a_driver_that_will_not_say_how_many_rows_it_touched_is_an_error(engine):
    """``rowcount`` is ``-1`` on drivers that cannot report it, and could be ``None``.
    Both mean "unknown". Turning either into ``0`` would report a retention run that
    reclaimed nothing — the exact unknown-read-as-zero this module exists to avoid."""
    with pytest.raises(RuntimeError, match="(?i)rowcount"):
        cleanup_mod._checked_rowcount(None, "blank")
    with pytest.raises(RuntimeError, match="(?i)rowcount"):
        cleanup_mod._checked_rowcount(-1, "delete")
    # A genuine zero is a genuine measurement and passes through.
    assert cleanup_mod._checked_rowcount(0, "blank") == 0
    assert cleanup_mod._checked_rowcount(7, "delete") == 7


def test_the_reported_counts_come_from_the_driver_not_from_a_select():
    """The counts must be the number of rows the DATABASE says it changed.

    A ``SELECT COUNT(*)`` with the same ``WHERE`` returns the same number today, so no
    behavioural test can separate them — but it is the same number only for as long as
    the two predicates stay identical, and the failure when they drift is a confident
    "12,221 blanked" for an ``UPDATE`` that touched nothing. Asserted structurally
    because that is where the invariant actually lives."""
    import ast
    import inspect
    import textwrap

    for name in ("_blank_trade_action_result_payloads", "_delete_aged_trade_action_results"):
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(cleanup_mod, name))))
        returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
        assert returns, f"{name} returns nothing"
        for node in returns:
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "rowcount" in attrs, (
                f"{name} returns a count that does not come from the driver's rowcount: "
                f"{ast.unparse(node)}")


def test_the_open_transaction_count_counts_rows_not_join_paths(engine):
    """OVER-reporting is as misleading as under-reporting.

    One recommendation routinely carries several ``TradingOrder`` rows (entry, TP, SL,
    the legs of an option strategy), each pointing at the same open ``Transaction``. A
    plain ``COUNT`` over that join returns one row PER PATH, so a single action record
    behind a four-leg position is reported as four. The operator then sees "1,216 rows
    from open positions" over a table that only holds 304 such rows and cannot reconcile
    the preview against anything.

    (Found by a mutation run: the mutation that removes ``distinct`` could not be applied
    because ``distinct`` was not there.)"""
    with Session(engine) as s:
        rec = _make_recommendation(s)
        _open_transaction_for(s, rec, TransactionStatus.OPENED)   # entry
        _open_transaction_for(s, rec, TransactionStatus.OPENED)   # take-profit
        _open_transaction_for(s, rec, TransactionStatus.OPENED)   # stop-loss
        _add_result(s, age_days=90, rec=rec)
        _add_result(s, age_days=400, rec=rec)

    preview = _preview()

    assert preview["rows_to_blank"] == 1
    assert preview["rows_to_blank_with_open_transactions"] == 1
    assert preview["rows_to_delete"] == 1
    assert preview["rows_to_delete_with_open_transactions"] == 1
