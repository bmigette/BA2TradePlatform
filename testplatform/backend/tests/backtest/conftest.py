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

import sys
from pathlib import Path

import pytest


def _ensure_live_tree_on_path() -> None:
    """Put the repo root (the checkout's ``ba2_trade_platform`` live tree) on ``sys.path``.

    A handful of live<->backtest parity tests (``test_option_breaker_parity.py``,
    ``test_per_leg_expiry_parity.py``) import the LIVE tree directly -- e.g.
    ``ba2_trade_platform.modules.accounts.AlpacaAccount`` -- to prove identity/parity against
    the real live account classes, not a double. ``ba2_trade_platform`` lives at the repo root
    (three directories above ``testplatform/backend``), which is on no import path when pytest
    runs from ``testplatform/backend`` (the working directory this suite is always run from),
    so those imports raise ``ModuleNotFoundError`` unless something adds it. Walk up from this
    file (rather than hard-coding a parent count) to the first ancestor that actually contains
    a ``ba2_trade_platform`` package, and prepend it.
    """
    try:
        import ba2_trade_platform  # noqa: F401  (already importable -- nothing to do)
        return
    except ModuleNotFoundError:
        pass

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "ba2_trade_platform" / "__init__.py").is_file():
            sys.path.insert(0, str(candidate))
            return



def _ensure_launcher_on_path() -> None:
    """Put ``testplatform/`` on ``sys.path`` so ``import ba2test_launcher`` works.

    ``ba2test_launcher.py`` sits one level ABOVE the working directory this suite runs from,
    and nothing installs it: CI pip-installs packages/{common,providers,experts} and nothing
    else. On a dev box a bare ``import ba2test_launcher`` resolves anyway, through the editable
    install of ``ba2test_app`` -- a .pth finder that exists only because someone once ran
    ``pip install -e`` on the testplatform tree. So such an import collects locally and raises
    ``ModuleNotFoundError`` on CI, which pytest reports as "Interrupted: 1 error during
    collection" and exit code 2: the whole tests/backtest step fails with no test having run.
    That is exactly what test_inert_rm_toggles_stay_off.py did, and it left CI red from the
    commit that added it (a0acd65d) until 2026-09-07.

    Fixed HERE rather than per-file so the next test to need the launcher cannot reintroduce
    it. Walk up to the ancestor that actually holds the module, the same way
    ``_ensure_live_tree_on_path`` locates the live tree, instead of counting parents.
    """
    try:
        import ba2test_launcher  # noqa: F401  (already importable -- nothing to do)
        return
    except ModuleNotFoundError:
        pass

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "ba2test_launcher.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_ensure_live_tree_on_path()
_ensure_launcher_on_path()

# Dummy credential values seeded into the throwaway test DB so provider __init__ calls
# that read get_app_setting(...) do not raise. These are NOT real keys.
_SEED_KEYS = {
    "FMP_API_KEY": "test-fmp-key",
    "finnhub_api_key": "test-finnhub-key",
}


@pytest.fixture(scope="session", autouse=True)
def _seed_backtest_credentials(tmp_path_factory):
    """Point ba2_common at a throwaway DB and seed dummy credential keys for the session.

    Yields the ``db_file`` path (rather than ``None``) so a test whose OWN fixture calls
    ``ba2_common.core.db.configure_db(...)`` directly (a raw global reassignment, not the
    per-thread ``configure_db_threadlocal`` override backtests use, and not the
    ``backtest_trading_db()`` context manager, both of which restore themselves) can request
    this fixture by name to get back the ONE db file that has these credentials seeded, and
    repoint the global engine at it in its own ``finally``. Without that restore, whichever
    test in the session configures the db last and never repoints it back leaves every
    LATER test that assumes the seeded credentials exist (e.g. constructing a real
    ``FMPOHLCVProvider``) failing with "FMP API key not configured" -- an order-dependent
    failure with nothing wrong at the failing test itself.
    """
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

    yield db_file
