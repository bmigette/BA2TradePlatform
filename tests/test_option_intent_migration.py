"""The option-intent revision must add exactly what the model declares, twice over.

Two paths reach a database: ``init_db()``'s ``create_all`` on a brand-new one (which
then gets stamped at head), and Alembic on every existing one. They must produce the
same ``transaction`` table, and re-running the revision must be free -- the sibling
revision's runbook ends "IF IT FAILS, JUST RE-RUN IT" and this one says the same.

Every database here is a THROWAWAY under ``tmp_path``. The subprocess tests point
alembic at one with ``BA2_DB_FILE`` (not ``DB_FILE``); nothing in this file can
reach ``~/Documents/ba2/trade/db.sqlite``.
"""
import importlib.util
import os
import pathlib
import sqlite3
import subprocess

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import AssetClass, OrderDirection

ROOT = pathlib.Path(__file__).resolve().parents[1]
REV = "b2f4c81d6a35"
PARENT_REV = "a3f1c07d9e21"
REVISION_FILE = ROOT / f"alembic/versions/{REV}_option_intent_columns.py"
COLS = ("asset_class", "option_strategy", "expiry")
INDEXES = ("ix_transaction_asset_class", "ix_transaction_expiry")


# --- helpers ----------------------------------------------------------------

def _load_revision():
    spec = importlib.util.spec_from_file_location("option_intent_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(engine):
    """Apply THIS revision's ``upgrade()`` to ``engine``, as alembic would."""
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


def _downgrade(engine):
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()


def _stamp(engine, revision):
    """Write ``alembic_version`` by hand, the way ``init_db()``'s stamp does."""
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})


def _build_via_create_all(db_path, *, stamp=PARENT_REV):
    """The whole schema exactly as ``init_db()`` builds it -- the three columns included.

    This is the developer database: create_all got there first, so the table already
    has the columns and the revision must skip them rather than die on ADD COLUMN.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    if stamp:
        _stamp(engine, stamp)
    return engine


def _build_pre_migration(db_path, *, stamp=PARENT_REV):
    """A database in the shape every EXISTING one is in: no intent columns.

    Built with create_all and then stripped, because no alembic revision has ever
    created ``transaction`` -- see ca1825d61f7d, "we don't drop the transaction table
    since it was created by SQLModel". ``alembic upgrade`` from base therefore does
    not work on this repo at all and cannot be what "a clean database" means here;
    a stamped create_all database is.

    The indexes have to go first: SQLite refuses to drop a column an index mentions.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for index_name in INDEXES:
            connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        for column in COLS:
            connection.execute(text(f'ALTER TABLE "transaction" DROP COLUMN {column}'))
    assert not set(COLS) & set(_cols(str(db_path)))
    if stamp:
        _stamp(engine, stamp)
    return engine


def _alembic_upgrade_head(db_path):
    """`alembic upgrade head` in a subprocess, against a THROWAWAY file."""
    env = {**os.environ, "BA2_DB_FILE": str(db_path)}
    result = subprocess.run(
        [str(ROOT / "venv/bin/python"), "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _cols(db_path, table="transaction"):
    con = sqlite3.connect(db_path)
    try:
        return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
    finally:
        con.close()


def _column_info(db_path, table="transaction"):
    con = sqlite3.connect(db_path)
    try:
        return {row[1]: row for row in con.execute(f'PRAGMA table_info("{table}")')}
    finally:
        con.close()


def _version(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        con.close()


# --- the three end-to-end scenarios, through the real alembic CLI -----------

def test_a_database_without_the_columns_gains_all_three(tmp_path):
    db = tmp_path / "clean.sqlite"
    _build_pre_migration(db)

    _alembic_upgrade_head(db)

    assert set(COLS) <= set(_cols(str(db)))
    assert _version(str(db)) == REV


def test_running_the_upgrade_twice_is_a_no_op(tmp_path):
    """The sibling revision's runbook ends 'IF IT FAILS, JUST RE-RUN IT'. Hold that line.

    Re-running through the CLI is a no-op for the boring reason (alembic_version is
    already at head), so the second pass here re-executes ``upgrade()`` itself --
    which is what an operator who stamped wrong, or a half-finished run, actually
    hits. ``ADD COLUMN`` of a column that exists is a hard error on SQLite.
    """
    db = tmp_path / "twice.sqlite"
    engine = _build_pre_migration(db)
    _alembic_upgrade_head(db)
    before = _cols(str(db))

    _upgrade(engine)          # the revision's own body, a second time
    _alembic_upgrade_head(db)

    assert _cols(str(db)) == before
    for column in COLS:
        assert before.count(column) == 1


def test_a_create_all_database_that_already_has_them_upgrades_cleanly(tmp_path):
    """``init_db()``'s create_all can build the table before Alembic ever runs.

    It only stamps head when the schema was ABSENT beforehand, so on an existing
    database create_all silently materialises the new columns and leaves
    ``alembic_version`` where it was. An unguarded ``op.add_column`` then dies with
    "duplicate column name: asset_class" and the revision can never be applied.
    """
    db = tmp_path / "createall.sqlite"
    _build_via_create_all(db)
    assert set(COLS) <= set(_cols(str(db)))

    _alembic_upgrade_head(db)

    assert set(COLS) <= set(_cols(str(db)))
    assert [c for c in _cols(str(db)) if c in COLS] == list(COLS)   # no duplicates
    assert _version(str(db)) == REV


# --- the shape of what was added --------------------------------------------

def test_asset_class_is_not_null_and_defaults_to_the_stored_enum_NAME(tmp_path):
    """SQLModel str-enums store the NAME ('EQUITY'), not the value ('equity').

    A default written as the value produces rows no query matches -- and worse, a
    row SQLAlchemy refuses to load at all, because 'equity' is not among the names
    it maps back to the enum.
    """
    db = tmp_path / "default.sqlite"
    _build_pre_migration(db)
    _alembic_upgrade_head(db)

    info = _column_info(str(db))
    assert info["asset_class"][3] == 1, "asset_class must be NOT NULL"
    assert "EQUITY" in str(info["asset_class"][4])
    assert "equity" not in str(info["asset_class"][4]), (
        "the lowercase VALUE would never match a row the ORM writes")
    assert info["option_strategy"][3] == 0 and info["expiry"][3] == 0


def test_a_row_that_predates_the_column_reads_back_through_the_ORM(tmp_path):
    """The server default has to produce a value the ORM can actually load.

    This is the enum-name trap end to end: write a row while the column does not
    exist, migrate, then read it with SQLAlchemy. A default of 'equity' leaves the
    raw column holding a string that is not one of the enum NAMES, and every read
    of that row raises LookupError instead of returning a position.
    """
    db = tmp_path / "legacy_row.sqlite"
    engine = _build_pre_migration(db)
    with engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO "transaction" (id, symbol, quantity, side, status, '
            " tp_manual_override, sl_manual_override, created_at) "
            "VALUES (1, 'ACN', 10, 'BUY', 'OPENED', 0, 0, '2026-08-01 00:00:00')"))

    _alembic_upgrade_head(db)

    con = sqlite3.connect(str(db))
    try:
        raw = con.execute('SELECT asset_class FROM "transaction" WHERE id = 1').fetchone()[0]
    finally:
        con.close()
    assert raw == "EQUITY"

    with Session(create_engine(f"sqlite:///{db}")) as session:
        rows = session.exec(
            select(Transaction).where(Transaction.asset_class == AssetClass.EQUITY)).all()
        assert [r.id for r in rows] == [1]
        assert rows[0].asset_class is AssetClass.EQUITY
        assert rows[0].option_strategy is None and rows[0].expiry is None


def test_a_historical_option_transaction_also_reads_as_EQUITY(tmp_path):
    """FORWARD-ONLY, pinned rather than discovered later.

    Nothing is back-filled: 23 of the 82 historical option orders have unrecoverable
    contracts, so ``option_strategy`` and ``expiry`` cannot be reconstructed. The
    price is that ``asset_class`` on a pre-existing option row says EQUITY -- on the
    live database that is 20 transactions, 13 of them still OPEN, every one of which
    carries a TradingOrder with asset_class 'OPTION' and multiplier 100.

    They are not wrong by accident and this test says so out loud. Recovering them
    is a separate, cheap revision (``UPDATE "transaction" SET asset_class='OPTION'
    WHERE EXISTS (SELECT 1 FROM tradingorder o WHERE o.transaction_id = id AND
    o.asset_class = 'OPTION')`` -- a lookup of a recorded fact, not a guess), and
    this assertion is what will fail, loudly and in the right place, when that
    revision lands.
    """
    db = tmp_path / "historical_option.sqlite"
    engine = _build_pre_migration(db)
    with engine.begin() as connection:
        connection.execute(text(
            'INSERT INTO "transaction" (id, symbol, quantity, side, status, multiplier, '
            " tp_manual_override, sl_manual_override, created_at) "
            "VALUES (1, 'ACN', 1, 'BUY', 'OPENED', 100, 0, 0, '2026-08-01 00:00:00')"))

    _alembic_upgrade_head(db)

    con = sqlite3.connect(str(db))
    try:
        row = con.execute('SELECT asset_class, multiplier, option_strategy, expiry '
                          'FROM "transaction" WHERE id = 1').fetchone()
    finally:
        con.close()
    assert row == ("EQUITY", 100, None, None)


def test_the_intent_columns_are_indexed_under_the_names_create_all_uses(tmp_path):
    """One name per index across both paths, or a create_all database and a migrated
    one disagree on what already exists and the next revision's guard misfires."""
    migrated = tmp_path / "indexed.sqlite"
    _build_pre_migration(migrated)
    _alembic_upgrade_head(migrated)
    fresh = tmp_path / "indexed_fresh.sqlite"
    _build_via_create_all(fresh, stamp=None)

    def _index_names(path):
        con = sqlite3.connect(str(path))
        try:
            return {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='transaction'")
                if r[0]}
        finally:
            con.close()

    assert set(INDEXES) <= _index_names(migrated)
    assert _index_names(migrated) == _index_names(fresh)


def test_the_migrated_schema_is_what_create_all_would_have_built(tmp_path):
    """Alembic's OWN comparator, over the transaction table, must see zero differences.

    Names and nullability are the cheap half; this compares types, server defaults,
    indexes and uniqueness the way autogenerate does, so a column that exists in both
    places with the wrong type cannot slip through.
    """
    from alembic.autogenerate import compare_metadata

    db = tmp_path / "comparator.sqlite"
    engine = _build_pre_migration(db)
    _upgrade(engine)

    def _only_transaction(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name == "transaction"
        return True

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": _only_transaction, "compare_type": True})
        diffs = compare_metadata(context, SQLModel.metadata)

    assert diffs == [], f"migrated schema differs from create_all: {diffs}"


def test_the_migrated_columns_are_the_same_SET_create_all_builds_but_appended(tmp_path):
    """Same columns, different POSITION -- and the difference is not fixable here.

    ``ALTER TABLE ADD COLUMN`` appends; the model declares the three after
    ``multiplier``. The sibling allocation revision can assert exact order because it
    CREATEs its tables. This one cannot: no revision has ever created ``transaction``
    (create_all did), so there is no CREATE to place a column inside, and the live
    table is already out of declaration order for the same reason -- ``side``,
    ``close_reason``, the two override flags and ``multiplier`` all arrived by
    ALTER and all sit after ``created_at``. A batch rebuild would reorder OUR three
    and leave those five where they are, buying nothing for a table copy.

    So the invariant that is actually true, and that the comparator test above backs
    with types and nullability, is set equality.
    """
    migrated = tmp_path / "order_migrated.sqlite"
    _build_pre_migration(migrated)
    _alembic_upgrade_head(migrated)
    fresh = tmp_path / "order_fresh.sqlite"
    _build_via_create_all(fresh, stamp=None)

    assert set(_cols(str(migrated))) == set(_cols(str(fresh)))
    assert _cols(str(migrated))[-3:] == list(COLS), "the ALTER path appends"
    fresh_cols = _cols(str(fresh))
    start = fresh_cols.index("multiplier")
    assert fresh_cols[start + 1:start + 4] == list(COLS), "create_all follows the model"


# --- the guards themselves ---------------------------------------------------

@pytest.mark.parametrize("column", COLS)
def test_each_column_is_added_individually(tmp_path, column):
    """One guarded add per column, so a database missing only ONE of them converges.

    That is not hypothetical: it is what a half-finished run leaves behind, and
    alembic reports SQLite as non-transactional DDL, so nothing rolls back.
    """
    db = tmp_path / f"partial_{column}.sqlite"
    engine = _build_via_create_all(db, stamp=None)
    with engine.begin() as connection:
        for index_name in INDEXES:
            connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        connection.execute(text(f'ALTER TABLE "transaction" DROP COLUMN {column}'))
    assert column not in _cols(str(db))

    _upgrade(engine)

    assert column in _cols(str(db))
    assert set(COLS) <= set(_cols(str(db)))


def test_the_upgrade_refuses_a_database_with_no_transaction_table(tmp_path):
    """Silently skipping would leave the model and the schema disagreeing forever.

    Unlike the allocation revision, this one has no CREATE to fall back on: if
    ``transaction`` is missing, this is not a database this revision knows how to
    repair and guessing at one is how a schema quietly forks.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'no_table.sqlite'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))

    with pytest.raises(RuntimeError, match="transaction"):
        _upgrade(engine)


def test_indexing_refuses_when_the_column_it_indexes_is_missing(tmp_path):
    """``CREATE INDEX`` on a column the ADD above was supposed to make.

    If the column is not there, the add silently did nothing and the revision is
    about to leave a half-migrated table behind while alembic_version moves to head
    -- SQLite DDL is reported as non-transactional, so nothing rolls back. Say so,
    with the column named, instead of letting sqlite's "no such column" surface from
    two layers down.

    This is also the assertion that makes the fresh-inspector-per-call discipline
    observable: a shared Inspector memoises the column list from BEFORE the first
    ADD COLUMN, so it would answer "missing" for a column that was just added.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'no_column.sqlite'}")
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE "transaction" (id INTEGER PRIMARY KEY, symbol VARCHAR)'))

    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="asset_class"):
                module._create_index_if_absent("ix_transaction_asset_class",
                                               ["asset_class"])


def test_downgrade_removes_the_three_columns_and_tolerates_their_absence(tmp_path):
    """A downgrade over a half-removed schema must finish the job, not die on the gap."""
    db = tmp_path / "down.sqlite"
    engine = _build_via_create_all(db, stamp=None)

    _downgrade(engine)
    assert not set(COLS) & set(_cols(str(db)))

    _downgrade(engine)          # already gone: must not raise
    assert not set(COLS) & set(_cols(str(db)))


def test_the_revision_is_chained_onto_the_option_iv_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic history has branched: {heads}"
    assert heads[0] == REV
    revision = script.get_revision(REV)
    assert revision.down_revision == PARENT_REV
    assert script.get_revision(PARENT_REV) is not None
