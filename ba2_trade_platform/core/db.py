"""Re-export shim: implementation lives in ba2_common.core.db (Phase 6 migration).

Kept so existing ``from ba2_trade_platform.core.db import ...`` imports resolve
unchanged. The single source of truth is now ba2_common.core.db; do not add
logic here.

Split-shim note: the package made the SQLAlchemy engine LAZY (built on first
``get_engine()`` after ``configure_db()``), so it no longer exposes a module-level
``engine`` global. One live caller (thirdparties/TradingAgents db_storage) does
``from ba2_trade_platform.core.db import Session, engine``. We re-expose ``engine``
as a lazily-resolved module attribute via ``__getattr__`` so that import keeps
working AND still honours the configure-then-build seam (the import happens at call
time, after wire_all_seams() has run configure_db()).
"""
from ba2_common.core.db import *  # noqa: F401,F403
from ba2_common.core.db import get_engine  # noqa: F401  (explicit: ensure present)


def __getattr__(name):
    # PEP 562 module-level __getattr__: resolve the legacy ``engine`` symbol
    # lazily so callers that did ``from ...core.db import engine`` keep working
    # against the package's lazy engine.
    if name == "engine":
        return get_engine()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
