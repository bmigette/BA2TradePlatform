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

import importlib
import sys
from pathlib import Path

import pytest


def checkout_root() -> Path:
    """The checkout THIS conftest belongs to: the ancestor holding the live tree.

    Walked (rather than a hard-coded parent count) so moving the suite cannot
    silently point it somewhere else.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "ba2_trade_platform" / "__init__.py").is_file():
            return candidate
    raise RuntimeError(
        f"no checkout root above {here}: nothing here contains ba2_trade_platform/")


def module_is_under(module, root: Path) -> bool:
    """Is this imported module's file inside ``root``?"""
    path = getattr(module, "__file__", None)
    if path is None:
        return False
    try:
        Path(path).resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _ensure_from_this_checkout(name: str, directory: Path, root: Path) -> None:
    """Make ``import name`` resolve inside THIS checkout, or fail loudly.

    Two problems, one function.

    (1) ``ba2_trade_platform`` (the live tree, imported by the live<->backtest
    parity tests) and ``ba2test_launcher`` are on no import path when pytest runs
    from ``testplatform/backend``, and CI installs neither -- so a bare import
    raises ``ModuleNotFoundError`` and the whole step dies during collection. That
    is what ``test_inert_rm_toggles_stay_off.py`` did to CI (red from a0acd65d to
    2026-09-07).

    (2) On a dev box those imports DO resolve -- through the venv's editable
    installs, which point at the MAIN checkout by absolute path. In a git worktree
    that silently runs another checkout's code: importing the main
    ``ba2test_launcher`` also puts the MAIN ``testplatform/backend`` on
    ``sys.path`` (it does that at import time), so every later ``app.*`` import
    comes from there too. Measured, not theorised: it made this worktree's
    ``tests/replay`` fail against a stale ``gather_tape`` whenever ``tests/backtest``
    was collected in the same run, with a message describing code that no longer
    exists here.

    So: APPEND this checkout's directory (append, never insert -- the repo root
    holds a ``tests`` package of its own, and putting it ahead of the working
    directory would shadow ``tests.replay``), import, and then VERIFY the file we
    got is inside this checkout. A module already imported from elsewhere cannot be
    re-pointed safely once objects are bound to it, so that case raises instead of
    pretending: a loud stop beats a run that silently measures another checkout.
    """
    module = sys.modules.get(name)
    if module is None:
        if str(directory) not in sys.path:
            sys.path.append(str(directory))
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"{name} is not importable from this checkout ({directory})") from exc
    if not module_is_under(module, root):
        raise RuntimeError(
            f"{name} resolved to {getattr(module, '__file__', '?')}, which is OUTSIDE "
            f"this checkout ({root}). That is the venv's editable install pointing at "
            f"another checkout; this suite would silently exercise its code. Run pytest "
            f"from this checkout's testplatform/backend, and check that its pytest.ini "
            f"pythonpath still lists this checkout's packages."
        )


_ROOT = checkout_root()
_ensure_from_this_checkout("ba2_trade_platform", _ROOT, _ROOT)
_ensure_from_this_checkout("ba2test_launcher", _ROOT / "testplatform", _ROOT)

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
