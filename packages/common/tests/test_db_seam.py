"""DB-seam tests: configure_db points ba2_common at a throwaway sqlite, init_db
creates the schema lazily, and AppSetting survives a round-trip through the
db helpers. AppSetting field names (key, value_str) reconciled against
ba2_common/core/models.py."""


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
    """Importing the db module must not eagerly build the engine."""
    import importlib
    import ba2_common.core.db as db
    importlib.reload(db)
    assert db._engine is None
