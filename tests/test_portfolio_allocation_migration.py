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
