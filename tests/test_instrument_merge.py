"""Merging duplicate `instrument` rows before `name` becomes unique.

The fixture database is built with RAW SQL, not SQLModel.metadata.create_all:
once `Instrument.name` is unique, create_all emits the unique index and the
duplicate rows this whole module is about could not be inserted at all. The table
definition below is the live pre-migration schema, verbatim.

These tests never touch a real database -- every one gets its own tmp_path file.
"""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine, text

from ba2_trade_platform.core.instrument_merge import (
    merge_duplicate_instruments,
    report_duplicate_instruments,
)

_CREATE = (
    "CREATE TABLE instrument ("
    " id INTEGER NOT NULL,"
    " name VARCHAR NOT NULL,"
    " instrument_type VARCHAR(6),"
    " categories JSON,"
    " labels JSON,"
    " company_name VARCHAR,"
    " PRIMARY KEY (id))"
)
_INSERT = text(
    "INSERT INTO instrument (id, name, instrument_type, categories, labels, company_name)"
    " VALUES (:id, :name, :instrument_type, :categories, :labels, :company_name)"
)


def _make_db(tmp_path, rows):
    """rows: list of (id, name, instrument_type, categories, labels, company_name)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'instruments.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        for row in rows:
            conn.execute(_INSERT, {
                "id": row[0], "name": row[1], "instrument_type": row[2],
                "categories": None if row[3] is None else json.dumps(row[3]),
                "labels": None if row[4] is None else json.dumps(row[4]),
                "company_name": row[5],
            })
    return engine


def _write_raw_labels(engine, row_id, raw):
    """Store a labels value EXACTLY as given, bypassing the fixture's json.dumps.

    The malformed values live rows can hold cannot be produced by json.dumps.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE instrument SET labels = :raw WHERE id = :id"),
                     {"raw": raw, "id": row_id})


def _dump(engine):
    with engine.connect() as conn:
        return [
            (r[0], r[1], r[2], r[3], json.loads(r[4] or "[]"), json.loads(r[5] or "[]"))
            for r in conn.execute(text(
                "SELECT id, name, instrument_type, company_name, categories, labels"
                " FROM instrument ORDER BY id"
            ))
        ]


def test_merge_keeps_lowest_id_and_coalesces_a_null_instrument_type(tmp_path):
    engine = _make_db(tmp_path, [
        (7, 'AAPL', None, [], ['ark26'], None),
        (9, 'AAPL', 'STOCK', [], [], 'Apple Inc'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1
    assert stats['rows_deleted'] == 1
    assert _dump(engine) == [(7, 'AAPL', 'STOCK', 'Apple Inc', [], ['ark26'])]


def test_merge_unions_disjoint_label_lists(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'MSFT', 'STOCK', ['Tech'], ['ark26'], None),
        (2, 'MSFT', 'STOCK', ['Software'], ['nasdaq30'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [
        (1, 'MSFT', 'STOCK', None, ['Tech', 'Software'], ['ark26', 'nasdaq30'])
    ]


def test_merge_dedupes_overlapping_label_lists_preserving_order(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'NVDA', 'STOCK', [], ['semis', 'ark26'], None),
        (2, 'NVDA', 'STOCK', [], ['ark26', 'highrisk'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine)[0][5] == ['semis', 'ark26', 'highrisk']


def test_merge_collapses_three_rows_of_one_name_into_the_lowest_id(tmp_path):
    engine = _make_db(tmp_path, [
        (5, 'TSLA', None, [], ['a'], None),
        (6, 'TSLA', 'STOCK', [], ['b'], None),
        (7, 'TSLA', None, ['EV'], ['c', 'a'], 'Tesla Inc'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['rows_deleted'] == 2
    assert _dump(engine) == [(5, 'TSLA', 'STOCK', 'Tesla Inc', ['EV'], ['a', 'b', 'c'])]


def test_merge_normalises_a_lower_case_name_into_its_upper_case_twin(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'META', 'STOCK', [], ['social'], None),
        (2, ' meta ', None, [], ['ark26'], None),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1
    assert _dump(engine) == [(1, 'META', 'STOCK', None, [], ['social', 'ark26'])]


def test_merge_normalises_a_lone_badly_cased_name_without_deleting_it(tmp_path):
    engine = _make_db(tmp_path, [(3, ' ibm ', 'STOCK', [], ['legacy'], None)])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 0
    assert stats['rows_renamed'] == 1
    assert _dump(engine) == [(3, 'IBM', 'STOCK', None, [], ['legacy'])]


def test_merge_leaves_already_unique_rows_untouched(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', 'STOCK', ['Tech'], ['ark26'], 'Apple Inc'),
        (2, 'MSFT', 'STOCK', [], [], None),
    ])
    before = _dump(engine)
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats == {'groups': 0, 'duplicate_groups': 0, 'rows_deleted': 0, 'rows_renamed': 0}
    assert _dump(engine) == before


def test_running_the_merge_twice_changes_nothing_the_second_time(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', ['Tech'], ['b'], 'Apple Inc'),
        (3, 'aapl', None, [], ['c'], None),
    ])
    with engine.begin() as conn:
        first = merge_duplicate_instruments(conn)
    after_first = _dump(engine)
    with engine.begin() as conn:
        second = merge_duplicate_instruments(conn)
    assert first['rows_deleted'] == 2
    assert second == {'groups': 0, 'duplicate_groups': 0, 'rows_deleted': 0, 'rows_renamed': 0}
    assert _dump(engine) == after_first


def test_dry_run_reports_the_same_work_but_writes_nothing(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', [], ['b'], None),
    ])
    before = _dump(engine)
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn, dry_run=True)
        plan = report_duplicate_instruments(conn)
    assert stats['duplicate_groups'] == 1 and stats['rows_deleted'] == 1
    assert plan[0]['name'] == 'AAPL'
    assert plan[0]['keep_id'] == 1 and plan[0]['delete_ids'] == [2]
    assert plan[0]['labels'] == ['a', 'b']
    assert _dump(engine) == before


def test_merge_tolerates_null_json_columns(tmp_path):
    engine = _make_db(tmp_path, [
        (1, 'GOOG', None, None, None, None),
        (2, 'GOOG', 'STOCK', None, ['x'], None),
    ])
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [(1, 'GOOG', 'STOCK', None, [], ['x'])]


def test_merge_drops_malformed_label_values_instead_of_stringifying_them(tmp_path):
    """Non-strings must be DROPPED, never coerced into plausible-looking labels.

    ``str()`` would write a JSON null through as a real label named 'None' -- and
    only into rows the migration is already rewriting, so nobody would notice.
    """
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], [], None),
        (2, 'AAPL', 'STOCK', [], [], None),
        (3, 'AAPL', None, [], [], None),
    ])
    _write_raw_labels(engine, 1, '[1, 2.5, null, true, "keeper"]')   # non-string members
    _write_raw_labels(engine, 2, 'not json at all')                  # undecodable
    _write_raw_labels(engine, 3, '{"a": 1}')                         # decodes, but not a list
    with engine.begin() as conn:
        merge_duplicate_instruments(conn)
    assert _dump(engine) == [(1, 'AAPL', 'STOCK', None, [], ['keeper'])]


def test_merge_keeps_the_lowest_ids_company_name_when_they_conflict(tmp_path):
    """Coalesce is first-non-null by id, so a conflict resolves to the keeper's."""
    engine = _make_db(tmp_path, [
        (4, 'AAPL', 'STOCK', [], [], 'Apple Inc'),
        (5, 'AAPL', 'STOCK', [], [], 'Apple Computer Inc'),
        (6, 'AAPL', 'STOCK', [], [], 'APPLE INC.'),
    ])
    with engine.begin() as conn:
        stats = merge_duplicate_instruments(conn)
    assert stats['rows_deleted'] == 2
    assert _dump(engine) == [(4, 'AAPL', 'STOCK', 'Apple Inc', [], [])]


def test_merge_writes_nothing_when_the_caller_never_commits(tmp_path):
    """The CALLER owns the transaction: connect() with no commit is a total no-op.

    Pinned because the stats come back fully populated either way -- the only
    signal that the merge was lost is the unchanged table.
    """
    engine = _make_db(tmp_path, [
        (1, 'AAPL', None, [], ['a'], None),
        (2, 'AAPL', 'STOCK', [], ['b'], None),
    ])
    before = _dump(engine)
    conn = engine.connect()
    stats = merge_duplicate_instruments(conn)
    conn.close()                        # no commit: SQLAlchemy 2.0 rolls back
    assert stats['rows_deleted'] == 1   # the work was reported...
    assert _dump(engine) == before      # ...but none of it survived


def test_merge_on_an_empty_table_is_a_no_op(tmp_path):
    engine = _make_db(tmp_path, [])
    with engine.begin() as conn:
        assert merge_duplicate_instruments(conn)['groups'] == 0
    assert _dump(engine) == []


def test_fixture_db_is_never_the_real_database(tmp_path):
    """Guard rail: the live production DB must never be opened by this module."""
    engine = _make_db(tmp_path, [])
    assert str(tmp_path) in str(engine.url)
    conn = sqlite3.connect(str(tmp_path / 'instruments.sqlite'))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {'instrument'}
