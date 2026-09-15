import os, tempfile, pathlib
import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    """Point ba2_common's DB seam at a throwaway sqlite for the whole test session.

    The DB seam (ba2_common.core.db.configure_db) lands in Task 3. Until then this
    fixture is a safe no-op so that the foundation-leaf tests (which touch no DB)
    can run. Once db.py exists, it isolates the whole session to a temp sqlite.
    """
    tmp = pathlib.Path(tempfile.mkdtemp()) / "test.sqlite"
    try:
        from ba2_common.core import db
    except ImportError:
        # DB seam not yet present (pre-Task-3); nothing to isolate.
        yield
        return
    if hasattr(db, "configure_db"):
        db.configure_db(str(tmp))
        db.init_db()
    yield


@pytest.fixture(autouse=True)
def _restore_db_seam():
    """Give every test back the session's initialized DB, whatever it repointed.

    THE DEFECT THIS EXISTS FOR. ``configure_db`` is a process-global seam, and
    several tests here point it at a fresh temp file to exercise the seam itself
    (``test_threadlocal_db.py``, ``test_db_seam.py``). None of them ran
    ``init_db()`` on that file and none put the seam back, so every test that ran
    AFTER them -- alphabetically, ``test_tp_action_post_hook_preserves_broker_write.py``
    -- opened a SCHEMA-LESS database and failed. The failure looked like a bug in
    the code under test and moved when either file was renamed, which is the
    signature of leaked global state, not of a real defect.

    Restoring is cheap (``configure_db`` only rebinds a path and drops the
    memoized engine) and belongs here rather than in each test: a seam that must
    be put back by hand is one a future test will forget.
    """
    try:
        from ba2_common.core import db
    except ImportError:
        yield
        return

    previous_file = getattr(db, "_db_file", None)
    # The alembic head is memoized per database; a test that repointed the seam
    # may have resolved (or failed to resolve) a different one.
    had_head = hasattr(db, "_alembic_head_script")
    previous_head = getattr(db, "_alembic_head_script", None)
    try:
        yield
    finally:
        if getattr(db, "_db_file", None) != previous_file and previous_file is not None:
            db.configure_db(previous_file)
        if had_head:
            db._alembic_head_script = previous_head
        # A thread-local override outlives the thread that set it only if the
        # test left one behind on the main thread; drop it either way.
        if hasattr(db, "clear_threadlocal_db"):
            db.clear_threadlocal_db()
