"""The allocator-protection Alembic revision must build exactly what the in-tree models declare,
survive ``init_db()``/create_all having got there first, and leave one head."""
import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

from ba2_trade_platform.core import allocator_protection_models  # noqa: F401 -- registers the tables

ROOT = pathlib.Path(__file__).resolve().parents[1]
REVISION_FILE = ROOT / "alembic/versions/a7c3e91d5b24_add_allocator_protection_tables.py"
TABLES = ["allocator_protection", "allocator_protection_order"]


def _load():
    spec = importlib.util.spec_from_file_location("alloc_protection_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn_name):
    module = _load()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, fn_name)()


@pytest.fixture
def migrated(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.sqlite'}")
    _run(engine, "upgrade")
    return engine


def test_creates_both_tables(migrated):
    assert sorted(t for t in inspect(migrated).get_table_names() if t.startswith("allocator_")) == sorted(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_columns_and_indexes_match_the_model(migrated, table):
    inspector = inspect(migrated)
    assert {c["name"] for c in inspector.get_columns(table)} == \
        {c.name for c in SQLModel.metadata.tables[table].columns}
    assert {i["name"] for i in inspector.get_indexes(table)} == \
        {i.name for i in SQLModel.metadata.tables[table].indexes}


def test_the_migrated_schema_is_what_create_all_would_build(migrated):
    from alembic.autogenerate import compare_metadata

    def ours(obj, name, type_, reflected, compare_to):
        return (name or "").startswith("allocator_") if type_ == "table" else True

    with migrated.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": ours, "compare_type": True})
        assert compare_metadata(context, SQLModel.metadata) == []


def test_one_protection_per_account_and_symbol(migrated):
    unique = {tuple(u["column_names"]) for u in inspect(migrated).get_unique_constraints("allocator_protection")}
    assert ("account_id", "symbol") in unique


def test_upgrade_is_a_no_op_when_create_all_built_the_tables_first(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[t] for t in TABLES])
    _run(engine, "upgrade")            # must not raise "table already exists"
    assert sorted(t for t in inspect(engine).get_table_names() if t.startswith("allocator_")) == sorted(TABLES)


def test_upgrade_twice_is_harmless(migrated):
    before = {t: sorted(i["name"] for i in inspect(migrated).get_indexes(t)) for t in TABLES}
    _run(migrated, "upgrade")
    assert {t: sorted(i["name"] for i in inspect(migrated).get_indexes(t)) for t in TABLES} == before


def test_downgrade_drops_both_and_tolerates_a_gap(migrated):
    _run(migrated, "downgrade")
    assert [t for t in inspect(migrated).get_table_names() if t.startswith("allocator_")] == []
    _run(migrated, "downgrade")        # nothing left: still fine


def test_the_revision_chains_onto_the_previous_head_and_keeps_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["a7c3e91d5b24"]
    assert script.get_revision("a7c3e91d5b24").down_revision == "d9e3b72a10fc"


def test_main_registers_the_models_before_init_db():
    source = (ROOT / "main.py").read_text(encoding="utf8")
    assert source.index("allocator_protection_models") < source.index("    init_db()")


def test_alembic_env_registers_the_models():
    assert "allocator_protection_models" in (ROOT / "alembic/env.py").read_text(encoding="utf8")


def test_no_package_imports_the_in_tree_protection_modules():
    """GA-neutrality: nothing under packages/ may reach this live-only code."""
    offenders = []
    for path in (ROOT / "packages").rglob("*.py"):
        text = path.read_text(encoding="utf8", errors="ignore")
        if "allocator_protection" in text and "tests" not in path.parts:
            offenders.append(str(path))
    assert offenders == []
