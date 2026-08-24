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


def _alembic_upgrade_head(db_path, target=REV):
    """`alembic upgrade <target>` in a subprocess, against a THROWAWAY file.

    THIS revision, not ``head``: revisions land after it (c4d7e2b18a93 was the
    first), and `upgrade head` would then run them too and leave
    ``alembic_version`` past the value these tests are about. Naming the target
    keeps each revision's own tests measuring that revision.
    """
    env = {**os.environ, "BA2_DB_FILE": str(db_path)}
    result = subprocess.run(
        [str(ROOT / "venv/bin/python"), "-m", "alembic", "upgrade", target],
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


def _insert_transaction(engine, tx_id, *, symbol="ACN", multiplier=None,
                        status="OPENED", asset_class=None):
    """A transaction row, by default in PRE-migration shape: no asset_class to fill.

    ``multiplier`` defaults to NULL on purpose -- ids 406 and 407 on the live
    database are real open SPY option positions with a NULL multiplier.

    ``asset_class`` must be passed on a create_all database, where the column is
    already there and is NOT NULL with no *server* default (SQLModel's default is
    client-side, so a raw INSERT that omits it fails). Pass the stored enum NAME.
    """
    columns = "id, symbol, quantity, side, status, multiplier, " \
              "tp_manual_override, sl_manual_override, created_at"
    values = ":id, :symbol, 1, 'BUY', :status, :multiplier, 0, 0, '2026-08-01 00:00:00'"
    params = {"id": tx_id, "symbol": symbol, "status": status, "multiplier": multiplier}
    if asset_class is not None:
        columns += ", asset_class"
        values += ", :asset_class"
        params["asset_class"] = asset_class
    with engine.begin() as connection:
        connection.execute(
            text(f'INSERT INTO "transaction" ({columns}) VALUES ({values})'), params)


def _insert_order(engine, order_id, *, transaction_id, asset_class, symbol="ACN"):
    """A child TradingOrder. ``asset_class`` is the stored enum NAME: 'OPTION'/'EQUITY'."""
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tradingorder (id, account_id, symbol, quantity, side, order_type, "
            "status, open_type, asset_class, transaction_id, created_at) "
            "VALUES (:id, 1, :symbol, 1, 'BUY', 'MARKET', 'FILLED', 'AUTOMATIC', "
            ":asset_class, :transaction_id, '2026-08-01 00:00:00')"),
            {"id": order_id, "symbol": symbol, "asset_class": asset_class,
             "transaction_id": transaction_id})


def _asset_class(db_path, tx_id):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute('SELECT asset_class FROM "transaction" WHERE id = ?',
                           (tx_id,)).fetchone()[0]
    finally:
        con.close()


def _intent_rows(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute('SELECT id, asset_class, option_strategy, expiry, multiplier '
                           'FROM "transaction" ORDER BY id').fetchall()
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


def test_a_historical_option_transaction_is_backfilled_to_OPTION(tmp_path):
    """The inverse of what this test asserted when the revision first landed.

    It used to be ``test_a_historical_option_transaction_also_reads_as_EQUITY`` and
    it pinned the forward-only decision: nothing was back-filled, so the 20
    transactions on the live database that carry an option TradingOrder (13 of them
    still OPEN) all read EQUITY. Its own docstring named the recovery UPDATE and said
    "this assertion is what will fail, loudly and in the right place, when that
    revision lands". It did, and this is that landing.

    The decision it was pinning was right for ``option_strategy`` and ``expiry`` --
    23 of the 82 historical option orders have unrecoverable contracts, so both stay
    NULL below and always will. It was wrong for ``asset_class``, which is not a
    guess: ``o.asset_class = 'OPTION'`` on the child order is a recorded fact, and
    Task 8's lifecycle pass filters on exactly this column, so leaving it EQUITY
    silently drops 13 live option positions out of management.

    The row is built the way the old docstring described the live ones -- an option
    TradingOrder underneath -- which is also the whole difference: the derivation is
    the child order, not ``multiplier``.
    """
    db = tmp_path / "historical_option.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, multiplier=100)
    _insert_order(engine, 1, transaction_id=1, asset_class="OPTION")

    _alembic_upgrade_head(db)

    con = sqlite3.connect(str(db))
    try:
        row = con.execute('SELECT asset_class, multiplier, option_strategy, expiry '
                          'FROM "transaction" WHERE id = 1').fetchone()
    finally:
        con.close()
    assert row == ("OPTION", 100, None, None), (
        "asset_class is a lookup and must be recovered; strategy and expiry are "
        "guesswork and must stay NULL")


# --- the asset_class back-fill ------------------------------------------------

def test_the_backfill_separates_option_transactions_from_equity_ones(tmp_path):
    """The base case, both directions at once: a child order decides, per row.

    An UPDATE with no WHERE, or one whose EXISTS is inverted, passes half of this.
    """
    db = tmp_path / "backfill.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="ACN", multiplier=100)
    _insert_order(engine, 1, transaction_id=1, asset_class="OPTION", symbol="ACN260821C00300000")
    _insert_transaction(engine, 2, symbol="AAPL")
    _insert_order(engine, 2, transaction_id=2, asset_class="EQUITY", symbol="AAPL")

    _alembic_upgrade_head(db)

    assert _asset_class(db, 1) == "OPTION"
    assert _asset_class(db, 2) == "EQUITY"


def test_a_NULL_multiplier_still_backfills(tmp_path):
    """Ids 406 and 407 on the live database, and why the derivation is not multiplier.

    Both are real SPY option transactions, both still OPEN, and both have
    ``multiplier`` NULL -- 18 of the 20 live option transactions have multiplier 100
    and these two do not. A multiplier-derived back-fill gets 18 of 20 and abandons
    exactly the rows nobody would think to check.
    """
    db = tmp_path / "null_multiplier.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 406, symbol="SPY", multiplier=None)
    _insert_order(engine, 1, transaction_id=406, asset_class="OPTION", symbol="SPY260821P00600000")
    _insert_transaction(engine, 407, symbol="SPY", multiplier=None)
    _insert_order(engine, 2, transaction_id=407, asset_class="OPTION", symbol="SPY260821C00600000")

    _alembic_upgrade_head(db)

    assert _asset_class(db, 406) == "OPTION"
    assert _asset_class(db, 407) == "OPTION"
    # and the multiplier is left exactly as it was -- this revision back-fills ONE column
    assert [row[4] for row in _intent_rows(db)] == [None, None]


def test_a_transaction_whose_children_are_all_equity_stays_EQUITY(tmp_path):
    """multiplier = 100 on an equity-only transaction must NOT make it an option.

    ``multiplier`` was only ever a coincidence of P&L arithmetic; a stock split
    adjustment or a hand-fixed row can carry 100 without a single option leg. The
    child order's ``asset_class`` is the recorded fact and it is the only input.
    """
    db = tmp_path / "equity_children.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="AAPL", multiplier=100)
    _insert_order(engine, 1, transaction_id=1, asset_class="EQUITY", symbol="AAPL")
    _insert_order(engine, 2, transaction_id=1, asset_class="EQUITY", symbol="AAPL")

    _alembic_upgrade_head(db)

    assert _asset_class(db, 1) == "EQUITY"


def test_a_transaction_with_no_orders_at_all_stays_EQUITY(tmp_path):
    """No child order is no evidence, and no evidence is not an option.

    EXISTS answers false and the server default stands. This is most of the 701
    other rows on the live database.
    """
    db = tmp_path / "orphan.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="AAPL", multiplier=100)

    _alembic_upgrade_head(db)

    assert _asset_class(db, 1) == "EQUITY"


def test_a_transaction_with_both_equity_and_option_children_is_OPTION(tmp_path):
    """ANY option leg wins. There is no such thing as a half-option position here.

    No live row is shaped like this yet, but an assigned wheel is once Task 5 lands:
    a short put is exercised, the equity fill that settles it is recorded under the
    same transaction, and the row now has one child of each kind. OPTION is the
    answer that costs least when wrong. ``asset_class`` is the filter for Task 8's
    lifecycle pass -- expiry, assignment, early-exercise -- so calling a mixed row
    EQUITY drops it out of that pass entirely and an option leg expires unmanaged,
    whereas calling it OPTION at worst runs an option check over a position whose
    option side is already closed, which finds nothing to do.
    """
    db = tmp_path / "mixed.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="ACN", multiplier=100)
    _insert_order(engine, 1, transaction_id=1, asset_class="EQUITY", symbol="ACN")
    _insert_order(engine, 2, transaction_id=1, asset_class="OPTION", symbol="ACN260821P00300000")

    _alembic_upgrade_head(db)

    assert _asset_class(db, 1) == "OPTION"


def test_the_backfill_writes_the_NAME_so_the_ORM_reads_an_enum_back(tmp_path):
    """'OPTION', not 'option'. The value would produce rows no query matches.

    SQLAlchemy persists a SQLModel str-enum by NAME, so a back-fill written as the
    enum's VALUE leaves a string it refuses to map back: the ORM raises LookupError
    on the read instead of returning the position, and every
    ``where(asset_class == AssetClass.OPTION)`` -- which is how Task 8 finds work --
    silently returns nothing.
    """
    db = tmp_path / "enum_name.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="ACN")
    _insert_order(engine, 1, transaction_id=1, asset_class="OPTION")
    _insert_transaction(engine, 2, symbol="AAPL")

    _alembic_upgrade_head(db)

    assert _asset_class(db, 1) == "OPTION"
    assert _asset_class(db, 1) != "option"

    with Session(create_engine(f"sqlite:///{db}")) as session:
        options = session.exec(
            select(Transaction).where(Transaction.asset_class == AssetClass.OPTION)).all()
        assert [row.id for row in options] == [1]
        assert options[0].asset_class is AssetClass.OPTION
        assert options[0].option_strategy is None and options[0].expiry is None
        equities = session.exec(
            select(Transaction).where(Transaction.asset_class == AssetClass.EQUITY)).all()
        assert [row.id for row in equities] == [2]


def test_running_the_backfill_a_second_time_changes_nothing(tmp_path):
    """"IF IT FAILS, JUST RE-RUN IT" now covers an UPDATE, not only ADD COLUMNs.

    The second pass must report ZERO rows, not "the same 20 again": an operator
    following the runbook reads that number to decide whether the first run worked.
    """
    db = tmp_path / "backfill_twice.sqlite"
    engine = _build_pre_migration(db)
    _insert_transaction(engine, 1, symbol="ACN")
    _insert_order(engine, 1, transaction_id=1, asset_class="OPTION")
    _insert_transaction(engine, 2, symbol="AAPL")
    _insert_order(engine, 2, transaction_id=2, asset_class="EQUITY")
    _alembic_upgrade_head(db)
    before = _intent_rows(db)
    assert before == [(1, "OPTION", None, None, None), (2, "EQUITY", None, None, None)]

    _upgrade(engine)              # the revision's own body, a second time
    assert _intent_rows(db) == before

    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            assert module._backfill_asset_class_from_child_orders() == 0


def test_the_backfill_only_promotes_and_never_demotes(tmp_path):
    """A row the APP already marked OPTION survives a re-run with equity-only children.

    The revision races init_db(): on a create_all database the columns are already
    there and already carry values the running app wrote. A back-fill that also set
    EQUITY where no option child exists would rewrite live intent -- a spread whose
    legs were closed and whose closing fills happen to be equity-shaped, say -- from
    a migration. Recovering unset rows and overruling set ones are different jobs and
    this one only does the first.
    """
    db = tmp_path / "no_demote.sqlite"
    engine = _build_via_create_all(db)
    _insert_transaction(engine, 1, symbol="ACN", asset_class="OPTION")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE \"transaction\" SET option_strategy = 'bull_call_spread' WHERE id = 1"))
    _insert_order(engine, 1, transaction_id=1, asset_class="EQUITY")

    _alembic_upgrade_head(db)

    assert _intent_rows(db) == [(1, "OPTION", "bull_call_spread", None, None)]


def test_the_backfill_refuses_a_database_with_no_tradingorder_table(tmp_path):
    """The evidence table is the whole derivation; missing, it must say so.

    Letting sqlite's "no such table: tradingorder" surface from two layers down would
    look like a broken revision rather than a database this revision cannot read. And
    skipping instead would be worse: the columns would be added, alembic_version would
    move to head, and 13 open option positions would sit there labelled EQUITY with
    nothing left to notice it.
    """
    db = tmp_path / "no_orders.sqlite"
    engine = _build_pre_migration(db)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE tradingorder"))

    with pytest.raises(RuntimeError, match="tradingorder"):
        _upgrade(engine)


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
    """One head, and this revision is on the path to it.

    It was the head when it shipped; later revisions chain onto it (c4d7e2b18a93
    first). What still has to hold is that the history has not BRANCHED -- two heads
    make `alembic upgrade head` refuse outright -- and that this revision's own
    parent is unchanged.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic history has branched: {heads}"
    ancestry = {r.revision for r in script.iterate_revisions(heads[0], "base")}
    assert REV in ancestry, f"{REV} is no longer on the path to {heads[0]}"
    revision = script.get_revision(REV)
    assert revision.down_revision == PARENT_REV
    assert script.get_revision(PARENT_REV) is not None
