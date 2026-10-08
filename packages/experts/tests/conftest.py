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


@pytest.fixture(autouse=True)
def _reset_deterministic_scorer_caches():
    """The DeterministicScorer OHLCV caches are process-global; a test that fills them must not
    leak a frame into the next one (test order must not matter)."""
    from ba2_experts.DeterministicScorer import data
    data.reset_caches()
    yield
    data.reset_caches()
