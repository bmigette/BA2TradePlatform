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
def test_migration_columns_match_the_model(migrated_engine, table_name):
    migrated = {c["name"] for c in inspect(migrated_engine).get_columns(table_name)}
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
