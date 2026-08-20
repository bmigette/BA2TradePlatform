"""The instrument-uniqueness migration, run for real against a fixture database.

Why importlib instead of `alembic upgrade`: in this codebase the base schema is
created by SQLModel.metadata.create_all, not by an initial migration, so
`alembic upgrade` from an empty sqlite file fails long before reaching this
revision. We build the pre-migration `instrument` table by hand (the live schema,
verbatim) and execute the real revision module's upgrade() bound to a live Alembic
Operations context -- the exact DDL and data SQL a production migration runs.

Every test uses its own tmp_path database. The live production DB is never opened.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_PATH = os.path.join(
    REPO, "alembic", "versions",
    "f1a7c2e9b4d0_merge_duplicate_instruments_unique_name.py",
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
_ROWS = [
    (1, 'AAPL', None, [], ['ark26'], None),
    (2, 'AAPL', 'STOCK', ['Tech'], ['nasdaq30'], 'Apple Inc'),
    (3, 'msft', 'STOCK', [], ['ark26'], None),
    (4, 'NVDA', 'STOCK', [], ['semis'], 'Nvidia Corp'),
]


def _load_migration_module():
    assert os.path.exists(MIGRATION_PATH), f"missing migration file {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("instrument_unique_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_premerge_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'premerge.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        for row in _ROWS:
            conn.execute(_INSERT, {
                "id": row[0], "name": row[1], "instrument_type": row[2],
                "categories": json.dumps(row[3]), "labels": json.dumps(row[4]),
                "company_name": row[5],
            })
    return engine


def _run_upgrade(engine, module):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.upgrade()


def _rows(engine):
    with engine.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(text("SELECT id, name FROM instrument ORDER BY id"))]


def _indexes(engine):
    with engine.connect() as conn:
        return {r[1]: r[2] for r in conn.execute(text("PRAGMA index_list(instrument)"))}


@pytest.fixture(autouse=True)
def _no_ambient_dry_run(monkeypatch):
    """Every test starts with BA2_INSTRUMENT_MERGE_DRY_RUN unset.

    The flag is fail-safe: anything that is not explicitly false means dry run. So
    a value left in the developer's shell would turn every real-run test in this
    file into a silent no-op that still passed its "nothing was written" asserts.
    The dry-run tests opt back in explicitly.
    """
    monkeypatch.delenv("BA2_INSTRUMENT_MERGE_DRY_RUN", raising=False)


def test_alembic_runs_without_a_pythonpath_prefix(tmp_path):
    """`alembic current` must work with a bare interpreter, no PYTHONPATH prefix.

    The Phase 6 packages (ba2_common/ba2_providers/ba2_experts) are only on
    sys.path for pytest, via pytest.ini's `pythonpath`; this checkout's editable
    installs point at an absolute path that does not exist here. Until env.py put
    packages/* on sys.path itself, `alembic current` -- and therefore
    `python migrate.py upgrade`, and therefore this revision -- died with
    ModuleNotFoundError: No module named 'ba2_common'.

    BA2_DB_FILE aims alembic at a throwaway file so no real database is opened.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["BA2_DB_FILE"] = str(tmp_path / "alembic_probe.sqlite")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_alembic_heads_is_this_revision_alone_without_a_pythonpath_prefix(tmp_path):
    """`alembic heads` must still work bare, and report exactly one head: ours.

    `heads` (like `history` and `branches`, i.e. `migrate.py heads|history`) imports
    every revision module but does NOT run env.py, so the sys.path fix in env.py does
    not apply to it. A revision that imports ba2_common at module scope breaks all
    three commands; this revision therefore defers that import into upgrade().
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["BA2_DB_FILE"] = str(tmp_path / "alembic_probe.sqlite")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    # The id is deliberately NOT pinned: Task 8 chains f1c8a24b7e05 onto this
    # revision, which would break the assert without anything being wrong. What
    # matters here is that the command runs at all (no module-scope app import)
    # and that the chain has not accidentally forked.
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one head, got:\n{result.stdout}"
    assert '(head)' in heads[0], result.stdout


def test_migration_declares_the_verified_head_as_its_parent():
    module = _load_migration_module()
    assert module.revision == 'f1a7c2e9b4d0'
    assert module.down_revision == '0a3e0bd24598'


def test_upgrade_merges_duplicates_and_creates_the_unique_index(tmp_path):
    engine = _build_premerge_db(tmp_path)
    _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
    assert _indexes(engine).get('ix_instrument_name') == 1

    with engine.connect() as conn:
        merged = conn.execute(text(
            "SELECT instrument_type, company_name, labels FROM instrument WHERE id = 1"
        )).fetchone()
    assert merged[0] == 'STOCK'
    assert merged[1] == 'Apple Inc'
    assert json.loads(merged[2]) == ['ark26', 'nasdaq30']


def test_production_shape_collapses_124_groups_and_still_indexes(tmp_path):
    """The live table's exact shape: 2477 rows, 2353 distinct names, 124 dup pairs.

    Read-only queries on 2026-08-20 put production at 2477 instrument rows over 2353
    distinct names with every name already `upper(trim(name))`, i.e. 124 groups of
    exactly two rows and no renaming at all. The small fixture above proves the
    merge's semantics; this proves CREATE UNIQUE INDEX actually succeeds once those
    124 groups are gone -- if even one duplicate survived the merge, the index
    creation, not the merge, is what would blow up in production.
    """
    db = tmp_path / "prodshape.sqlite"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text(_CREATE))
        row_id = 0
        for i in range(2353):
            name = f"SYM{i:04d}"
            for _ in range(2 if i < 124 else 1):
                row_id += 1
                conn.execute(_INSERT, {
                    "id": row_id, "name": name, "instrument_type": "STOCK",
                    "categories": json.dumps([]), "labels": json.dumps([f"lab{row_id}"]),
                    "company_name": None,
                })
        assert row_id == 2477

    _run_upgrade(engine, _load_migration_module())

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM instrument")).scalar() == 2353
        assert conn.execute(text("SELECT count(DISTINCT name) FROM instrument")).scalar() == 2353
        assert conn.execute(text(
            "SELECT count(*) FROM instrument WHERE name <> upper(trim(name))"
        )).scalar() == 0
        # the merged pair keeps both rows' labels on the surviving lowest id
        assert json.loads(conn.execute(text(
            "SELECT labels FROM instrument WHERE name = 'SYM0000'"
        )).scalar()) == ['lab1', 'lab2']
    assert _indexes(engine).get('ix_instrument_name') == 1


def test_after_upgrade_the_database_rejects_a_duplicate_name(tmp_path):
    engine = _build_premerge_db(tmp_path)
    _run_upgrade(engine, _load_migration_module())

    conn = sqlite3.connect(str(tmp_path / 'premerge.sqlite'))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO instrument (name) VALUES ('AAPL')")
    conn.close()


def test_running_the_upgrade_twice_succeeds_and_changes_nothing(tmp_path):
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    _run_upgrade(engine, module)
    after_first = _rows(engine)
    _run_upgrade(engine, module)          # must not raise "index already exists"
    assert _rows(engine) == after_first
    assert _indexes(engine).get('ix_instrument_name') == 1


def test_dry_run_env_var_reports_and_aborts_without_touching_the_database(tmp_path, monkeypatch, capsys):
    engine = _build_premerge_db(tmp_path)
    before = _rows(engine)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", "1")

    with pytest.raises(RuntimeError, match="BA2_INSTRUMENT_MERGE_DRY_RUN"):
        _run_upgrade(engine, _load_migration_module())

    printed = capsys.readouterr().out
    assert "AAPL" in printed and "MSFT" in printed
    assert _rows(engine) == before
    assert 'ix_instrument_name' not in _indexes(engine)


@pytest.mark.parametrize("value", ['1', 'true', 'TRUE', 'yes', ' 1 ', 'on', 'ON', 'Y', 'enabled', '2'])
def test_any_value_that_is_not_explicitly_false_means_dry_run(tmp_path, monkeypatch, value):
    """A truthy-value WHITELIST here would irreversibly delete production rows.

    'on', 'Y', 'enabled' and '2' are all things an operator plausibly types when
    they mean "just show me". Under a whitelist every one of them falls through to
    the real merge -- the exact failure this flag exists to prevent -- so anything
    unrecognised must abort instead.
    """
    engine = _build_premerge_db(tmp_path)
    before = _rows(engine)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", value)

    with pytest.raises(RuntimeError, match="BA2_INSTRUMENT_MERGE_DRY_RUN"):
        _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == before
    assert 'ix_instrument_name' not in _indexes(engine)


@pytest.mark.parametrize("value", ['', '0', 'false', 'FALSE', 'no', ' no '])
def test_only_explicit_falsiness_arms_the_real_merge(tmp_path, monkeypatch, value):
    """The other direction: disarming still has to work, or the flag is a wall."""
    engine = _build_premerge_db(tmp_path)
    monkeypatch.setenv("BA2_INSTRUMENT_MERGE_DRY_RUN", value)

    _run_upgrade(engine, _load_migration_module())

    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
    assert _indexes(engine).get('ix_instrument_name') == 1


@pytest.mark.parametrize("ddl", [
    "CREATE INDEX ix_instrument_name ON instrument (name)",          # right column, not unique
    "CREATE UNIQUE INDEX ix_instrument_name ON instrument (id)",     # unique, wrong column
])
def test_upgrade_refuses_an_index_that_only_borrows_the_name(tmp_path, ddl):
    """An index merely NAMED ix_instrument_name must not satisfy the guard.

    Skipping on the name alone means the migration reports success and stamps the
    revision while uniqueness is not enforced at all -- every downstream task then
    builds on an invariant that silently does not hold. Loud failure only.
    """
    engine = _build_premerge_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(ddl))
    before = _rows(engine)

    with pytest.raises(RuntimeError, match="not UNIQUE"):
        _run_upgrade(engine, _load_migration_module())

    # and the merge went back with it: untouched, not half-migrated
    assert _rows(engine) == before


class _CreateIndexExplodes:
    """Real Operations, except create_index raises -- a mid-migration failure."""

    def __init__(self, ops):
        self._ops = ops

    def __getattr__(self, name):
        return getattr(self._ops, name)

    def create_index(self, *args, **kwargs):
        raise RuntimeError("simulated failure during CREATE UNIQUE INDEX")


def test_a_failure_at_index_creation_rolls_the_entire_merge_back(tmp_path):
    """The property the whole recovery story rests on: all-or-nothing.

    `merge_duplicate_instruments` deliberately does not open or commit its own
    transaction, so the merge and the CREATE UNIQUE INDEX live or die together.
    That is what makes "if it fails, just re-run it" true, and what makes
    hand-repairing rows the wrong move.
    """
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    before = _rows(engine)
    assert before == [(1, 'AAPL'), (2, 'AAPL'), (3, 'msft'), (4, 'NVDA')]

    with pytest.raises(RuntimeError, match="simulated failure"):
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn, opts={"as_batch": True})
            module.op = _CreateIndexExplodes(Operations(ctx))
            module.sa = sqlalchemy
            module.upgrade()

    assert _rows(engine) == before      # deleted row back, 'msft' still lower-case
    assert 'ix_instrument_name' not in _indexes(engine)


def test_downgrade_drops_the_index_but_keeps_the_merged_rows(tmp_path):
    engine = _build_premerge_db(tmp_path)
    module = _load_migration_module()
    _run_upgrade(engine, module)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn, opts={"as_batch": True})
        module.op = Operations(ctx)
        module.sa = sqlalchemy
        module.downgrade()

    assert 'ix_instrument_name' not in _indexes(engine)
    assert _rows(engine) == [(1, 'AAPL'), (3, 'MSFT'), (4, 'NVDA')]
