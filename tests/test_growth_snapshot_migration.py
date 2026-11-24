"""The growth-snapshot Alembic revision builds exactly what the in-tree models declare, survives ``init_db()`` /
create_all having got there first, and chains onto the previous head (one head)."""
import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

from ba2_trade_platform.core import growth_snapshot_models  # noqa: F401 -- registers the tables

ROOT = pathlib.Path(__file__).resolve().parents[1]
REVISION = "f4c2a8d61b39"
REVISION_FILE = ROOT / "alembic/versions/f4c2a8d61b39_add_growth_snapshot_tables.py"
TABLES = ["account_growth_snapshot", "symbol_dividend_history"]


def _run(engine, fn_name):
    spec = importlib.util.spec_from_file_location("growth_snapshot_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, fn_name)()


@pytest.fixture
def migrated(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'g.sqlite'}")
    _run(engine, "upgrade")
    yield engine
    engine.dispose()


def _ours(engine):
    return sorted(t for t in inspect(engine).get_table_names() if t in TABLES)


def test_creates_both_tables(migrated):
    assert _ours(migrated) == TABLES


@pytest.mark.parametrize("table", TABLES)
def test_columns_primary_keys_and_indexes_match_the_model(migrated, table):
    inspector = inspect(migrated)
    model = SQLModel.metadata.tables[table]
    assert {c["name"] for c in inspector.get_columns(table)} == {c.name for c in model.columns}
    assert inspector.get_pk_constraint(table)["constrained_columns"] == [c.name for c in model.primary_key.columns]
    assert {i["name"] for i in inspector.get_indexes(table)} == {i.name for i in model.indexes}


def test_the_migrated_schema_is_what_create_all_would_build(migrated):
    from alembic.autogenerate import compare_metadata

    def ours(obj, name, type_, reflected, compare_to):
        return name in TABLES if type_ == "table" else True

    with migrated.connect() as connection:
        context = MigrationContext.configure(connection, opts={"include_object": ours, "compare_type": True})
        assert compare_metadata(context, SQLModel.metadata) == []


def test_upgrade_is_a_no_op_when_create_all_built_the_tables_first(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[t] for t in TABLES])
    _run(engine, "upgrade")            # must not raise "table already exists"
    assert _ours(engine) == TABLES
    engine.dispose()


def test_upgrade_twice_and_downgrade_twice_are_harmless(migrated):
    _run(migrated, "upgrade")
    assert _ours(migrated) == TABLES
    _run(migrated, "downgrade")
    assert _ours(migrated) == []
    _run(migrated, "downgrade")        # nothing left: still fine


def test_the_revision_chains_onto_the_previous_head_and_keeps_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == "e3b7c5a91f20"
