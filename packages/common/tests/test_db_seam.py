"""DB-seam tests: configure_db points ba2_common at a throwaway sqlite, init_db
creates the schema lazily, and AppSetting survives a round-trip through the
db helpers. AppSetting field names (key, value_str) reconciled against
ba2_common/core/models.py.

Also covers the genesis stamp: when init_db() is what creates the schema, that
schema IS the alembic head schema, so alembic_version must say so -- otherwise
`migrate.py upgrade` replays the whole chain from base against a full schema and
dies. Every other starting state must be left strictly alone.
"""
import sqlite3

import pytest

from ._leakgate import MARK, probe_verdict


@pytest.fixture
def db_module():
    """The db module, with the global DB target and the alembic-head memo restored
    afterwards so these tests cannot leak into the rest of the session."""
    from ba2_common.core import db
    previous_file, previous_memo = db._db_file, db._alembic_head_script
    yield db
    db._alembic_head_script = previous_memo
    db.configure_db(previous_file)


def _version_rows(path):
    """The alembic_version contents of a sqlite file: a list, or None if no table."""
    connection = sqlite3.connect(str(path))
    try:
        if not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchall():
            return None
        return sorted(r[0] for r in connection.execute("SELECT version_num FROM alembic_version"))
    finally:
        connection.close()


def _repo_head():
    """The migration chain's head, read the same way alembic reads it."""
    import pathlib
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = pathlib.Path(__file__).resolve().parents[3]
    heads = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"alembic history has branched: {heads}"
    return heads[0]


def test_configure_db_isolates_to_temp(tmp_path):
    from ba2_common.core import db
    target = tmp_path / "iso.sqlite"
    db.configure_db(str(target))
    db.init_db()
    eng = db.get_engine()
    assert str(target) in str(eng.url)
    assert target.exists()


def test_appsetting_round_trip(tmp_path):
    from ba2_common.core import db
    from ba2_common.core.models import AppSetting
    db.configure_db(str(tmp_path / "rt.sqlite"))
    db.init_db()
    db.add_instance(AppSetting(key="x", value_str="42"))
    assert db.get_setting("x") == "42"


def test_no_engine_at_import():
    """Importing the db module must not eagerly build the engine.

    Checked in a FRESH interpreter, not via ``importlib.reload()``. reload()
    re-executes the module body into the *live* module ``__dict__``, rebinding
    every name in it -- including ``class InstanceNotFound``. Any module that did
    ``from ba2_common.core.db import InstanceNotFound`` at its own import time
    keeps the OLD class object, while ``db.get_instance`` (whose ``__globals__``
    *is* that same, mutated dict) starts raising the NEW one, so
    ``except InstanceNotFound`` / ``absorb_if_benign(e, InstanceNotFound)``
    silently stop matching. That leaked out of this test and broke 5 tests in
    test_new_option_actions.py whenever this directory was run as a whole, while
    every file passed on its own. reload() also reset ``_engine``, ``_tls``, the
    write lock and the activity-log queue, and double-registered the atexit hook.

    A subprocess asserts the actual import-time invariant and mutates nothing.
    """
    verdict = probe_verdict(
        "import ba2_common.core.db as db\n"
        f"print({MARK!r} + repr(db._engine))\n"
    )
    assert verdict == "None", f"engine was built at import time: {verdict}"


def test_fresh_database_is_stamped_at_alembic_head(db_module, tmp_path):
    """A brand-new file: init_db() creates the schema AND records that it is at head,
    so the very first `migrate.py upgrade` is a no-op instead of a replay from base."""
    target = tmp_path / "genesis.sqlite"
    db_module.configure_db(str(target))
    db_module.init_db()
    assert _version_rows(target) == [_repo_head()]


def test_existing_database_with_a_version_row_is_not_restamped(db_module, tmp_path):
    """An installed database mid-chain keeps its revision: re-stamping it to head
    would silently skip every migration it still owes."""
    target = tmp_path / "midchain.sqlite"
    db_module.configure_db(str(target))
    db_module.init_db()
    connection = sqlite3.connect(str(target))
    connection.execute("UPDATE alembic_version SET version_num='0000deadbeef'")
    connection.commit()
    connection.close()

    db_module.configure_db(str(target))   # restart against the same file
    db_module.init_db()
    assert _version_rows(target) == ["0000deadbeef"]


def test_existing_database_without_a_version_row_is_left_unstamped(db_module, tmp_path):
    """The known-broken state (tables, no alembic_version) is NOT a fresh install.

    It has real pending migrations; stamping it to head would skip them silently.
    Detection therefore keys off "the database had no tables at all", observed
    before create_all -- not off the absence of a version row.
    """
    target = tmp_path / "broken.sqlite"
    db_module.configure_db(str(target))
    db_module.init_db()
    connection = sqlite3.connect(str(target))
    connection.execute("DROP TABLE alembic_version")
    connection.commit()
    connection.close()

    db_module.configure_db(str(target))
    db_module.init_db()
    assert _version_rows(target) is None


def test_in_memory_database_is_not_stamped(db_module):
    """Throwaway backtest DBs (`:memory:`) are never migrated -- no stamp, no cost."""
    from sqlalchemy import inspect
    db_module.configure_db(":memory:")
    db_module.init_db()
    assert "alembic_version" not in inspect(db_module.get_engine()).get_table_names()


def test_branched_history_stamps_nothing(db_module, tmp_path, monkeypatch):
    """With several heads there is no single right answer, so refuse to guess."""
    scripts = tmp_path / "fake" / "alembic"
    (scripts / "versions").mkdir(parents=True)
    (tmp_path / "fake" / "alembic.ini").write_text("[alembic]\n")
    for revision in ("aaaa11112222", "bbbb33334444"):
        (scripts / "versions" / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = None\n"
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"
        )
    monkeypatch.setattr(db_module, "_find_alembic_dir", lambda: str(scripts))
    db_module._alembic_head_script = db_module._UNRESOLVED

    target = tmp_path / "branched.sqlite"
    db_module.configure_db(str(target))
    db_module.init_db()
    assert _version_rows(target) is None


def test_missing_alembic_does_not_break_startup():
    """alembic is a dev/ops tool, not a runtime dependency: with it unimportable the
    schema is still created and init_db() still returns -- only the stamp is skipped.

    Run in a fresh interpreter so the meta_path block is real (this session has
    already imported alembic) and nothing is mutated here.
    """
    verdict = probe_verdict(
        "import sys, os, tempfile\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'alembic' or name.startswith('alembic.'):\n"
        "            raise ImportError('alembic unavailable (simulated)')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from sqlalchemy import inspect\n"
        "from ba2_common.core import db\n"
        "db.configure_db(os.path.join(tempfile.mkdtemp(), 'no_alembic.sqlite'))\n"
        "db.init_db()\n"
        "tables = set(inspect(db.get_engine()).get_table_names())\n"
        f"print({MARK!r} + ('schema' if 'expertinstance' in tables else 'no-schema')\n"
        "      + ',' + ('stamped' if 'alembic_version' in tables else 'unstamped'))\n"
    )
    assert verdict == "schema,unstamped", verdict
