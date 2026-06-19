"""Pytest fixtures local to ``tests/backtest``.

Why this exists
---------------
A few seam-wiring tests construct real providers (e.g. ``FMPOHLCVProvider``) whose
``__init__`` calls ``get_app_setting("FMP_API_KEY")`` and raises loudly when the key
is missing. The credential AppSetting rows live in the ba2_common-configured DB, but
that DB was relocated (the test box now keeps real keys in ``~/Documents/ba2/test``),
so a bare ``pytest`` run finds no keys and provider construction blows up.

The autouse fixture below points ba2_common at a THROWAWAY temp sqlite DB for the whole
test session and seeds DUMMY credential values into its ``AppSetting`` table. This makes
provider construction succeed without ever touching the real keys DB.

Important safety properties:
  * We use ``configure_db`` to a temp file under ``tmp_path_factory`` — never the real
    ``~/Documents/ba2/test/dl_forecasting.db``.
  * Per-run backtests override the engine PER THREAD (``configure_db_threadlocal``) and
    restore it in their ``finally``, so this session-global temp DB stays intact and the
    backtest-isolation tests still see their own ``:memory:`` / per-run sqlite engines.
"""
from __future__ import annotations

import pytest

# Dummy credential values seeded into the throwaway test DB so provider __init__ calls
# that read get_app_setting(...) do not raise. These are NOT real keys.
_SEED_KEYS = {
    "FMP_API_KEY": "test-fmp-key",
    "finnhub_api_key": "test-finnhub-key",
}


@pytest.fixture(scope="session", autouse=True)
def _seed_backtest_credentials(tmp_path_factory):
    """Point ba2_common at a throwaway DB and seed dummy credential keys for the session."""
    from ba2_common.core import db as common_db
    from ba2_common.core.models import AppSetting
    from sqlmodel import Session, select

    db_file = tmp_path_factory.mktemp("ba2-keys") / "backtest_keys.sqlite"
    common_db.configure_db(str(db_file))
    common_db.init_db()  # create AppSetting (and the rest of the schema) in the temp DB

    engine = common_db.get_engine()
    with Session(engine) as session:
        for key, value in _SEED_KEYS.items():
            existing = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            if existing:
                existing.value_str = value
                session.add(existing)
            else:
                session.add(AppSetting(key=key, value_str=value))
        session.commit()

    yield
