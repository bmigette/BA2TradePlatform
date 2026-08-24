"""The allocation-tables Alembic revision must build exactly what the models declare."""
import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import SQLModel
from sqlalchemy import create_engine, inspect

REVISION_FILE = pathlib.Path(__file__).resolve().parents[1] / \
    "alembic/versions/f1c8a24b7e05_add_portfolio_allocation_tables.py"

ALLOCATION_TABLES = [
    "portfolio_allocation_config",
    "portfolio_allocation_label",
    "portfolio_allocation_symbol",
    "portfolio_income_event",
    "portfolio_allocation_run",
]


def _load_revision():
    spec = importlib.util.spec_from_file_location("pf_alloc_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_engine(tmp_path):
    """A scratch sqlite with ONLY this revision's upgrade() applied."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
    return engine


def _upgrade(engine):
    """Apply THIS revision's upgrade() to ``engine``, as alembic would."""
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


#: Revisions chained AFTER this one that also touch the allocation tables. The
#: "does alembic build what create_all builds" claims are about the CHAIN, not about
#: one revision: a column added later exists in ``SQLModel.metadata`` from the moment
#: it is declared, so comparing a partially-upgraded database against it would report
#: drift that is really just "you stopped early".
LATER_ALLOCATION_REVISIONS = (
    "c4d7e2b18a93_add_portfolio_allocation_label_color.py",
)


def _upgrade_through_the_chain(engine):
    """This revision plus every later one that touches these five tables."""
    _upgrade(engine)
    for filename in LATER_ALLOCATION_REVISIONS:
        path = REVISION_FILE.parent / filename
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()


def _create_all_allocation_tables(engine, only=None):
    """Build the allocation tables the way ``init_db()``'s create_all does.

    ``only`` restricts it to a subset, which is how the partial-pre-existence
    wreckage is reproduced. The parent ``accountdefinition`` is deliberately not
    created: SQLite accepts a foreign key to a table that does not exist yet, and
    the point here is the five tables and nothing else.
    """
    names = only or ALLOCATION_TABLES
    SQLModel.metadata.create_all(
        engine, tables=[SQLModel.metadata.tables[n] for n in names])


def test_migration_creates_all_five_allocation_tables(migrated_engine):
    tables = inspect(migrated_engine).get_table_names()
    assert sorted(t for t in tables if t.startswith("portfolio_")) == sorted(ALLOCATION_TABLES)


@pytest.mark.parametrize("table_name", ALLOCATION_TABLES)
def test_migration_columns_match_the_model(tmp_path, table_name):
    """The alembic CHAIN builds every column the model declares.

    Through the chain, not through this revision alone: ``portfolio_allocation_label``
    gained ``color`` in c4d7e2b18a93, which is in ``SQLModel.metadata`` from the
    moment it is declared.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'chained.sqlite'}")
    _upgrade_through_the_chain(engine)
    migrated = {c["name"] for c in inspect(engine).get_columns(table_name)}
    declared = {c.name for c in SQLModel.metadata.tables[table_name].columns}
    assert migrated == declared


@pytest.mark.parametrize("table_name", ALLOCATION_TABLES)
def test_migration_indexes_match_the_model(migrated_engine, table_name):
    migrated = {i["name"] for i in inspect(migrated_engine).get_indexes(table_name)}
    declared = {i.name for i in SQLModel.metadata.tables[table_name].indexes}
    assert migrated == declared


def test_the_migrated_schema_is_what_create_all_would_have_built(migrated_engine):
    """Alembic's OWN comparator must see zero differences on the five tables.

    The per-table column/index assertions above compare NAMES; this compares the
    whole thing the way autogenerate does -- types, nullability, uniqueness,
    added and removed columns -- so a column that exists in both places with the
    wrong type or nullability cannot slip through. Every table that is not ours
    is filtered out: the scratch DB holds only these five, so the rest of
    ``SQLModel.metadata`` would otherwise show up as "add_table".
    """
    from alembic.autogenerate import compare_metadata

    def _ours(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return (name or "").startswith("portfolio_")
        return True

    _upgrade_through_the_chain(migrated_engine)   # see LATER_ALLOCATION_REVISIONS
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": _ours, "compare_type": True})
        diffs = compare_metadata(context, SQLModel.metadata)

    assert diffs == [], f"migrated schema differs from create_all: {diffs}"


def test_migration_records_the_income_consumption_guard(migrated_engine):
    """``income_consumed_at`` is the replay guard, so it must exist and be NULLable.

    NULL is what "this run has never spent from the ledger" is spelled as, and a
    NOT NULL column with no server default would also break every raw-SQL insert
    that predates it.
    """
    columns = {c["name"]: c for c in
               inspect(migrated_engine).get_columns("portfolio_allocation_run")}
    assert columns["income_consumed_at"]["nullable"] is True
    assert columns["income_consumed_events"]["nullable"] is True


def test_migration_enforces_the_income_idempotency_key(migrated_engine):
    unique = {tuple(u["column_names"])
              for u in inspect(migrated_engine).get_unique_constraints("portfolio_income_event")}
    assert ("account_id", "external_id") in unique


def test_migration_allows_only_one_config_row_per_account(migrated_engine):
    index_names = {i["name"] for i in
                   inspect(migrated_engine).get_indexes("portfolio_allocation_config")}
    unique = {tuple(u["column_names"]) for u in
              inspect(migrated_engine).get_unique_constraints("portfolio_allocation_config")}
    unique_indexes = {tuple(i["column_names"]) for i in
                      inspect(migrated_engine).get_indexes("portfolio_allocation_config")
                      if i["unique"]}
    assert ("account_id",) in unique or ("account_id",) in unique_indexes, index_names


def test_migration_downgrade_drops_all_five_tables(migrated_engine):
    module = _load_revision()
    with migrated_engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
    remaining = inspect(migrated_engine).get_table_names()
    assert [t for t in remaining if t.startswith("portfolio_")] == []


# --- the revision has to survive init_db() having got there first ----------

def test_upgrade_is_a_no_op_when_create_all_already_built_every_table(tmp_path):
    """The state ONE app start on this branch produces.

    ``init_db()`` only stamps head when the schema was absent beforehand, so on
    an existing database create_all silently materialises all five tables
    outside alembic and leaves ``alembic_version`` where it was. An unguarded
    ``op.create_table`` then dies with "table portfolio_allocation_config
    already exists" and the revision can never be applied to the one database it
    was written for.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'create_all_first.sqlite'}")
    _create_all_allocation_tables(engine)

    _upgrade(engine)          # must not raise

    tables = inspect(engine).get_table_names()
    assert sorted(t for t in tables if t.startswith("portfolio_")) == sorted(ALLOCATION_TABLES)


def test_upgrade_finishes_from_a_partially_created_schema(tmp_path):
    """The wreckage the unguarded version left, and could never clear.

    ``portfolio_income_event`` is the FOURTH table the revision creates, so a
    failure on it happens with three already made. Alembic treats SQLite as
    non-transactional DDL, so those three stayed behind while ``alembic_version``
    did not move -- and the retry then failed one table EARLIER, on config,
    forever. Starting from exactly that shape must now converge.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.sqlite'}")
    _create_all_allocation_tables(engine, only=["portfolio_income_event"])

    _upgrade(engine)

    inspector = inspect(engine)
    assert sorted(t for t in inspector.get_table_names()
                  if t.startswith("portfolio_")) == sorted(ALLOCATION_TABLES)
    # The pre-existing table must be left ALONE, not dropped and rebuilt: on a
    # real database it holds the income ledger.
    for table_name in ALLOCATION_TABLES:
        declared = {i.name for i in SQLModel.metadata.tables[table_name].indexes}
        assert {i["name"] for i in inspector.get_indexes(table_name)} == declared


def test_upgrade_run_twice_changes_nothing_the_second_time(migrated_engine):
    """Re-running is the documented recovery, so it has to be free of side effects."""
    before = {
        t: sorted(i["name"] for i in inspect(migrated_engine).get_indexes(t))
        for t in ALLOCATION_TABLES}

    _upgrade(migrated_engine)

    after = {
        t: sorted(i["name"] for i in inspect(migrated_engine).get_indexes(t))
        for t in ALLOCATION_TABLES}
    assert after == before


def test_downgrade_tolerates_tables_that_are_already_gone(tmp_path):
    """A downgrade over half-removed tables must finish the job, not die on the gap."""
    engine = create_engine(f"sqlite:///{tmp_path / 'half_dropped.sqlite'}")
    _create_all_allocation_tables(
        engine, only=["portfolio_allocation_config", "portfolio_allocation_run"])

    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    assert [t for t in inspect(engine).get_table_names() if t.startswith("portfolio_")] == []


def test_the_revision_is_chained_onto_the_instrument_revision():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = pathlib.Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic history has branched: {heads}"
    revision = script.get_revision("f1c8a24b7e05")
    assert revision.down_revision == "f1a7c2e9b4d0"
    assert script.get_revision(revision.down_revision) is not None


# --- the submitted_* -> filled_* rename, on a developer database ------------

_OLD_RUN_TABLE_SQL = """
CREATE TABLE portfolio_allocation_run (
    id INTEGER NOT NULL PRIMARY KEY,
    account_id INTEGER NOT NULL,
    mode VARCHAR NOT NULL,
    scope_label VARCHAR,
    base_notional FLOAT NOT NULL,
    available_buying_power FLOAT NOT NULL,
    allow_fractional BOOLEAN NOT NULL,
    plan_json JSON,
    submitted_buy_value FLOAT NOT NULL,
    submitted_sell_value FLOAT NOT NULL,
    order_ids JSON,
    income_consumed_at DATETIME,
    income_consumed_events JSON,
    created_at DATETIME NOT NULL
)
"""


def _old_shaped_run_table(engine):
    """The run table as an OLDER checkout's create_all built it: submitted_*."""
    from sqlalchemy import text
    with engine.begin() as connection:
        connection.execute(text(_OLD_RUN_TABLE_SQL))
        connection.execute(text(
            "INSERT INTO portfolio_allocation_run "
            "(id, account_id, mode, base_notional, available_buying_power, "
            " allow_fractional, submitted_buy_value, submitted_sell_value, created_at) "
            "VALUES (1, 7, 'REBALANCE', 0.0, 0.0, 0, 1600.0, 400.0, '2026-08-01 00:00:00')"))


def test_upgrade_renames_the_money_columns_a_stale_create_all_left_behind(tmp_path):
    """A developer DB built before the rename has submitted_*; the CREATE guard
    skips the table entirely, so without _rename_column_if_present the model and
    the schema disagree forever and every finalise dies on a missing column."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stale_names.sqlite'}")
    _old_shaped_run_table(engine)

    _upgrade(engine)

    names = {c["name"] for c in inspect(engine).get_columns("portfolio_allocation_run")}
    assert "filled_buy_value" in names and "filled_sell_value" in names
    assert "submitted_buy_value" not in names and "submitted_sell_value" not in names


def test_the_rename_carries_the_money_across_rather_than_dropping_it(tmp_path):
    """batch_alter_table copies the table; a rename that lost the values would
    zero every past run's totals and so mis-state what the ledger already spent."""
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'stale_values.sqlite'}")
    _old_shaped_run_table(engine)

    _upgrade(engine)

    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT filled_buy_value, filled_sell_value FROM portfolio_allocation_run "
            "WHERE id = 1")).one()
    assert (row[0], row[1]) == (1600.0, 400.0)


def test_the_rename_is_skipped_when_the_column_is_already_correct(migrated_engine):
    """Idempotence, the same discipline the create guards follow: re-running this
    revision over its own output must not raise."""
    _upgrade(migrated_engine)

    names = {c["name"] for c in inspect(migrated_engine).get_columns("portfolio_allocation_run")}
    assert "filled_buy_value" in names


def test_the_rename_refuses_to_guess_at_a_schema_it_does_not_recognise(tmp_path):
    """Neither spelling present means this is not the table we think it is.
    Renaming blind, or silently continuing, would hide a real schema drift."""
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'alien.sqlite'}")
    with engine.begin() as connection:
        # The indexed columns are present, so the revision reaches the rename
        # rather than dying earlier on CREATE INDEX; the money columns are not.
        connection.execute(text(
            "CREATE TABLE portfolio_allocation_run (id INTEGER PRIMARY KEY, "
            "account_id INTEGER, mode VARCHAR, created_at DATETIME)"))

    with pytest.raises(RuntimeError, match="refusing to guess"):
        _upgrade(engine)


# --- W2: previous_target_pct / previous_weight_pct, amended in place --------
#
# Free only while this revision is UNAPPLIED. Verified read-only on the live DB
# on 2026-08-23: alembic_version = d5e1b9a3c842, zero portfolio_% tables, and
# f1c8a24b7e05 is the only head. A developer DB that init_db()'s create_all
# already built is the case _add_column_if_absent exists for.

_PREVIOUS_COLUMNS = [
    ("portfolio_allocation_label", "previous_target_pct"),
    ("portfolio_allocation_symbol", "previous_weight_pct"),
]


@pytest.mark.parametrize("table_name,column", _PREVIOUS_COLUMNS)
def test_the_previous_target_columns_exist_and_are_nullable(migrated_engine, table_name,
                                                            column):
    """NULLABLE, not 0.0. NULL is how "there is no last" is spelled, and it is what
    makes the Load-last button's disabled state a fact rather than a guess: a stored
    0.0 would mean "the last run allocated nothing to this", which is a real and
    different answer."""
    columns = {c["name"]: c for c in inspect(migrated_engine).get_columns(table_name)}
    assert column in columns
    assert columns[column]["nullable"] is True


@pytest.mark.parametrize("table_name,column", _PREVIOUS_COLUMNS)
def test_a_create_all_database_gains_the_previous_columns_on_upgrade(tmp_path, table_name,
                                                                     column):
    """The developer case. ``_create_table_if_absent`` SKIPS a table create_all has
    already built, so a column added to the CREATE alone would never reach it and
    the model and the schema would disagree forever."""
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'older_create_all.sqlite'}")
    _create_all_allocation_tables(engine)
    # Reproduce the OLD shape by dropping the column create_all has just made.
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column}"))
    assert column not in {c["name"] for c in inspect(engine).get_columns(table_name)}

    _upgrade(engine)

    assert column in {c["name"] for c in inspect(engine).get_columns(table_name)}


@pytest.mark.parametrize("table_name,column", _PREVIOUS_COLUMNS)
def test_adding_the_previous_columns_is_idempotent(migrated_engine, table_name, column):
    """Re-running is the documented recovery, and ``op.add_column`` of a column that
    is already there is a hard error on SQLite."""
    _upgrade(migrated_engine)

    names = [c["name"] for c in inspect(migrated_engine).get_columns(table_name)]
    assert names.count(column) == 1


@pytest.mark.parametrize("table_name", ALLOCATION_TABLES)
def test_the_migrated_column_ORDER_is_the_one_create_all_would_have_built(tmp_path,
                                                                          table_name):
    """The two paths have to produce the SAME table, not merely the same set of names.

    This revision exists because ``init_db()``'s create_all and Alembic both build
    these tables, on different databases, and must agree -- and column order is part
    of that: it is what ``SELECT *`` and a positional ``INSERT INTO t VALUES (...)``
    see, both of which this suite uses.

    It is also the only thing that separates a column DECLARED in
    ``op.create_table`` from one that arrives through the trailing
    ``_add_column_if_absent`` repair, because a repair that fires appends to the END
    of the table. Delete the column from the CREATE and every name-and-nullability
    assertion still passes -- the repair silently covers for it -- while an alembic
    database and a create_all database quietly stop being the same schema.
    """
    migrated = create_engine(f"sqlite:///{tmp_path / 'ordered_migration.sqlite'}")
    _upgrade(migrated)
    from_create_all = create_engine(f"sqlite:///{tmp_path / 'ordered_create_all.sqlite'}")
    _create_all_allocation_tables(from_create_all)

    migrated_names = [c["name"] for c in inspect(migrated).get_columns(table_name)]
    # Only THIS revision is applied to ``migrated``, so a column a LATER revision
    # adds is legitimately absent -- and it could not match the order anyway, since
    # ``ALTER TABLE ADD COLUMN`` appends while create_all puts it where the model
    # declares it. The claim under test is that the columns this revision creates
    # are in the same ORDER, which is what catches one silently moved to the end by
    # a stray ``_add_column_if_absent`` repair.
    create_all_names = [c["name"] for c in inspect(from_create_all).get_columns(table_name)
                        if c["name"] in set(migrated_names)]
    assert migrated_names == create_all_names


def test_adding_a_previous_column_preserves_the_rows_already_there(tmp_path):
    """``op.add_column`` is not a table rebuild, but pin it anyway: these two rows
    ARE the user's allocation configuration, and a migration that dropped them
    would silently unmanage the account."""
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'with_rows.sqlite'}")
    _create_all_allocation_tables(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolio_allocation_label "
                                "DROP COLUMN previous_target_pct"))
        connection.execute(text(
            "INSERT INTO portfolio_allocation_label "
            "(id, account_id, label, target_pct, sort_order, created_at) "
            "VALUES (1, 7, 'ARK26', 60.0, 0, '2026-08-01 00:00:00')"))

    _upgrade(engine)

    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT label, target_pct, previous_target_pct "
            "FROM portfolio_allocation_label WHERE id = 1")).one()
    assert (row[0], row[1]) == ('ARK26', 60.0)
    assert row[2] is None       # no last: the column is new, not back-filled


# --- W8: unallocated_pct, the stored cash reserve ---------------------------
#
# Amended in place on the same grounds as the previous_* columns above, and it
# lands in TWO places for the same reason they do: in ``op.create_table`` for a
# database this revision builds, and in a trailing ``_add_column_if_absent`` for
# the developer database ``init_db()``'s create_all got to first. Either one alone
# leaves half the world without the column, and neither is redundant -- each is the
# only path on its own kind of database.
#
# Unlike its two siblings this one is NOT NULL with a server default, and the
# difference is a statement about history rather than about SQL: nobody who
# predates the column ever reserved anything, so 0 is the true value and there is
# nothing to guess. (SQLite accepts ADD COLUMN NOT NULL only BECAUSE of that
# default; without one it refuses outright on a non-empty table.)

def test_the_unallocated_reserve_column_is_not_null_with_a_zero_default(migrated_engine):
    """NOT NULL and DEFAULT 0, which is the opposite choice from the previous_*
    columns beside it and has to be pinned as such.

    NULL there would mean "this account has not said", and there is no such state:
    an account that never touched the box reserves nothing, which is a real answer
    and the one every pre-existing row means. Nullable, the reserve would arrive at
    ``compute_allocation`` as None on every legacy row and reach the arithmetic as a
    guess -- and ``float(None or 0.0)`` would quietly make that guess look like a
    decision.
    """
    columns = {c["name"]: c for c in
               inspect(migrated_engine).get_columns("portfolio_allocation_config")}
    assert "unallocated_pct" in columns
    assert columns["unallocated_pct"]["nullable"] is False
    assert str(columns["unallocated_pct"]["default"]).strip("'\"") == "0"


def test_a_create_all_database_gains_the_unallocated_column_on_upgrade(tmp_path):
    """The developer case. ``_create_table_if_absent`` SKIPS a table create_all has
    already built, so a column added to the CREATE alone would never reach it: the
    model would declare ``unallocated_pct`` and the schema would not have it, and
    every read of the allocation config would die on a missing column."""
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'older_create_all_reserve.sqlite'}")
    _create_all_allocation_tables(engine)
    # Reproduce the OLD shape by dropping the column create_all has just made.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolio_allocation_config "
                                "DROP COLUMN unallocated_pct"))
    assert "unallocated_pct" not in {
        c["name"] for c in inspect(engine).get_columns("portfolio_allocation_config")}

    _upgrade(engine)

    assert "unallocated_pct" in {
        c["name"] for c in inspect(engine).get_columns("portfolio_allocation_config")}


def test_the_repaired_unallocated_column_is_also_not_null_with_a_zero_default(tmp_path):
    """The repair has to produce the same column the CREATE would have.

    Two paths, one schema: a database that reached the column through
    ``_add_column_if_absent`` must not end up with a nullable one, or "has this
    account set a reserve?" answers differently depending on which build first
    touched the database.
    """
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'repaired_reserve.sqlite'}")
    _create_all_allocation_tables(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolio_allocation_config "
                                "DROP COLUMN unallocated_pct"))

    _upgrade(engine)

    columns = {c["name"]: c for c in
               inspect(engine).get_columns("portfolio_allocation_config")}
    assert columns["unallocated_pct"]["nullable"] is False
    assert str(columns["unallocated_pct"]["default"]).strip("'\"") == "0"


def test_an_account_that_predates_the_reserve_reads_back_as_reserving_nothing(tmp_path):
    """The back-fill, on a row that was configured before the column existed.

    "No reserve" is what every such row MEANT, so 0.0 is history rather than a
    default standing in for a missing answer -- and it is the value the whole
    feature is safe at. A NULL here would reach ``compute_allocation`` on the next
    dry run for an account nobody has touched since.
    """
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_config_row.sqlite'}")
    _create_all_allocation_tables(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE portfolio_allocation_config "
                                "DROP COLUMN unallocated_pct"))
        connection.execute(text(
            "INSERT INTO portfolio_allocation_config "
            "(id, account_id, valuation_mode, allow_fractional, updated_at) "
            "VALUES (1, 7, 'market', 1, '2026-08-01 00:00:00')"))

    _upgrade(engine)

    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT account_id, valuation_mode, unallocated_pct "
            "FROM portfolio_allocation_config WHERE id = 1")).one()
    assert (row[0], row[1]) == (7, 'market')
    assert row[2] == 0.0        # back-filled from the server default, never NULL


def test_adding_the_unallocated_column_is_idempotent(migrated_engine):
    """Re-running is the documented recovery, and ``op.add_column`` of a column that
    is already there is a hard error on SQLite."""
    _upgrade(migrated_engine)

    names = [c["name"] for c in
             inspect(migrated_engine).get_columns("portfolio_allocation_config")]
    assert names.count("unallocated_pct") == 1
