"""DB-seam tests: configure_db points ba2_common at a throwaway sqlite, init_db
creates the schema lazily, and AppSetting survives a round-trip through the
db helpers. AppSetting field names (key, value_str) reconciled against
ba2_common/core/models.py."""
from ._leakgate import MARK, probe_verdict


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
