"""The root test suite never opens a real database or writes into a real log folder.

Three accounts named ``acc-fmp`` were found in the DEV database on 2026-11-18: a test called the real
``wire_all_seams()``, which pointed the engine at ``config.DB_FILE`` (then the dev DB), and created an
account. The same default sent every test's log records into the dev instance's log folder. ``conftest``
now points both at a session temp folder before anything is imported, and refuses any other DB file.
"""
import os
import tempfile

import pytest

from tests import conftest


def _inside(path, root):
    path, root = (os.path.normcase(os.path.realpath(p)) for p in (path, root))
    return os.path.commonpath([path, root]) == root


def test_configured_db_and_log_paths_are_in_the_session_temp_folder():
    import ba2_common.config as common_config
    import ba2_trade_platform.config as app_config

    for module in (app_config, common_config):
        assert _inside(module.DB_FILE, conftest.TEST_HOME), (module.__name__, module.DB_FILE)
        assert _inside(module.LOG_FOLDER, conftest.TEST_HOME), (module.__name__, module.LOG_FOLDER)


def test_both_loggers_write_into_the_session_temp_folder():
    import ba2_common.logger as common_log
    import ba2_trade_platform.logger as app_log

    for module in (app_log, common_log):
        assert _inside(module.LOGS_DIR, tempfile.gettempdir()), (module.__name__, module.LOGS_DIR)


def test_the_default_db_file_of_the_package_is_not_a_real_store():
    import ba2_common.core.db as pkg_db

    assert _inside(pkg_db._DEFAULT_DB_FILE, conftest.TEST_HOME), pkg_db._DEFAULT_DB_FILE


def test_an_engine_on_a_file_outside_a_temp_folder_is_refused():
    import ba2_common.core.db as pkg_db

    real = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "trade", "db.sqlite")
    with pytest.raises(RuntimeError, match="outside a temporary folder"):
        pkg_db._build_engine(real)


def test_an_engine_on_a_temp_file_or_in_memory_is_built(tmp_path):
    import ba2_common.core.db as pkg_db

    for target in (str(tmp_path / "ok.sqlite"), ":memory:"):
        pkg_db._build_engine(target).dispose()


def test_real_wiring_cannot_reach_a_real_database(test_engine):
    """The exact path of the leak: the REAL ``wire_all_seams()`` followed by a DB write."""
    import ba2_common.core.db as pkg_db
    from tests.test_seam_wiring import _isolated_seam_state

    with _isolated_seam_state() as seam_wiring:
        seam_wiring._wired = False
        seam_wiring.wire_all_seams()
        assert _inside(pkg_db._db_file, conftest.TEST_HOME), pkg_db._db_file
