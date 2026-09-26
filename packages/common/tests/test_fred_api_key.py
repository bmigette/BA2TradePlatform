"""The FRED key has ONE name (AppSetting ``fred_api_key``) and one resolver.

The test platform's settings page used to save it as ``FRED_API_KEY``; AppSetting lookups are
exact, so nothing ever read that row. These pin the resolver's order (env override, then the
canonical setting), its refusal of a legacy-only row, and the idempotent migration.
"""
import sqlite3

import pytest

from ba2_common.core import fred_api_key as fk


@pytest.fixture
def settings(monkeypatch):
    """A fake AppSetting table behind ``ba2_common.config.get_app_setting``."""
    rows = {}
    monkeypatch.setattr("ba2_common.config.get_app_setting",
                        lambda key, default=None: rows.get(key, default))
    monkeypatch.delenv(fk.FRED_API_KEY_ENV, raising=False)
    return rows


def test_the_canonical_setting_is_read(settings):
    settings["fred_api_key"] = "canon"
    assert fk.resolve_fred_api_key() == "canon"
    assert fk.require_fred_api_key("x") == "canon"


def test_the_env_var_is_an_explicit_override(settings, monkeypatch):
    settings["fred_api_key"] = "canon"
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    assert fk.resolve_fred_api_key() == "from-env"


def test_a_missing_key_refuses_when_required(settings):
    assert fk.resolve_fred_api_key() is None
    with pytest.raises(fk.FredApiKeyMissing, match="fred_api_key") as e:
        fk.require_fred_api_key("the options cache build")
    assert "the options cache build" in str(e.value)
    assert isinstance(e.value, ValueError)


def test_a_legacy_only_row_is_refused_not_read_as_no_key(settings):
    settings["FRED_API_KEY"] = "legacy"
    with pytest.raises(fk.FredApiKeyMisnamed, match="migrate_fred_api_key"):
        fk.resolve_fred_api_key()


def test_the_canonical_row_wins_over_a_leftover_legacy_one(settings):
    settings["FRED_API_KEY"] = "legacy"
    settings["fred_api_key"] = "canon"
    assert fk.resolve_fred_api_key() == "canon"


# --------------------------------------------------------------------------- migration
def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE appsetting (id INTEGER PRIMARY KEY, key TEXT UNIQUE, "
                 "value_str TEXT)")
    for k, v in rows:
        conn.execute("INSERT INTO appsetting (key, value_str) VALUES (?, ?)", (k, v))
    return conn


def _rows(conn):
    return sorted(conn.execute("SELECT key, value_str FROM appsetting").fetchall())


def test_a_legacy_row_is_renamed_and_the_migration_is_idempotent():
    conn = _db([("FRED_API_KEY", "abc"), ("FMP_API_KEY", "fmp")])
    assert fk.migrate_legacy_fred_api_key(conn) == "renamed"
    assert _rows(conn) == [("FMP_API_KEY", "fmp"), ("fred_api_key", "abc")]
    assert fk.migrate_legacy_fred_api_key(conn) == "nothing-to-do"
    assert _rows(conn) == [("FMP_API_KEY", "fmp"), ("fred_api_key", "abc")]


def test_a_duplicate_legacy_row_is_dropped():
    conn = _db([("FRED_API_KEY", "abc"), ("fred_api_key", "abc")])
    assert fk.migrate_legacy_fred_api_key(conn) == "dropped-duplicate"
    assert _rows(conn) == [("fred_api_key", "abc")]


def test_an_empty_canonical_row_is_replaced_by_the_legacy_value():
    conn = _db([("FRED_API_KEY", "abc"), ("fred_api_key", "")])
    assert fk.migrate_legacy_fred_api_key(conn) == "renamed"
    assert _rows(conn) == [("fred_api_key", "abc")]


def test_two_different_keys_are_refused_not_chosen_between():
    conn = _db([("FRED_API_KEY", "abc"), ("fred_api_key", "xyz")])
    with pytest.raises(ValueError, match="DIFFERENT values"):
        fk.migrate_legacy_fred_api_key(conn)
    assert _rows(conn) == [("FRED_API_KEY", "abc"), ("fred_api_key", "xyz")]


def test_the_migration_tool_moves_a_legacy_row_in_a_real_db(tmp_path):
    import importlib.util
    import pathlib

    db = tmp_path / "keys.sqlite"
    conn = _db([])
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.execute("INSERT INTO appsetting (key, value_str) VALUES ('FRED_API_KEY', 'k')")
    disk.commit()
    disk.close()

    tool = pathlib.Path(__file__).resolve().parents[3] / "tools" / "migrate_fred_api_key.py"
    spec = importlib.util.spec_from_file_location("_migrate_fred_api_key", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.main(["--db", str(db), "--dry-run"]) == 0
    assert _rows(sqlite3.connect(db)) == [("FRED_API_KEY", "k")]      # dry run changed nothing
    assert mod.main(["--db", str(db)]) == 0
    assert _rows(sqlite3.connect(db)) == [("fred_api_key", "k")]
    assert mod.main(["--db", str(db)]) == 0                          # idempotent
    assert _rows(sqlite3.connect(db)) == [("fred_api_key", "k")]
