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
