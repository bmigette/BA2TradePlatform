import os, tempfile, pathlib
import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_db():
    """Point ba2_common's DB seam at a throwaway sqlite for the whole test session."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "test.sqlite"
    from ba2_common.core import db
    db.configure_db(str(tmp))   # defined in Task 3
    db.init_db()
    yield
