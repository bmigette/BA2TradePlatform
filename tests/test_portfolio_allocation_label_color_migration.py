"""The label-colour Alembic revision: chained, idempotent, and NOT a back-fill.

Three properties, and each of them has a named precedent in this repo:

* It chains off ``b2f4c81d6a35`` and leaves alembic with ONE head. A second head
  is not a cosmetic problem -- ``alembic upgrade head`` refuses outright.
* It is IDEMPOTENT, because it races ``init_db()``: starting the app once on this
  branch runs ``SQLModel.metadata.create_all()``, which materialises ``color``
  outside alembic on any database that is not brand new, and an unguarded
  ``op.add_column`` then dies with "duplicate column name" forever after. Exactly
  the trap ``b2f4c81d6a35`` documents at length.
* It does NOT back-fill. NULL means "the user has not chosen a colour", which is a
  different fact from a stored default -- and a default would make every label that
  predates the column claim a colour nobody picked.

No real database is touched: every engine here is a scratch file under ``tmp_path``.
"""
import importlib.util
import pathlib

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REVISION_FILE = REPO_ROOT / \
    "alembic/versions/c4d7e2b18a93_add_portfolio_allocation_label_color.py"

TABLE = "portfolio_allocation_label"


def _load_revision():
    spec = importlib.util.spec_from_file_location("pf_label_color_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, action: str) -> None:
    """Apply this revision's ``upgrade``/``downgrade`` to ``engine``, as alembic would."""
    module = _load_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, action)()


@pytest.fixture
def legacy_engine(tmp_path):
    """A scratch sqlite holding the label table WITHOUT ``color``, plus two rows.

    The column is dropped after ``create_all`` rather than the table hand-written,
    so everything else about it -- indexes, the unique constraint, the foreign key --
    is exactly what the model declares and only the one difference is under test.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'label_color.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[TABLE]])
    with engine.begin() as connection:
        connection.execute(text(f'ALTER TABLE {TABLE} DROP COLUMN color'))
        connection.execute(text(
            f"INSERT INTO {TABLE} (account_id, label, target_pct, sort_order, created_at) "
            "VALUES (1, 'ARK26', 40.0, 0, '2026-01-05 00:00:00')"))
        connection.execute(text(
            f"INSERT INTO {TABLE} (account_id, label, target_pct, sort_order, created_at) "
            "VALUES (1, 'HighRisk', 60.0, 1, '2026-01-05 00:00:00')"))
    return engine


def _columns(engine):
    return {c["name"]: c for c in inspect(engine).get_columns(TABLE)}


# ---------------------------------------------------------------------------
# Where it sits in the graph
# ---------------------------------------------------------------------------

def test_the_revision_chains_off_the_current_head():
    module = _load_revision()
    assert module.revision == "c4d7e2b18a93"
    assert module.down_revision == "b2f4c81d6a35"


def test_alembic_still_has_exactly_one_head():
    """Two heads is not cosmetic: ``alembic upgrade head`` refuses to pick one."""
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["c4d7e2b18a93"]


# ---------------------------------------------------------------------------
# What it builds
# ---------------------------------------------------------------------------

def test_upgrade_adds_the_colour_column(legacy_engine):
    assert "color" not in _columns(legacy_engine)
    _run(legacy_engine, "upgrade")
    assert "color" in _columns(legacy_engine)


def test_the_added_column_is_nullable(legacy_engine):
    _run(legacy_engine, "upgrade")
    assert _columns(legacy_engine)["color"]["nullable"] is True


def test_the_added_column_carries_no_server_default(legacy_engine):
    """A server default is a back-fill wearing a schema's clothes: every row would
    read as having chosen that colour."""
    _run(legacy_engine, "upgrade")
    assert _columns(legacy_engine)["color"]["default"] is None


def test_the_migrated_table_is_what_create_all_would_have_built(legacy_engine):
    """Alembic's OWN comparator, types and nullability included -- so the app's
    create_all path and this revision cannot drift apart."""
    from alembic.autogenerate import compare_metadata

    _run(legacy_engine, "upgrade")

    def _ours(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name == TABLE
        return True

    with legacy_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": _ours, "compare_type": True})
        diffs = compare_metadata(context, SQLModel.metadata)
    assert diffs == [], f"migrated schema differs from create_all: {diffs}"


# ---------------------------------------------------------------------------
# It does NOT back-fill
# ---------------------------------------------------------------------------

def test_every_pre_existing_label_keeps_a_NULL_colour(legacy_engine):
    """NULL is "no colour chosen". A stored default would be a different fact, and
    it would be one the user never asserted."""
    _run(legacy_engine, "upgrade")
    with legacy_engine.connect() as connection:
        rows = connection.execute(text(f"SELECT label, color FROM {TABLE} ORDER BY label")).all()
    assert rows == [("ARK26", None), ("HighRisk", None)]


def test_the_upgrade_writes_no_rows_at_all(legacy_engine):
    """Not just "the colours are NULL": nothing else about the rows moves either."""
    with legacy_engine.connect() as connection:
        before = connection.execute(
            text(f"SELECT account_id, label, target_pct, sort_order, comment "
                 f"FROM {TABLE} ORDER BY label")).all()
    _run(legacy_engine, "upgrade")
    with legacy_engine.connect() as connection:
        after = connection.execute(
            text(f"SELECT account_id, label, target_pct, sort_order, comment "
                 f"FROM {TABLE} ORDER BY label")).all()
    assert before == after


def test_a_colour_set_before_the_revision_runs_is_left_alone(legacy_engine):
    """The create_all race: the running app may already have written colours through
    the ORM. The revision must not overwrite them with anything, default included."""
    _run(legacy_engine, "upgrade")
    with legacy_engine.begin() as connection:
        connection.execute(text(f"UPDATE {TABLE} SET color = '#56B4E9' WHERE label = 'ARK26'"))
    _run(legacy_engine, "upgrade")
    with legacy_engine.connect() as connection:
        rows = dict(connection.execute(text(f"SELECT label, color FROM {TABLE}")).all())
    assert rows == {"ARK26": "#56B4E9", "HighRisk": None}


# ---------------------------------------------------------------------------
# Idempotence -- it RACES init_db()
# ---------------------------------------------------------------------------

def test_running_the_upgrade_twice_is_not_an_error(legacy_engine):
    _run(legacy_engine, "upgrade")
    _run(legacy_engine, "upgrade")
    assert "color" in _columns(legacy_engine)


def test_the_upgrade_skips_a_column_create_all_already_built(tmp_path):
    """``init_db()`` materialises the column outside alembic on any database that is
    not brand new. Unguarded, ``op.add_column`` dies with "duplicate column name"
    and the revision can never be applied at all."""
    engine = create_engine(f"sqlite:///{tmp_path / 'create_all.sqlite'}")
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[TABLE]])
    assert "color" in _columns(engine)
    _run(engine, "upgrade")
    assert "color" in _columns(engine)


def test_the_upgrade_refuses_rather_than_skipping_when_the_table_is_missing(tmp_path):
    """A silent skip would move ``alembic_version`` to head with the model declaring
    a column the schema does not have, and every read would die on "no such column"."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    with pytest.raises(RuntimeError, match=TABLE):
        _run(engine, "upgrade")


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------

def test_downgrade_drops_the_column(legacy_engine):
    _run(legacy_engine, "upgrade")
    _run(legacy_engine, "downgrade")
    assert "color" not in _columns(legacy_engine)


def test_downgrade_is_guarded_the_same_way_the_upgrade_is(legacy_engine):
    _run(legacy_engine, "upgrade")
    _run(legacy_engine, "downgrade")
    _run(legacy_engine, "downgrade")
    assert "color" not in _columns(legacy_engine)


def test_downgrade_on_a_missing_table_skips_instead_of_exploding(tmp_path):
    """The asymmetry with the upgrade is deliberate: going FORWARD onto a schema that
    has no table is a state nobody can repair silently, while going BACK to a state
    that is already reached is simply done."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    _run(engine, "downgrade")
